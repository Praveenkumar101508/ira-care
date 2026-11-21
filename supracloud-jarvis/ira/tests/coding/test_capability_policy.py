"""Tests for coding/capability_policy.py — fail-closed capability checks."""
import time

import pytest

from coding.capability_policy import (
    Capability, FORBIDDEN_FOR_WORKERS, check, issue_token, load_protected_paths,
)


@pytest.fixture()
def sandbox(tmp_path):
    root = tmp_path / "worktree"
    (root / "supracloud-jarvis" / "ira" / "security").mkdir(parents=True)
    (root / "supracloud-jarvis" / "ira" / "coding").mkdir(parents=True)
    return root


def _token(sandbox, caps=None, **kw):
    return issue_token(
        "IRA-ENG-000001", "worker-1",
        caps or {Capability.READ_REPO, Capability.WRITE_WORKTREE, Capability.GIT_COMMIT},
        [sandbox], **kw,
    )


def test_forbidden_capabilities_never_issued(sandbox):
    assert Capability.NETWORK_EXTERNAL in FORBIDDEN_FOR_WORKERS
    with pytest.raises(PermissionError, match="never grantable"):
        _token(sandbox, caps={Capability.NETWORK_EXTERNAL})


def test_no_push_capability_exists_at_all():
    # C7: the enum itself must not contain push/merge/delete-remote powers.
    names = {c.value for c in Capability}
    assert not any(("push" in n or "merge" in n or "delete" in n) for n in names)


def test_ungranted_capability_denied(sandbox):
    tok = _token(sandbox, caps={Capability.READ_REPO})
    ok, reason = check(tok, Capability.RUN_COMMANDS)
    assert not ok and "not granted" in reason


def test_unknown_capability_denied(sandbox):
    ok, reason = check(_token(sandbox), "format_disk")
    assert not ok and "unknown capability" in reason


def test_expired_token_denied(sandbox):
    tok = _token(sandbox, ttl_seconds=-1)
    ok, reason = check(tok, Capability.READ_REPO)
    assert not ok and "expired" in reason


def test_path_outside_allowed_roots_denied(sandbox, tmp_path):
    outside = tmp_path / "elsewhere" / "x.py"
    ok, reason = check(_token(sandbox), Capability.WRITE_WORKTREE, path=outside)
    assert not ok and "outside allowed roots" in reason


def test_relative_path_denied(sandbox):
    ok, reason = check(_token(sandbox), Capability.WRITE_WORKTREE, path="ira/x.py")
    assert not ok and "absolute" in reason


def test_write_inside_sandbox_allowed(sandbox):
    p = sandbox / "supracloud-jarvis" / "ira" / "coding" / "new_module.py"
    ok, reason = check(_token(sandbox), Capability.WRITE_WORKTREE, path=p)
    assert ok, reason


def test_protected_path_write_denied_without_elevation(sandbox):
    p = sandbox / "supracloud-jarvis" / "ira" / "security" / "roles.py"
    ok, reason = check(_token(sandbox), Capability.WRITE_WORKTREE, path=p)
    assert not ok and "protected path" in reason
    # git commit on a protected path is equally blocked
    ok, _ = check(_token(sandbox), Capability.GIT_COMMIT, path=p)
    assert not ok


def test_protected_path_read_allowed(sandbox):
    p = sandbox / "supracloud-jarvis" / "ira" / "security" / "roles.py"
    ok, reason = check(_token(sandbox), Capability.READ_REPO, path=p)
    assert ok, reason


def test_elevated_approval_unlocks_protected_path(sandbox):
    tok = _token(sandbox, elevated_protected_approval="owner:2026-07-12")
    p = sandbox / "supracloud-jarvis" / "ira" / "security" / "roles.py"
    ok, reason = check(tok, Capability.WRITE_WORKTREE, path=p)
    assert ok and "elevated approval" in reason


def test_gitignore_is_protected_at_root(sandbox):
    (sandbox / ".gitignore").parent.mkdir(exist_ok=True)
    ok, reason = check(_token(sandbox), Capability.WRITE_WORKTREE,
                       path=sandbox / ".gitignore")
    assert not ok and "protected" in reason


def test_empty_roots_and_bad_network_refused(sandbox):
    with pytest.raises(PermissionError, match="allowed root"):
        issue_token("IRA-ENG-000001", "w", {Capability.READ_REPO}, [])
    with pytest.raises(PermissionError, match="network"):
        _token(sandbox, network="internet")


def test_fallback_protected_paths_never_empty(tmp_path):
    # no constitution.yaml in tmp_path → fallback list must still protect
    paths = load_protected_paths(tmp_path)
    assert any("ira/security/" in p for p in paths)
    assert ".gitignore" in paths
