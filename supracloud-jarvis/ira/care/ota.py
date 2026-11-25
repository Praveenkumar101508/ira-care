"""IRA CARE OS over-the-air updates, the server side (requirement E4.d).

A paired watch asks "is there an update for me?" with its build fingerprint and its current
verified-boot rollback index. The server answers from the deployment's release catalogue
(`os_releases`, migration 120) and signs the answer, so the device acts only on a manifest
the deployment's OTA key vouched for, whatever carried it.

The rules, each tested in tests/care/test_ota.py and tests/tenancy/test_os_update_db.py:

* Only a paired device is answered (the device token, as every other device route).
* A release with a LOWER rollback index than the device's is never offered: an attacker who
  can replay an old, vulnerable image must not get the server to hand it out. (The bootloader
  refuses one anyway, E4.e; the server does not rely on it.)
* The image the device already runs is not offered again.
* Staged rollout: a device is in a release's stage when its bucket, a hash of the release and
  the device, 0 to 99, is below `rollout_percent`. The same device always gets the same answer
  for the same release, so a staged rollout widens without devices flapping in and out.
* Withdrawn releases are never offered.
* The answer is a canonical JSON manifest signed with Ed25519. No signing key configured
  means no answer (503), never an unsigned one.

What this does NOT do: serve the image bytes (object storage serves `payload_url`), sign the
image (the A/B payload and its verified-boot signature are made on the build host with the
release keys, E4.c and E4.g), or apply anything on a device. Nothing here has run against a
real image or a real device.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Iterable, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

SIGNING_KEY_ENV = "IRA_OTA_MANIFEST_KEY"   # base64 of the 32-byte Ed25519 private key seed
MANIFEST_SCHEMA = 1
MANIFEST_LIFETIME = dt.timedelta(hours=1)


class SigningNotConfigured(RuntimeError):
    """No OTA signing key: the server must not answer at all."""


@dataclass(frozen=True)
class Release:
    id: str
    version: str
    build_fingerprint: str
    rollback_index: int
    payload_url: str
    payload_sha256: str
    payload_size: int
    rollout_percent: int
    withdrawn: bool
    published_at: dt.datetime

    @classmethod
    def from_row(cls, row) -> "Release":
        return cls(id=str(row["id"]), version=row["version"], build_fingerprint=row["build_fingerprint"],
                   rollback_index=int(row["rollback_index"]), payload_url=row["payload_url"],
                   payload_sha256=row["payload_sha256"], payload_size=int(row["payload_size"]),
                   rollout_percent=int(row["rollout_percent"]), withdrawn=bool(row["withdrawn"]),
                   published_at=row["published_at"])


def rollout_bucket(release_id: str, device_id: str) -> int:
    """0 to 99, fixed for a (release, device) pair."""
    digest = hashlib.sha256(f"ira-ota-rollout:{release_id}:{device_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 100


def in_rollout(release: Release, device_id: str) -> bool:
    return rollout_bucket(release.id, device_id) < release.rollout_percent


def choose(releases: Iterable[Release], *, device_id: str, build_fingerprint: str,
           rollback_index: int) -> Optional[Release]:
    """The release to offer this device, or None. Never one with a lower rollback index."""
    eligible = [
        r for r in releases
        if not r.withdrawn
        and r.rollback_index >= rollback_index
        and r.build_fingerprint != build_fingerprint
        and in_rollout(r, device_id)
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda r: (r.rollback_index, r.published_at, r.version))


def canonical(obj: dict) -> bytes:
    """The bytes that are signed: sorted keys, no spaces, UTF-8. The device verifies these
    exact bytes, so the manifest travels as this string, never re-serialised."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def manifest_for(release: Release, *, device_id: str, now: dt.datetime) -> dict:
    return {
        "schema": MANIFEST_SCHEMA,
        "device_id": device_id,
        "release_id": release.id,
        "version": release.version,
        "build_fingerprint": release.build_fingerprint,
        "rollback_index": release.rollback_index,
        "payload": {"url": release.payload_url, "sha256": release.payload_sha256, "size": release.payload_size},
        "issued_at": now.isoformat(),
        "expires_at": (now + MANIFEST_LIFETIME).isoformat(),
    }


def load_signing_key(env: Optional[dict] = None) -> Ed25519PrivateKey:
    raw = (env if env is not None else os.environ).get(SIGNING_KEY_ENV, "")
    if not raw:
        raise SigningNotConfigured(f"{SIGNING_KEY_ENV} is not set")
    try:
        seed = base64.b64decode(raw, validate=True)
    except Exception as exc:  # noqa: BLE001 - any decoding fault is the same refusal
        raise SigningNotConfigured(f"{SIGNING_KEY_ENV} is not base64") from exc
    if len(seed) != 32:
        raise SigningNotConfigured(f"{SIGNING_KEY_ENV} must be a 32-byte Ed25519 seed")
    return Ed25519PrivateKey.from_private_bytes(seed)


def key_id(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def sign(manifest: dict, key: Ed25519PrivateKey) -> dict:
    body = canonical(manifest)
    return {
        "manifest": body.decode("utf-8"),
        "signature": base64.b64encode(key.sign(body)).decode("ascii"),
        "key_id": key_id(key.public_key()),
        "algorithm": "Ed25519",
    }


def verify(signed: dict, public_key: Ed25519PublicKey, *, device_id: str,
           now: Optional[dt.datetime] = None) -> dict:
    """What a device does before acting: check the signature over the exact bytes, that the
    manifest names this device, and that it has not expired. Returns the manifest or raises
    ValueError. The watch-side implementation must do the same checks."""
    body = signed["manifest"].encode("utf-8")
    try:
        public_key.verify(base64.b64decode(signed["signature"]), body)
    except (InvalidSignature, ValueError) as exc:
        raise ValueError("signature does not verify") from exc
    manifest = json.loads(body)
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("unknown manifest schema")
    if manifest.get("device_id") != device_id:
        raise ValueError("manifest is for another device")
    expires = dt.datetime.fromisoformat(manifest["expires_at"])
    if (now or dt.datetime.now(dt.timezone.utc)) >= expires:
        raise ValueError("manifest has expired")
    return manifest


async def offerable_releases(conn) -> list[Release]:
    rows = await conn.fetch(
        "SELECT * FROM os_releases WHERE NOT withdrawn AND rollout_percent > 0 "
        "ORDER BY rollback_index DESC, published_at DESC LIMIT 50")
    return [Release.from_row(r) for r in rows]


async def offer(conn, *, device_id: str, build_fingerprint: str, rollback_index: int,
                key: Ed25519PrivateKey, now: Optional[dt.datetime] = None) -> Optional[dict]:
    release = choose(await offerable_releases(conn), device_id=device_id,
                     build_fingerprint=build_fingerprint, rollback_index=rollback_index)
    if release is None:
        return None
    return sign(manifest_for(release, device_id=device_id, now=now or dt.datetime.now(dt.timezone.utc)), key)


# ── Operator side (scripts/os_release.py, with the migrator's credentials) ────────────────

async def publish(conn, *, version: str, build_fingerprint: str, rollback_index: int, payload_url: str,
                  payload_sha256: str, payload_size: int, rollout_percent: int = 0, notes: str = "") -> str:
    """Catalogue a release. It starts at 0 % unless told otherwise: publishing and offering are
    separate decisions. A release whose rollback index is below the highest already published is
    refused, so the catalogue itself can never make a downgrade look current."""
    highest = await conn.fetchval("SELECT max(rollback_index) FROM os_releases WHERE NOT withdrawn")
    if highest is not None and rollback_index < highest:
        raise ValueError(f"rollback index {rollback_index} is below the highest published ({highest})")
    return str(await conn.fetchval(
        "INSERT INTO os_releases (version, build_fingerprint, rollback_index, payload_url, payload_sha256,"
        " payload_size, rollout_percent, notes) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id",
        version, build_fingerprint, rollback_index, payload_url, payload_sha256, payload_size,
        rollout_percent, notes))


async def set_rollout(conn, release_id: str, percent: int) -> None:
    if not 0 <= percent <= 100:
        raise ValueError("rollout percent must be 0 to 100")
    await conn.execute("UPDATE os_releases SET rollout_percent = $2 WHERE id = $1", release_id, percent)


async def withdraw(conn, release_id: str) -> None:
    await conn.execute("UPDATE os_releases SET withdrawn = TRUE, rollout_percent = 0 WHERE id = $1", release_id)
