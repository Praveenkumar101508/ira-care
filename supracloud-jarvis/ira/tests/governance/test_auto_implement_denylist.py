"""The architect's auto-apply denylist covers the constitution's protected paths.

`utils/auto_implement.py` git-applies an LLM-produced diff once the owner types
"architect apply". Its `_is_protected_path` is defense-in-depth: the generator
"must never be allowed to rewrite its own security and control surface …
regardless of who triggered it". On `main` at `6b46efb` that list had drifted
from `docs/architecture/constitution.yaml` `protected_paths`: it protected
`api/middleware/auth.py` but not the rest of `api/middleware/`, and it did not
protect the `security/` authorisation tree (scope enforcer, owner gate,
delegated roles, JWT) at all — so a diff rewriting
`security/scope_enforcer.py` (the delegated default-deny) or
`security/owner_gate.py` (the owner check) would be applied.

The attack tests fail on `main` (negative control); the drift test keeps the
denylist and the constitution in step from here on.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD"):
    os.environ.setdefault(_k, "test-placeholder")

from utils.auto_implement import _is_protected_path

# Authorisation / tenant-isolation / egress files that an auto-applied diff must
# never be allowed to rewrite. Each is under the constitution's protected_paths
# or is the A1 egress chokepoint.
MUST_BE_PROTECTED = [
    "ira/security/scope_enforcer.py",   # delegated default-deny (S2)
    "security/owner_gate.py",           # the owner check (S5)
    "security/delegated.py",            # household roles
    "security/jwt.py",
    "supracloud-jarvis/ira/security/scope_enforcer.py",  # repo-root diff header form
    "api/middleware/scope.py",          # middleware beyond auth.py
    "utils/external_gate.py",           # egress master switch (A1)
    "core/identity/db_context.py",      # the RLS GUCs (tenant isolation)
]

# A plain feature file the architect is meant to be able to touch.
MUST_BE_ALLOWED = [
    "api/routes/image_gen.py",
    "features/some_feature.py",
    "utils/llm.py",
]


@pytest.mark.parametrize("path", MUST_BE_PROTECTED)
def test_security_surface_is_protected(path):
    assert _is_protected_path(path), f"auto-apply would rewrite {path!r}"


@pytest.mark.parametrize("path", MUST_BE_ALLOWED)
def test_ordinary_feature_files_are_not_blocked(path):
    assert not _is_protected_path(path), f"{path!r} should be applicable"


