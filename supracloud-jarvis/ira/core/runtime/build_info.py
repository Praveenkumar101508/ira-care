"""Safe build metadata — what is running, and which commit built it.

Every log line and error response carries this, because the first question in
any incident is *which version is this?* and the second is *which commit?*

Injected at container build time:

    docker build --build-arg GIT_COMMIT=$(git rev-parse HEAD) \
                 --build-arg BUILD_TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ) ...

Falls back to reading `.git` when the variables are absent, so a developer
running uvicorn locally still gets a real commit. The fallback is deliberately
best-effort and read-only: it never shells out to `git`, only reads `.git/HEAD`
and the ref file it points at. Shelling out from an import path would be a
startup dependency on git being installed, which in a distroless container it is
not.

**Nothing here may expose repository credentials, remote URLs, branch topology
or history.** A commit SHA and a version string are safe to return from an
unauthenticated endpoint; a remote URL with an embedded token is not, which is
why `.git/config` is never read.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

UNKNOWN = "unknown"


@dataclass(frozen=True)
class BuildInfo:
    version: str
    git_commit: str
    git_commit_short: str
    build_timestamp: str
    environment: str
    service: str

    def as_dict(self) -> dict[str, str]:
        return {
            "service": self.service,
            "version": self.version,
            "environment": self.environment,
            "git_commit": self.git_commit_short,
            "build_timestamp": self.build_timestamp,
        }


def _read_git_commit(start: Path | None = None) -> str:
    """Best-effort commit SHA from `.git`, without invoking git."""
    try:
        here = start or Path(__file__).resolve()
        for parent in [here, *here.parents]:
            git = parent / ".git"
            if not git.exists():
                continue
            # A worktree's .git is a file pointing at the real directory.
            if git.is_file():
                content = git.read_text(encoding="utf-8", errors="replace").strip()
                if content.startswith("gitdir:"):
                    git = Path(content.split(":", 1)[1].strip())
                    if not git.is_absolute():
                        git = (parent / git).resolve()
            head = git / "HEAD"
            if not head.is_file():
                continue
            ref = head.read_text(encoding="utf-8", errors="replace").strip()
            if ref.startswith("ref:"):
                ref_path = git / ref.split(":", 1)[1].strip()
                if ref_path.is_file():
                    return ref_path.read_text(encoding="utf-8", errors="replace").strip()
                # Packed refs — scan for the branch.
                packed = git / "packed-refs"
                if packed.is_file():
                    target = ref.split(":", 1)[1].strip()
                    for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                        if line.startswith("#") or " " not in line:
                            continue
                        sha, name = line.split(" ", 1)
                        if name.strip() == target:
                            return sha.strip()
                return UNKNOWN
            return ref  # detached HEAD holds the SHA directly
    except Exception:  # noqa: BLE001
        pass
    return UNKNOWN


@lru_cache(maxsize=1)
def get_build_info(service: str = "ira-api") -> BuildInfo:
    """Resolve build metadata once per process."""
    from core.runtime.environment import get_environment

    commit = (os.getenv("GIT_COMMIT") or "").strip() or _read_git_commit()
    version = (os.getenv("IRA_VERSION") or "").strip()
    if not version:
        try:
            from config import get_settings
            version = get_settings().ira_version
        except Exception:  # noqa: BLE001
            version = UNKNOWN

    return BuildInfo(
        version=version or UNKNOWN,
        git_commit=commit or UNKNOWN,
        git_commit_short=(commit[:12] if commit and commit != UNKNOWN else UNKNOWN),
        build_timestamp=(os.getenv("BUILD_TIMESTAMP") or "").strip() or UNKNOWN,
        environment=get_environment().value,
        service=(os.getenv("IRA_SERVICE_NAME") or service).strip() or service,
    )


def reset_build_info_cache() -> None:
    """Clear the cache. For tests that vary the environment."""
    get_build_info.cache_clear()
