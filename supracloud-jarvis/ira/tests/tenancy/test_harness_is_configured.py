"""The tenancy job's preconditions, checked as a FAILURE rather than a skip.

Every other file in this directory begins with the same shape:

    if not DSN or not ADMIN_DSN:
        pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                    allow_module_level=True)

Which is right for a developer without a local PostgreSQL, and quietly wrong in
the job whose whole purpose is to run them. `IRA_TEST_ADMIN_DSN` was missing
from the "Adversarial cross-tenant tests" step, so `test_p205_totp_tenancy.py`,
`test_p205_totp_erasure.py` and `test_p205_mfa_encryption.py` skipped themselves
and the step went green having executed none of them — the cross-tenant
assertions, the erasure assertions and the whole encryption boundary.

The step already ran with `-ra` to make skips visible, and its comment already
said "A SKIP here is a failure of this job's purpose". Neither helped, for the
reason that keeps recurring here: **a skip is visible and still exits 0**, and
success is decided by the exit code. Making the log say something is not the
same as making the build fail.

── Why the trigger is "partial configuration", not "am I in CI" ────────────

The first version of this file gated on `CI`, and it was wrong in a way worth
recording: the `test` job runs `pytest tests/` — the WHOLE suite, this
directory included — against no database at all, by design. The database work
belongs to the separate "Tenant isolation" job. So a CI gate fired in a job
that legitimately has no DSNs and turned a correct skip into three failures.

The condition that actually describes the defect is **partial** configuration.
A job that sets one DSN has declared it intends to run database tests, so a
missing second one is a misconfiguration rather than a choice. A job that sets
neither wants unit tests, and this file gets out of its way. That is precisely
the shape the real bug had: `IRA_TEST_DATABASE_DSN` set, `IRA_TEST_ADMIN_DSN`
absent.

`IRA_REQUIRE_DB_HARNESS` covers the remaining case — someone deleting *both*
DSNs from the tenancy step, which partial-configuration cannot see. The step
sets it, so the demand survives the variables it is guarding.
"""
from __future__ import annotations

import os

import pytest

REQUIRED = {
    "IRA_TEST_DATABASE_DSN":
        "the application-role DSN. Every suite here asserts what the runtime "
        "role can and cannot see, which requires connecting AS that role — a "
        "superuser bypasses RLS entirely and would pass every negative "
        "assertion in this directory while proving nothing.",
    "IRA_TEST_ADMIN_DSN":
        "the privileged DSN used to SEED fixtures. A fixture has to create the "
        "very rows the policies under test are about to hide, which the "
        "application role cannot do — its own policies stop it. Without this, "
        "three P2-05 suites skip at module level and the job is green having "
        "run no cross-tenant, erasure or encryption assertion at all.",
}

#: Set by the workflow step that runs this directory against PostgreSQL. Its
#: presence means "this job must have a database", independently of whether the
#: DSNs it is meant to carry are still there.
MARKER = "IRA_REQUIRE_DB_HARNESS"


def _configured() -> list[str]:
    return [name for name in REQUIRED if os.getenv(name)]


def _harness_is_expected() -> bool:
    """Is this a run that is SUPPOSED to reach a database?

    True when a DSN is present (something intends database work) or when the
    marker is set (a job has declared it). False for `pytest tests/` on a
    machine with no PostgreSQL, and for the CI `test` job, both of which are
    legitimate and must keep skipping.
    """
    return bool(_configured()) or bool(os.getenv(MARKER))


needs_harness = pytest.mark.skipif(
    not _harness_is_expected(),
    reason=("no database harness is configured or demanded — this run is not "
            "expected to reach PostgreSQL, so the tenancy suites skipping is "
            "correct"),
)


@needs_harness
@pytest.mark.parametrize("variable", sorted(REQUIRED))
def test_the_database_harness_is_fully_configured(variable: str):
    """Half a harness fails the job instead of silencing three suites."""
    assert os.getenv(variable), (
        f"{variable} is not set, but this run is configured for database "
        f"tests ({', '.join(_configured()) or MARKER} is set). Suites in "
        f"tests/tenancy/ will skip themselves at module level and the job will "
        f"report success without running them.\n\n{REQUIRED[variable]}\n\n"
        f"Set it in the workflow step that runs tests/tenancy/."
    )


@needs_harness
def test_the_two_dsns_are_not_the_same_role():
    """Both variables set to the same superuser DSN would satisfy the check
    above and defeat the point of it.

    Every negative assertion in this directory — "tenant B cannot read tenant
    A's row", "a tenant admin cannot read a member's second factor" — passes
    trivially against a role that bypasses row-level security. A harness
    misconfigured that way does not fail; it agrees with everything.
    """
    app = os.getenv("IRA_TEST_DATABASE_DSN", "")
    admin = os.getenv("IRA_TEST_ADMIN_DSN", "")
    if not app or not admin:
        pytest.skip("reported by test_the_database_harness_is_fully_configured")
    assert app != admin, (
        "IRA_TEST_DATABASE_DSN and IRA_TEST_ADMIN_DSN are identical. The "
        "application DSN must connect as a NOSUPERUSER, NOBYPASSRLS role, or "
        "every isolation assertion in this directory passes without testing "
        "anything."
    )


@pytest.mark.skipif(
    not os.getenv(MARKER),
    reason=(f"{MARKER} is not set — only the job that demands the database "
            "harness also demands the checkpoint saver"),
)
def test_the_checkpoint_saver_is_installed():
    """The same defect one layer down: a package, not a variable.

    Five real-database tests importorskip `langgraph.checkpoint.postgres` —
    three in tests/idempotency/test_p14r3_checkpoints.py and two in
    test_migration_097_checkpoint_tables.py. Until 30 October 2026 the
    tenancy job did not install it, so all five skipped and the job was
    green, behind a skip message claiming the job installed it. Gated on the
    marker alone, so a developer with a DSN but no saver still just skips.
    """
    import importlib.util

    assert importlib.util.find_spec("langgraph.checkpoint.postgres.aio") is not None, (
        "langgraph-checkpoint-postgres is not installed, but this job demands "
        f"the database harness ({MARKER} is set). The checkpoint-writer and "
        "migration-097 drift tests will skip and the job will report success "
        "without running them. Install requirements-test.txt, which pins it."
    )
