"""
Backup & Restore API — admin-only endpoints for database management.

GET  /backup/list                → list available backup files with metadata
GET  /backup/download/{filename} → download a specific .sql.gz backup
POST /backup/create              → trigger an immediate backup now
POST /backup/restore             → restore from uploaded .sql.gz file (DANGER)

All endpoints require admin authentication (403 for non-admin users).

Restore is the most destructive thing this API can do: the uploaded SQL runs
against the live database with the database's own credentials. So it takes two
requests, through the same approval guardrail as every other destructive route
(`utils/approval.py`):

  1. POST the archive with no token. Nothing is written or run. The answer is a
     one-time token and a preview naming THIS archive by its SHA-256 and size.
  2. POST the same archive again with `confirm_token` and `owner_password`
     (constitution B2: a high-risk action needs the owner password). Only then
     is it restored.

A token approves the archive it was issued for: a different upload with that
token is refused, and the token is spent. Until 31 October 2026 the only
confirmation was a fixed header, `X-Restore-Confirm: yes`, the same for every
archive, so a single request restored whatever it carried. That header is no
longer read.

While a lockdown is engaged (or its state cannot be read), both requests answer
423 Locked before the upload is read, so a token staged before the lockdown
cannot restore during it.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from api.middleware.auth import require_auth, require_owner
from api.routes._ledger import raise_if_ledger_refused
from api.routes._lockdown import refuse_during_lockdown
from config import get_settings
from utils.approval import owner_gated_action
from utils.private_files import ensure_private_dir, private_opener
from worker.backup import list_backups, restore_from_file, run_database_backup

router = APIRouter(prefix="/backup", tags=["backup"])
logger = logging.getLogger("ira.backup_api")


#: Owner authority is decided in one place, by `api.middleware.auth.require_owner`,
#: which refuses in the cloud edition. The local name is kept so the route
#: signatures below read unchanged. See 3C-04.
_require_admin = require_owner


@router.get("/list")
async def list_available_backups(_user: str = Depends(_require_admin)):
    """Return metadata for all available backups, newest first."""
    return {"backups": list_backups()}


@router.get("/download/{filename}")
async def download_backup(filename: str, _user: str = Depends(_require_admin)):
    """Download a specific backup file by filename."""
    # Prevent path traversal
    if any(c in filename for c in ("/", "\\", "..")):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not filename.endswith(".sql.gz"):
        raise HTTPException(status_code=400, detail="Only .sql.gz backups are available")

    path = Path(get_settings().backup_dir) / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Backup not found: {filename}")

    return FileResponse(
        path=str(path),
        media_type="application/gzip",
        filename=filename,
    )


@router.post("/create")
async def create_backup(_user: str = Depends(_require_admin)):
    """Trigger an immediate database backup. Returns metadata of the created file."""
    path = await run_database_backup()
    if not path:
        raise HTTPException(
            status_code=500,
            detail="Backup failed — check worker logs (is pg_dump installed?)",
        )
    stat = path.stat()
    return {
        "status": "created",
        "filename": path.name,
        "size_mb": round(stat.st_size / 1024 / 1024, 2),
    }


#: The guardrail action name, so a token staged for another action (a note
#: deletion, a memory forget) can never confirm a restore.
RESTORE_ACTION = "backup_restore"


def _restore_preview(sha256: str, size: int) -> str:
    """What the owner is asked to approve. Built from the bytes alone, never
    from the client's filename, so renaming the file does not change what the
    token approves and a different file cannot match it."""
    return (f"Restore the database from the uploaded archive sha256 {sha256} "
            f"({size} bytes). Its SQL runs against the live database; a "
            "pre-restore snapshot is attempted first. This cannot be undone "
            "from inside IRA.")


@router.post("/restore")
async def restore_backup(
    file: UploadFile = File(..., description="A .sql.gz backup file to restore from"),
    confirm_token: str | None = Form(
        None, description="The token from the first request; omit it to stage the restore"),
    owner_password: str | None = Form(
        None, description="The owner password; needed with confirm_token"),
    _user: str = Depends(_require_admin),
):
    """
    Restore the database from an uploaded .sql.gz backup file, in two steps.

    Without `confirm_token`: validate the archive and return a one-time token
    and a preview naming it by SHA-256. Nothing is written or restored.

    With `confirm_token` and `owner_password`: restore that same archive.

    WARNING: This applies the SQL dump on top of the current database.
    For a full reset, stop the API, drop and recreate the DB, then restore.
    """
    # Before anything else, on both requests: a lockdown refuses the stage and
    # the confirm alike. `require_owner` has already refused anyone else.
    await refuse_during_lockdown(risk="critical", action_type=RESTORE_ACTION, is_owner=True)

    if not file.filename or not file.filename.endswith(".sql.gz"):
        raise HTTPException(status_code=400, detail="Upload must be a .sql.gz backup file")

    from utils.file_utils import read_with_size_cap
    content = await read_with_size_cap(file, max_bytes=500 * 1024 * 1024)  # 500 MB cap
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    # Verify gzip integrity before anything else. In memory: nothing is written
    # until the stream is known good. (An earlier version also opened the temp
    # file here, before anything had been written to it, so every upload was
    # refused as "corrupt".)
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(content)) as _gz:
            _gz.read(512)  # Read a small chunk to verify the gzip header/stream
    except Exception as gz_err:
        raise HTTPException(status_code=400, detail=f"Invalid or corrupt .sql.gz file: {gz_err}")

    sha256 = hashlib.sha256(content).hexdigest()
    preview = _restore_preview(sha256, len(content))

    if confirm_token:
        # The password is checked before the token reaches the guardrail, so a
        # mistyped password refuses without spending the token.
        from security.delegated import verify_owner_password

        if not verify_owner_password(owner_password or ""):
            logger.warning("RESTORE refused: owner password missing or wrong (sha256=%s)", sha256)
            raise HTTPException(
                status_code=403,
                detail="Restoring a backup needs your owner password as well as the token.",
            )

    # The staged action restores the bytes of the request that STAGED it. The
    # confirming request must carry the same archive: its preview is rebuilt
    # from its own bytes, and a different SHA-256 does not match the token.
    filename = file.filename

    async def _do() -> dict:
        return await _restore_bytes(content, filename=filename, sha256=sha256)

    outcome = await owner_gated_action(
        owner_username=_user, is_owner=True,   # require_owner already decided
        action=RESTORE_ACTION, preview=preview,
        execute=_do, confirm_token=confirm_token,
    )
    # Class B: the ledger refusing alone does not stop a restore; the journal
    # refusing as well does, and the 503 names the break-glass.
    raise_if_ledger_refused(outcome)
    if outcome["status"] == "confirmation_required":
        logger.warning("RESTORE staged by admin: file=%s size_bytes=%d sha256=%s",
                       filename, len(content), sha256)
        return {**outcome, "sha256": sha256, "size_bytes": len(content)}
    if outcome["status"] in ("expired", "not_found"):
        raise HTTPException(
            status_code=409,
            detail=("Confirmation token invalid, already used, expired, or issued "
                    "for a different archive. Upload the archive again to get a "
                    "new token."),
        )
    if outcome["status"] == "forbidden":
        raise HTTPException(status_code=403, detail=outcome["detail"])
    return outcome["result"]


async def _restore_bytes(content: bytes, *, filename: str, sha256: str) -> dict:
    """Write the approved archive privately, snapshot, restore, clean up."""
    backup_dir = ensure_private_dir(Path(get_settings().backup_dir))
    # Use a UUID-based name — never trust the client-supplied filename
    tmp_path = backup_dir / f"restore_{uuid.uuid4()}.sql.gz"
    try:
        # 0600: an uploaded archive is somebody's whole database until it is unlinked
        with open(tmp_path, "wb", opener=private_opener) as fh:
            fh.write(content)

        logger.warning(
            "RESTORE initiated by admin: file=%s size_bytes=%d sha256=%s",
            filename, len(content), sha256,
        )

        # Take a pre-restore snapshot so we can recover if the restore goes wrong
        pre_restore_path = await run_database_backup()
        if pre_restore_path:
            logger.info("Pre-restore snapshot created: %s", pre_restore_path.name)
        else:
            logger.warning("Pre-restore snapshot failed — proceeding anyway")

        success, message = await restore_from_file(tmp_path)
        if not success:
            raise HTTPException(status_code=500, detail=message)

        logger.warning(
            "RESTORE complete: file=%s sha256=%s pre_restore_snapshot=%s",
            filename, sha256,
            pre_restore_path.name if pre_restore_path else "none",
        )
        return {
            "status": "restored",
            "message": message,
            "sha256": sha256,
            "pre_restore_snapshot": pre_restore_path.name if pre_restore_path else None,
        }
    finally:
        tmp_path.unlink(missing_ok=True)
