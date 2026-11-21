"""The last word in this directory: what did the suites leave behind?

**The filename is load-bearing.** pytest collects files in alphabetical order,
`zz` sorts last, and this file only means anything if it runs after everything
else here. Renaming it to something tidier moves it earlier and silently turns
it into an assertion about a half-finished suite.

── Why this exists ─────────────────────────────────────────────────────────

`iratest` is shared. Every suite in this directory writes to it, and then the
"Rollback migration applies cleanly" job step peels the whole migration chain
off the same database. Several rollback scripts REFUSE while a multi-tenant
deployment would lose attribution by reversing them —
`rollback/035_memberships_down.sql` refuses while more than one tenant holds
membership data, which is correct: dropping the lifecycle columns destroys the
record of which rows belong to whom.

So a fixture that creates a tenant with memberships and does not remove them
does not fail its own suite. It fails a rollback step three stages later, with
an error naming 035 and nothing whatsoever pointing back at the fixture. That
happened: two abandoned fixture tenants from `test_p205_mfa_encryption.py`
turned 1377 passing tests into a red job, and the message read
"Refusing rollback: 3 tenants hold memberships."

Fixing that one fixture fixes one instance. This fixes the class: the next
fixture that leaks is named here, in the suite that leaked it, by a test whose
failure message says what to do.

── Scope, deliberately narrow ──────────────────────────────────────────────

Only membership residue is asserted, because only membership residue breaks the
cycle. Leftover `tenants` and `users` rows with no memberships are harmless to
every rollback guard, and several suites leave them; asserting on those would
be a tidiness rule dressed up as a correctness one, and it would fail for
reasons that do not matter.
"""
from __future__ import annotations

import os

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not ADMIN_DSN:
    pytest.skip("IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

#: `023` seeds the private-edition tenant and its membership, and `035`'s
#: rollback explicitly keeps it ("the membership 023 seeded is kept, because 023
#: owns it"). It is the one tenant that may still hold memberships here.
BASELINE_TENANT = "IRA Private"


async def test_no_suite_left_a_tenant_holding_memberships():
    """At most one tenant may hold membership data when this directory is done.

    Read on the admin connection: this is a question about what is in the
    table, not about what any particular principal can see, and RLS would hide
    exactly the rows being looked for.
    """
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        rows = await admin.fetch(
            "SELECT t.id, t.name, count(*) AS n "
            "FROM tenant_memberships m JOIN tenants t ON t.id = m.tenant_id "
            "GROUP BY 1, 2 ORDER BY 2")
    finally:
        await admin.close()

    surplus = [r for r in rows if r["name"] != BASELINE_TENANT]
    assert not surplus, (
        "these tenants still hold memberships after tests/tenancy/ finished:\n"
        + "\n".join(f"  {r['name']} ({r['id']}) — {r['n']} membership(s)"
                    for r in surplus)
        + "\n\nA fixture created them and did not remove them. That does not "
        "fail the suite that owns it — it fails the 'Rollback migration "
        "applies cleanly' step later in the same job, because "
        "rollback/035_memberships_down.sql refuses while more than one tenant "
        "holds membership data, and the error there names 035 rather than the "
        "fixture.\n\nDelete the memberships in the fixture's teardown, "
        "child-first: tenant_memberships, then users, then tenants."
    )


async def test_no_suite_left_a_private_account_behind():
    """No membership may still carry a private role when this directory is done.

    The same failure, one migration later. A granted private account is a
    membership in the private tenant, which the check above allows by design,
    so it cannot see one. `rollback/092_private_account_memberships_down.sql`
    refuses while any membership carries a `private_role`, and its error would
    name 092 rather than the suite that granted the account.
    """
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        rows = await admin.fetch(
            "SELECT u.external_subject AS name, m.private_role "
            "FROM tenant_memberships m JOIN users u ON u.id = m.user_id "
            "WHERE m.private_role IS NOT NULL ORDER BY 1")
    finally:
        await admin.close()

    assert not rows, (
        "these private accounts still exist after tests/tenancy/ finished:\n"
        + "\n".join(f"  {r['name']} ({r['private_role']})" for r in rows)
        + "\n\nA test granted them and did not remove them. Rolling 092 back "
        "refuses while any exists, so the 'Rollback migration applies cleanly' "
        "step would fail naming 092. Delete the account's users row in the "
        "teardown: the credential and the membership cascade with it."
    )
