"""Phase 9 — "delete everything about me", checked against the whole schema.

Phase 5 built erasure and tested that the tables it names are emptied. That is
the right test for the tables it names. It cannot answer the question a launch
gate has to ask, which is about the tables it does not name — because the way
this promise breaks is not a delete that fails loudly, it is a table nobody
listed. `RELEASE_READINESS.md` R10 cites this suite.

So the classification is exhaustive by construction: every tenant-scoped table
in the live schema must fall into exactly one of four sets, and the test fails
if any table falls into none of them.

  DELETED   named in `erase_postgres`'s `account` scope — rows go
  REDACTED  handled by `erase_audit`: content erased, the proof that an action
            happened kept, on the line ADR-0004 drew
  SHREDDED  handled by `erase_keys`: the KEY is destroyed, which makes the data
            it protected unopenable everywhere at once — including copies a
            DELETE could not reach. Blocker B3.
  EXEMPT    deliberately neither, each with the reason recorded below

The fifth outcome — a table in none of the four — is the bug, and it is what
this suite found.

── What it found, and how it was closed ─────────────────────────────────────

Six tenant-scoped tables held personal data that a user asking for deletion
would expect to be gone, and were in none of the four sets:

    agent_runs          metadata JSONB, unbounded
    retention_settings  the user's own privacy preferences
    tenant_memberships  which tenant the person belonged to, and as what
    tool_calls          purpose, result_summary — free text about what was read
    user_devices        device label ("Praveen's iPhone"), platform
    user_sessions       token hash, IP hash, user agent

Phase 9 pinned them in `KNOWN_UNERASED` and deliberately did **not** fix them,
because whether an audit-shaped table is deleted or redacted is precisely the
question ADR-0004 answered for the trust ledger — by deliberate decision, with a
documented consequence — and making the same call for six tables in a one-line
list edit, inside the phase whose job was to report honestly on deletion, would
have been the wrong way to decide it.

**[ADR-0008](../../../../docs/cloud-transformation/adr/ADR-0008-DELETING-THE-LAST-SIX-TABLES.md)
is that decision.** Four are deleted and two are redacted:

    DELETED   user_sessions, user_devices, retention_settings,
              tenant_memberships — the user's own records, with no
              accountability value to anybody
    REDACTED  agent_runs (metadata), tool_calls (purpose, result_summary) —
              the shape is the audit, the content is the personal data

`KNOWN_UNERASED` is now empty and stays empty. It recorded a debt; the debt is
paid, and the set was never a bin for new ones.
"""
from __future__ import annotations

import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")

pytestmark = pytest.mark.skipif(
    not DSN, reason="IRA_TEST_DATABASE_DSN not set — schema-wide erasure sweep skipped"
)


#: Tables `erase_audit` handles by REDACTING content while keeping the record
#: that an action happened. ADR-0004; not a gap.
REDACTED: frozenset[str] = frozenset({
    "trust_ledger",     # the hash-chained decision record
    "action_receipts",  # Phase 7 — evidence the agent cannot author
    # Blocker B2, ADR-0008. The two of the six unreached tables that ARE
    # audit-shaped. `agent_runs.metadata` is blanked; `tool_calls.purpose` and
    # `.result_summary` are blanked and `.decision` is kept, because "IRA denied
    # this" is a claim worth keeping and says nothing about what "this" was.
    "agent_runs",
    "tool_calls",
    # Phase 3B Batch 1C. `biometric_audit` cannot be deleted — 038's trigger
    # refuses DELETE outright — and it should not be: that a voiceprint match
    # was attempted, and how it scored, is security evidence. What is personal
    # is WHO it was about, so `erase_audit` clears `user_id` and the statistics
    # stay. The redaction is keyed on the subject rather than the tenant,
    # because every row written today carries a NULL tenant.
    "biometric_audit",
    # Batch 11. The row STAYS — it is the record of how somebody came to hold
    # access to an organisation, and the membership it produced keeps working
    # after an erasure, so destroying it removes the audit trail for access that
    # survives it. `rollback/055` refuses to drop the table for the same reason.
    #
    # What goes is the invitee's ADDRESS, which is the only column identifying
    # the person rather than the grant. `invited_by`, `role` and `accepted_at`
    # stay, so "who let this person in, to what, and when" is still answerable.
    #
    # This was a stated residual risk from Batch 5 onward rather than an
    # oversight: the exemption entry named redaction as the correct end state
    # and said erasure had no redaction path reaching here. It does now.
    "tenant_invitations",
    # Batch 11. The same shape `biometric_audit` has, for the same reason:
    # `061`'s trigger refuses DELETE outright, and it should — WHEN an account
    # signed in, whether the second factor failed, and whether somebody linked a
    # provider is the evidence an incident is reconstructed from. What is
    # personal is WHO it was about and FROM WHERE, so `erase_audit` calls
    # `auth_audit.redact_for`, which clears user_id, subject_hash and ip_hash
    # and blanks user_agent. The trigger permits exactly that mutation and
    # refuses every other, so the redaction cannot become a rewrite.
    "auth_events",
})

#: Tables `erase_keys` handles by DESTROYING the key rather than the row. Blocker
#: B3, and a stronger outcome than deletion, not a weaker one: a DELETE has to
#: find every copy, and destroying the key does not. The row survives as a
#: tombstone — that is deliberate, and is what lets a user be told *why* their
#: connected accounts stopped working rather than left to guess.
SHREDDED: dict[str, str] = {
    "tenant_data_keys":
        "the key registry itself. `erase_keys` destroys the material "
        "(wrapped_dek → NULL) and keeps the row: the tombstone is the only "
        "thing that can distinguish a deliberate destruction from an "
        "unexplained failure to decrypt. Deleting it would remove the "
        "explanation while changing nothing about the data.",
    "user_mfa_keys":
        "the per-user MFA envelope keys (P2-05). `erase_keys` NULLs "
        "wrapped_dek/wrap_nonce and sets destroyed_at, which makes every copy "
        "of the TOTP ciphertext permanently unopenable — including the ones in "
        "backups, replicas and WAL archives that a DELETE cannot reach. The "
        "row survives as the tombstone for the same reason tenant_data_keys' "
        "does: a deleted row cannot distinguish a deliberate destruction from "
        "a key that never existed.",
}

#: Tables deliberately neither deleted nor redacted, with the reason. Each entry
#: is a decision somebody made, not a table somebody missed.
EXEMPT: dict[str, str] = {
    "care_people":
        "IRA CARE (099): belongs to the PERSON CARED FOR, who is usually not an account holder. Erasing a family member's or carer's account must not take the person's record with it. Any user_id link in the row is ON DELETE SET NULL. The rows are removed by the care package's per-person erasure, and when the TENANT is deleted, by ON DELETE CASCADE (checked below).",
    "care_circle_members":
        "IRA CARE (099): belongs to the PERSON CARED FOR, who is usually not an account holder. Erasing a family member's or carer's account must not take the person's care circle with it. Any user_id link in the row is ON DELETE SET NULL. The rows are removed by the care package's per-person erasure, and when the TENANT is deleted, by ON DELETE CASCADE (checked below).",
    "care_devices":
        "IRA CARE (099): belongs to the PERSON CARED FOR, who is usually not an account holder. Erasing a family member's or carer's account must not take the person's watch record with it. Any user_id link in the row is ON DELETE SET NULL. The rows are removed by the care package's per-person erasure, and when the TENANT is deleted, by ON DELETE CASCADE (checked below).",
    "care_vitals":
        "IRA CARE (099): belongs to the PERSON CARED FOR, who is usually not an account holder. Erasing a family member's or carer's account must not take the person's health readings with it. Any user_id link in the row is ON DELETE SET NULL. The rows are removed by the care package's per-person erasure, and when the TENANT is deleted, by ON DELETE CASCADE (checked below).",
    "care_safety_settings":
        "IRA CARE (099): belongs to the PERSON CARED FOR, who is usually not an account holder. Erasing a family member's or carer's account must not take the person's safety settings with it. Any user_id link in the row is ON DELETE SET NULL. The rows are removed by the care package's per-person erasure, and when the TENANT is deleted, by ON DELETE CASCADE (checked below).",
    "care_checkins":
        "IRA CARE (099): belongs to the PERSON CARED FOR, who is usually not an account holder. Erasing a family member's or carer's account must not take the person's check-in history with it. Any user_id link in the row is ON DELETE SET NULL. The rows are removed by the care package's per-person erasure, and when the TENANT is deleted, by ON DELETE CASCADE (checked below).",
    "care_alerts":
        "IRA CARE (099): belongs to the PERSON CARED FOR, who is usually not an account holder. Erasing a family member's or carer's account must not take the person's alert history with it. Any user_id link in the row is ON DELETE SET NULL. The rows are removed by the care package's per-person erasure, and when the TENANT is deleted, by ON DELETE CASCADE (checked below).",
    "care_alert_attempts":
        "IRA CARE (099): belongs to the PERSON CARED FOR, who is usually not an account holder. Erasing a family member's or carer's account must not take the person's record of who was called about an alert with it. Any user_id link in the row is ON DELETE SET NULL. The rows are removed by the care package's per-person erasure, and when the TENANT is deleted, by ON DELETE CASCADE (checked below).",
    "care_acknowledgements":
        "IRA CARE (099): belongs to the PERSON CARED FOR, who is usually not an account holder. Erasing a family member's or carer's account must not take the person's record of who acknowledged an alert with it. Any user_id link in the row is ON DELETE SET NULL. The rows are removed by the care package's per-person erasure, and when the TENANT is deleted, by ON DELETE CASCADE (checked below).",
    "care_routines":
        "IRA CARE (099): belongs to the PERSON CARED FOR, who is usually not an account holder. Erasing a family member's or carer's account must not take the person's routines with it. Any user_id link in the row is ON DELETE SET NULL. The rows are removed by the care package's per-person erasure, and when the TENANT is deleted, by ON DELETE CASCADE (checked below).",
    "care_medications":
        "IRA CARE (099): belongs to the PERSON CARED FOR, who is usually not an account holder. Erasing a family member's or carer's account must not take the person's medication list with it. Any user_id link in the row is ON DELETE SET NULL. The rows are removed by the care package's per-person erasure, and when the TENANT is deleted, by ON DELETE CASCADE (checked below).",
    "care_medication_events":
        "IRA CARE (099): belongs to the PERSON CARED FOR, who is usually not an account holder. Erasing a family member's or carer's account must not take the person's medication history with it. Any user_id link in the row is ON DELETE SET NULL. The rows are removed by the care package's per-person erasure, and when the TENANT is deleted, by ON DELETE CASCADE (checked below).",
    "care_transcripts":
        "IRA CARE (099): belongs to the PERSON CARED FOR, who is usually not an account holder. Erasing a family member's or carer's account must not take the person's conversation transcripts with it. Any user_id link in the row is ON DELETE SET NULL. The rows are removed by the care package's per-person erasure, and when the TENANT is deleted, by ON DELETE CASCADE (checked below).",
    "care_consents":
        "IRA CARE (099): belongs to the PERSON CARED FOR, who is usually not an account holder. Erasing a family member's or carer's account must not take the person's recorded consents with it. Any user_id link in the row is ON DELETE SET NULL. The rows are removed by the care package's per-person erasure, and when the TENANT is deleted, by ON DELETE CASCADE (checked below).",
    "care_access_log":
        "IRA CARE (099): the append-only record of who looked at a person's health "
        "data or transcripts. A trigger refuses UPDATE and DELETE. Deleting it at "
        "account erasure would destroy the proof of who read what. Retained and "
        "redacted like the other audit tables; its tenant key is ON DELETE RESTRICT.",
    "auth_token_versions":
        "the account's token version (migration 096): a users.id and an "
        "integer, no name or address. Erasure raises it, so no token issued "
        "before the erasure works after it, and keeps the row seven days "
        "(the longest life of a credential IRA signs), after which the daily "
        "purge deletes it. Deleting it at erasure would reset it to 0 while a "
        "seven-day refresh token could still be alive.",
    "privacy_events":
        "the record that an erasure happened. Deleting it destroys the proof "
        "that the user's request was honoured.",
    "deletion_requests":
        "the deletion request itself. A deletion that erases the record of "
        "having been asked cannot be audited or appealed.",
    "export_requests":
        "same shape as deletion_requests — the record of a request the user "
        "made about their own data.",
    "tenant_subscriptions":
        "a TENANT-level commercial record, not personal data about the user "
        "asking to be erased. It carries no user_id at all: it says which plan "
        "an organisation is on, for which period, and whether it cancelled. "
        "Deleting it because one member left would cancel the whole "
        "organisation's billing — erasing a fact about a company in response "
        "to a request from a person. When the TENANT is deleted the rows go "
        "with it, by ON DELETE CASCADE.",
    "billing_checkout_sessions":
        "the record that somebody tried to pay, and a TENANT-level commercial "
        "record like tenant_subscriptions rather than personal data about the "
        "user asking to be erased. It carries no user_id: the tenant is written "
        "from the verified principal at creation precisely so a completed "
        "checkout can be attributed to the organisation that started it. "
        "Deleting a member's rows would also be impossible in the sense that "
        "matters — there is no member to key on. 087 grants the runtime no "
        "DELETE and gives the table no delete policy, deliberately; when the "
        "TENANT is deleted the rows go with it, by ON DELETE CASCADE.",
    "payment_events":
        "the record that a PROVIDER told us something, keyed on their opaque "
        "customer reference and carrying no tenant_id or user_id at all. It is "
        "not personal data "
        "about the person asking to be erased, and 080's trigger refuses to "
        "delete it at all: an event marked 'unattributed' is an open question — "
        "a payment arrived for somebody we could not identify — and erasing it "
        "is how that stops being anybody's problem while the money stays "
        "received. On tenant deletion the tenant_id is set to NULL and the "
        "arrival survives.",
    "invoices":
        "a bill issued to the TENANT, carrying no user_id. Erasing one because "
        "a member left would delete an organisation's financial record in "
        "response to a request from a person — and 079's trigger refuses to "
        "delete an issued invoice at all, because it is evidence the customer "
        "was told what they owe. The rows go when the TENANT does, by ON "
        "DELETE CASCADE.",
    "invoice_lines":
        "the lines that justify an invoice's total. Same shape and same "
        "reasoning as `invoices`, and they cannot outlive it: ON DELETE "
        "CASCADE from the invoice they belong to.",
    "tenant_entitlement_overrides":
        "same shape: a tenant-level exception to a plan's limits, keyed on "
        "tenant and an entitlement key, with no user_id. `granted_by` names "
        "the OPERATOR who granted the exception, not the data subject, and it "
        "is the audit trail for a limit somebody raised by hand.",
    "admin_access_grants":
        "ADR-0007. Break-glass authorisation, append-only. The rollback script "
        "refuses to run while any exists, for the same reason.",
    "admin_access_log":
        "ADR-0007. The only evidence an administrator read was authorised. An "
        "erasure that could empty it would remove the accountability it exists "
        "to provide.",
    "usage_quotas":
        "billing configuration, not content. Holds no personal data beyond the "
        "tenant reference; retention stated in DATA_RETENTION_SCHEDULE.md.",
    "model_usage":
        "the metering record. Structurally cannot hold a prompt (no JSONB, no "
        "BYTEA, every TEXT column bounded or enumerated — Phase 8), and is "
        "retained for accounting. Retention period in "
        "DATA_RETENTION_SCHEDULE.md.",
    "approval_requests":
        "governance audit — what was asked and what was decided, which is the "
        "user's own evidence in a dispute about whether IRA was authorised. "
        "ADR-0008 decided agent_runs and tool_calls by the same reasoning and "
        "reached redaction; these three carry no free-text content column to "
        "redact, so exemption and redaction come to the same thing here.",
    "approval_events":
        "as approval_requests — the state transitions of a decision record.",
    "permission_decisions":
        "as approval_requests — why a permission was granted or refused.",
    "client_agents":
        "008_business_data.sql. Business-relationship data for the Private "
        "edition's owner, not consumer account data; no cloud write path.",
    "investors":
        "008_business_data.sql — as client_agents.",
    # ── Phase 3B Batch 0/1 ───────────────────────────────────────────────────
    #
    # Three tenant-lifecycle tables. They are exempt from ACCOUNT erasure and
    # removed by TENANT deletion instead, which is a different operation with a
    # different trigger — `ON DELETE CASCADE` from `tenants` on all three,
    # asserted by `test_the_tenant_lifecycle_exemptions_really_cascade` below so
    # the reason is checked rather than asserted in prose.
    "organisations":
        "tenant-lifecycle configuration, not personal data: every column is "
        "name, slug, legal_name or country_code, and none of them describes a "
        "member. One person leaving must not delete the organisation the "
        "remaining members belong to. Removed with the tenant by CASCADE.",
    "workspaces":
        "as organisations — name, slug and status, no member data. Deleting a "
        "workspace on one member's erasure would destroy the shared context "
        "every other member of it still relies on. Removed with the tenant by "
        "CASCADE; the member's own link to it is `workspace_memberships`, "
        "which IS deleted.",
    "tenant_mfa_policy":
        "the tenant's security posture — whether MFA is required and the grace "
        "period. Holds no member's data and deliberately no secret: 036 keeps "
        "the requirement and the secret in separate tables so a tenant can "
        "mandate MFA without being able to read anyone's second factor. "
        "Erasing it on one member's request would silently lower the security "
        "of every other member. `updated_by` is ON DELETE SET NULL, so the "
        "erased person's id does not survive in it. Removed with the tenant by "
        "CASCADE.",
    "evidence_approvals":
        "F1 changed-evidence repair, design decision D6: workspace content, not "
        "personal data about the person asking to be erased. It is removed with "
        "its workspace or tenant by ON DELETE CASCADE. The acting person's user "
        "id (created_by, imported_by, signer, actor, granted_by) stays as an "
        "opaque reference with no name or email beside it; erasing it would "
        "make the remaining evidence history unattributable for everybody else "
        "in the workspace. Holds who approved which exact output version; "
        "erasing the signer would leave an approval nobody gave.",
    "evidence_artifact_access":
        "F1 changed-evidence repair, design decision D6: workspace content, not "
        "personal data about the person asking to be erased. It is removed with "
        "its workspace or tenant by ON DELETE CASCADE. The acting person's user "
        "id (created_by, imported_by, signer, actor, granted_by) stays as an "
        "opaque reference with no name or email beside it; erasing it would "
        "make the remaining evidence history unattributable for everybody else "
        "in the workspace. Holds per-artifact grants; a grant to the erased "
        "person is already ineffective once their membership is removed.",
    "evidence_artifact_versions":
        "F1 changed-evidence repair, design decision D6: workspace content, not "
        "personal data about the person asking to be erased. It is removed with "
        "its workspace or tenant by ON DELETE CASCADE. The acting person's user "
        "id (created_by, imported_by, signer, actor, granted_by) stays as an "
        "opaque reference with no name or email beside it; erasing it would "
        "make the remaining evidence history unattributable for everybody else "
        "in the workspace. Holds the exact bytes of each source version; the "
        "runtime role cannot UPDATE or DELETE it at all.",
    "evidence_artifacts":
        "F1 changed-evidence repair, design decision D6: workspace content, not "
        "personal data about the person asking to be erased. It is removed with "
        "its workspace or tenant by ON DELETE CASCADE. The acting person's user "
        "id (created_by, imported_by, signer, actor, granted_by) stays as an "
        "opaque reference with no name or email beside it; erasing it would "
        "make the remaining evidence history unattributable for everybody else "
        "in the workspace. Holds a document's title, collection and access "
        "mode.",
    "evidence_audit_events":
        "F1 changed-evidence repair, design decision D6: workspace content, not "
        "personal data about the person asking to be erased. It is removed with "
        "its workspace or tenant by ON DELETE CASCADE. The acting person's user "
        "id (created_by, imported_by, signer, actor, granted_by) stays as an "
        "opaque reference with no name or email beside it; erasing it would "
        "make the remaining evidence history unattributable for everybody else "
        "in the workspace. The workspace's hash-chained F1 audit log; deleting "
        "one row would break the chain for every later event.",
    "evidence_claims":
        "F1 changed-evidence repair, design decision D6: workspace content, not "
        "personal data about the person asking to be erased. It is removed with "
        "its workspace or tenant by ON DELETE CASCADE. The acting person's user "
        "id (created_by, imported_by, signer, actor, granted_by) stays as an "
        "opaque reference with no name or email beside it; erasing it would "
        "make the remaining evidence history unattributable for everybody else "
        "in the workspace. Holds values extracted from a version, which belong "
        "with that version.",
    "evidence_dependencies":
        "F1 changed-evidence repair, design decision D6: workspace content, not "
        "personal data about the person asking to be erased. It is removed with "
        "its workspace or tenant by ON DELETE CASCADE. The acting person's user "
        "id (created_by, imported_by, signer, actor, granted_by) stays as an "
        "opaque reference with no name or email beside it; erasing it would "
        "make the remaining evidence history unattributable for everybody else "
        "in the workspace. Holds links between claims, outputs and approvals.",
    "evidence_output_spans":
        "F1 changed-evidence repair, design decision D6: workspace content, not "
        "personal data about the person asking to be erased. It is removed with "
        "its workspace or tenant by ON DELETE CASCADE. The acting person's user "
        "id (created_by, imported_by, signer, actor, granted_by) stays as an "
        "opaque reference with no name or email beside it; erasing it would "
        "make the remaining evidence history unattributable for everybody else "
        "in the workspace. Holds which regions of a report were calculated from "
        "which claims.",
    "evidence_patches":
        "F1 changed-evidence repair, design decision D6: workspace content, not "
        "personal data about the person asking to be erased. It is removed with "
        "its workspace or tenant by ON DELETE CASCADE. The acting person's user "
        "id (created_by, imported_by, signer, actor, granted_by) stays as an "
        "opaque reference with no name or email beside it; erasing it would "
        "make the remaining evidence history unattributable for everybody else "
        "in the workspace. Holds draft repairs to a workspace's outputs.",
}

#: Tables holding personal data that erasure does not reach. **Empty**, and
#: `test_the_debt_list_is_empty` keeps it that way. See the module docstring.
KNOWN_UNERASED: frozenset[str] = frozenset()


@pytest.fixture
def db():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    yield conn
    conn.close()


def _tenant_scoped_tables(db) -> set[str]:
    cur = db.cursor()
    cur.execute(
        "SELECT c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r' AND EXISTS ("
        "  SELECT 1 FROM information_schema.columns col "
        "  WHERE col.table_schema = 'public' AND col.table_name = c.relname "
        "    AND col.column_name = 'tenant_id')"
    )
    return {row[0] for row in cur.fetchall()}


def _account_scope_tables() -> set[str]:
    """The tables `erase_postgres` deletes under the `account` scope.

    Read out of the module rather than duplicated here, so this test tracks the
    real scope instead of a copy of it that could drift into agreement with
    nothing.
    """
    import ast
    import inspect

    from core.privacy import erasure

    # Parsed out of the SOURCE rather than imported, deliberately. Importing
    # `erasure.SUBJECT_TABLES` would read whatever the module happens to hold at
    # runtime, and this sweep exists to check the declaration — a conftest or a
    # fixture that mutated the dict would make the whole suite agree with itself
    # and with nothing else.
    #
    # P12-05 moved this to module scope so `core/privacy/export.py` could share
    # one definition; before that it was a local inside `erase_postgres` and
    # this parser walked that function instead.
    source = inspect.getsource(erasure)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        target_names = []
        if isinstance(node, ast.Assign):
            target_names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_names = [node.target.id]
        if "SUBJECT_TABLES" not in target_names or node.value is None:
            continue
        mapping = ast.literal_eval(node.value)
        return set(mapping["account"])
    raise AssertionError(
        "could not find `SUBJECT_TABLES` in core.privacy.erasure. The sweep "
        "reads the real erasure scope out of the source; if that changed shape, "
        "this test must be updated rather than allowed to pass on a stale copy."
    )


def test_the_account_scope_was_actually_parsed():
    """A parse that silently returned nothing would make the sweep vacuous."""
    scope = _account_scope_tables()
    assert len(scope) >= 10, (
        f"parsed only {len(scope)} tables from the account erasure scope "
        f"({sorted(scope)}); the real scope is larger, so the parser is wrong"
    )
    assert "messages" in scope and "conversations" in scope


def test_every_tenant_scoped_table_is_classified(db):
    """The sweep. A table in none of the four sets is unaccounted for.

    This is the assertion that catches the next table someone adds without
    thinking about deletion — which is the only way this promise realistically
    breaks.
    """
    scoped = _tenant_scoped_tables(db)
    assert scoped, "no tenant-scoped tables found — the sweep is not working"

    classified = (
        _account_scope_tables() | REDACTED | set(SHREDDED) | set(EXEMPT)
        | KNOWN_UNERASED
    )
    unaccounted = scoped - classified
    assert not unaccounted, (
        f"tenant-scoped table(s) that erasure neither deletes, redacts, "
        f"shreds, nor deliberately exempts: {sorted(unaccounted)}.\n\n"
        "A user asking IRA to delete everything about them would leave these "
        "rows behind, and nothing in the codebase records a decision to keep "
        "them. Add the table to the `account` scope in "
        "core/privacy/erasure.py, or to EXEMPT here with the reason it is "
        "kept. KNOWN_UNERASED is not the place for a new table — it records a "
        "debt this phase found, not a bin for future ones."
    )


def test_the_debt_list_is_empty(db):
    """**Blocker B2, closed and held closed.**

    `KNOWN_UNERASED` recorded a debt Phase 9 found. ADR-0008 paid it — four
    tables deleted, two redacted — so the list is empty and this asserts it
    stays that way. A future table that erasure does not reach must be
    classified in `_account_scope_tables`, `REDACTED`, `SHREDDED` or `EXEMPT`,
    not re-opened here.
    """
    assert KNOWN_UNERASED == frozenset(), (
        f"KNOWN_UNERASED is no longer empty: {sorted(KNOWN_UNERASED)}. B2 was "
        f"closed by deciding what happens to each table (ADR-0008), not by "
        f"listing them. A new unreached table needs the same decision, not an "
        f"entry here."
    )


#: Exemptions justified by "removed with the tenant instead". The claim is
#: checkable, so it is checked — an exemption whose stated reason is false is
#: worse than no exemption, because it reads as a decision somebody verified.
TENANT_LIFECYCLE_EXEMPT: frozenset[str] = frozenset({
    "organisations", "workspaces", "tenant_mfa_policy",
    # IRA CARE (099): exempt because the rows belong to the person cared for and
    # go with the tenant. That reason is only true if the key cascades.
    "care_people", "care_circle_members", "care_devices", "care_vitals",
    "care_safety_settings", "care_checkins", "care_alerts", "care_alert_attempts",
    "care_acknowledgements", "care_routines", "care_medications",
    "care_medication_events", "care_transcripts", "care_consents",
})


def test_the_tenant_lifecycle_exemptions_really_cascade(db):
    """Three tables are exempt from account erasure on the grounds that tenant
    deletion removes them. If the foreign key were `ON DELETE SET NULL` or
    `NO ACTION`, that reason would be false and the rows would outlive both the
    account erasure that skipped them and the tenant deletion that was supposed
    to catch them."""
    cur = db.cursor()
    cur.execute(
        "SELECT conrelid::regclass::text, confdeltype FROM pg_constraint "
        "WHERE confrelid = 'tenants'::regclass AND contype = 'f' "
        "  AND conrelid::regclass::text = ANY(%s)",
        (sorted(TENANT_LIFECYCLE_EXEMPT),),
    )
    actions = dict(cur.fetchall())

    missing = sorted(TENANT_LIFECYCLE_EXEMPT - set(actions))
    assert not missing, (
        f"{missing} has no foreign key to `tenants`, so 'removed with the "
        f"tenant' is not true of it. Either give it the key or give the "
        f"exemption a different, true reason."
    )
    # 'c' is CASCADE in pg_constraint.confdeltype.
    not_cascading = sorted(t for t, action in actions.items() if action != "c")
    assert not not_cascading, (
        f"{not_cascading} reference `tenants` but do NOT cascade on delete "
        f"(confdeltype != 'c'). Their exemption from account erasure rests on "
        f"tenant deletion reaching them, and it does not."
    )


def test_exempt_tables_all_carry_a_reason():
    """An exemption without a reason is an omission with better paperwork."""
    thin = [t for t, why in EXEMPT.items() if len(why.strip()) < 40]
    assert not thin, (
        f"exemption(s) with no substantive reason: {thin}. State why the data is "
        "kept, in terms a user could be shown."
    )


def test_shredded_tables_all_carry_a_reason():
    """Same standard. "The key is destroyed instead" is a stronger claim than
    deletion, so it needs its reasoning written down, not assumed."""
    thin = [t for t, why in SHREDDED.items() if len(why.strip()) < 40]
    assert not thin, f"shredded table(s) with no substantive reason: {thin}"


def test_the_shredded_tables_are_reached_by_a_real_eraser():
    """SHREDDED is a claim about `erase_keys`, so check `erase_keys` exists and
    is wired into the standard set. Listing a table here while nothing destroyed
    its key would be the same failure this file exists to catch, one level up."""
    from core.privacy.erasure import KNOWN_STORES, default_erasers, erase_keys

    assert callable(erase_keys)
    assert "keys" in KNOWN_STORES, (
        "the key store is not in KNOWN_STORES, so a deletion that never "
        "destroyed a key would still report every store reached"
    )

    import uuid

    from core.editions import EDITION_CLOUD
    from core.identity import Principal

    principal = Principal(
        user_id=uuid.uuid4(), tenant_id=uuid.uuid4(),
        session_id=uuid.uuid4(), edition=EDITION_CLOUD,
    )
    assert "keys" in default_erasers(principal, scope="account")

    # Each SHREDDED table names key material, and `erase_keys` is the claim
    # that something destroys it. Checking only that the function exists let
    # `user_mfa_keys` be added to the dict above while nothing touched it —
    # this file's own failure mode, one level up.
    #
    # The check walks the AST for a CALL rather than searching the text, and
    # that distinction is not pedantry: the first version of this guard used
    # `name in source`, and it passed with the shred deleted, because the
    # `from security.mfa_crypto import destroy_mfa_keys` line was still there
    # to satisfy the substring. A guard that matches an import matches the
    # absence of the thing it is guarding.
    import ast
    import inspect
    import textwrap

    from core.privacy import erasure as _erasure

    tree = ast.parse(textwrap.dedent(inspect.getsource(_erasure.erase_keys)))
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                called.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                called.add(fn.attr)

    destroyers = {
        "tenant_data_keys": "destroy_keys",
        "user_mfa_keys": "destroy_mfa_keys",
    }
    for table in sorted(SHREDDED):
        call = destroyers.get(table)
        assert call, (
            f"{table} is listed as SHREDDED but no destroyer is registered for "
            f"it here. Name the function in `destroyers` so this check is real.")
        assert call in called, (
            f"{table} is listed as SHREDDED, but `erase_keys` never CALLS "
            f"{call}() — it calls {sorted(called)}. The entry above claims a "
            f"destruction that does not happen: a user told their second factor "
            f"was destroyed would still have openable ciphertext.")


def test_shredding_is_scoped_to_full_account_erasure_only():
    """A user clearing their conversations has not asked to lose their
    connected accounts. Destroying the key under a narrower scope would do
    exactly that, irreversibly."""
    import asyncio
    import uuid

    from core.editions import EDITION_CLOUD
    from core.identity import Principal
    from core.privacy.erasure import erase_keys

    principal = Principal(
        user_id=uuid.uuid4(), tenant_id=uuid.uuid4(),
        session_id=uuid.uuid4(), edition=EDITION_CLOUD,
    )
    for scope in ("memories", "conversations", "voice", "everything"):
        result = asyncio.run(erase_keys(principal, scope=scope))
        assert result.ok and result.deleted == 0, (
            f"scope {scope!r} destroyed encryption keys. Only 'account' may."
        )


def test_no_table_is_in_two_places_at_once(db):
    """Deleted AND exempt is a contradiction, and whichever the code does, the
    other entry is a false statement about the system."""
    account = _account_scope_tables()
    overlaps = {
        "deleted and exempt": sorted(account & set(EXEMPT)),
        "deleted and redacted": sorted(account & REDACTED),
        "deleted and shredded": sorted(account & set(SHREDDED)),
        "deleted and listed as unerased": sorted(account & KNOWN_UNERASED),
        "redacted and exempt": sorted(REDACTED & set(EXEMPT)),
        "shredded and exempt": sorted(set(SHREDDED) & set(EXEMPT)),
        "shredded and redacted": sorted(set(SHREDDED) & REDACTED),
    }
    conflicts = {k: v for k, v in overlaps.items() if v}
    assert not conflicts, f"contradictory classifications: {conflicts}"


def test_the_object_store_is_reported_as_unreached_not_done():
    """Phase 5 chose to report the missing object store honestly. Pinned here
    because a launch gate is exactly when someone is tempted to make the report
    all-green by turning an unreached store into a successful no-op."""
    import asyncio
    import uuid

    from core.editions import EDITION_CLOUD
    from core.identity import Principal
    from core.privacy.erasure import erase_object_storage

    principal = Principal(
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        edition=EDITION_CLOUD,
    )
    result = asyncio.run(erase_object_storage(principal, scope="account"))
    assert result.ok is False, (
        "erase_object_storage reported success for a store that does not exist. "
        "An unreached store must be reported as unreached: the alternative is a "
        "deletion report that is wrong the moment a backend is configured."
    )
    assert result.error, "an unreached store must say why"
