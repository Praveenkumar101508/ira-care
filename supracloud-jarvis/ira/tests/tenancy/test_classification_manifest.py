"""The classification manifest is the specification. These tests enforce it.

`docs/cloud/evidence/DATABASE_TENANCY_CLASSIFICATION.json` says what every
table's ownership boundary must become. A specification nobody checks drifts
from the schema within a phase or two, and the drift is invisible until a
migration is written against a stale document.

So this suite asserts two things:

  1. **The manifest is internally consistent** — every table classified, no
     `UNRESOLVED`, every authentication and biometric table `USER_PRIVATE`,
     every content table scoped, every global table justified, every table with
     a deletion rule. These run without a database.

  2. **The manifest matches the live schema** — same table set, same current
     state. This needs `IRA_TEST_DATABASE_DSN` and skips without it, like the
     rest of `tests/tenancy/`.

The test that matters most is `test_a_new_table_must_be_classified`: adding a
table without classifying it fails CI. That is what stops Phase 3B from
migrating a schema the manifest no longer describes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

# tests/tenancy/ -> tests/ -> ira/ -> supracloud-jarvis/ -> repo root
REPO_ROOT = Path(__file__).resolve().parents[4]
MANIFEST_PATH = REPO_ROOT / "docs/cloud/evidence/DATABASE_TENANCY_CLASSIFICATION.json"

VALID_CLASSIFICATIONS = frozenset({
    "PLATFORM_GLOBAL", "IDENTITY_GLOBAL", "TENANT_SCOPED", "WORKSPACE_SCOPED",
    "USER_PRIVATE", "APPEND_ONLY_AUDIT", "EPHEMERAL_OPERATIONAL", "REFERENCE_DATA",
})

#: Classifications that mean "this row belongs to somebody".
SCOPED = frozenset({
    "TENANT_SCOPED", "WORKSPACE_SCOPED", "USER_PRIVATE",
    "APPEND_ONLY_AUDIT", "EPHEMERAL_OPERATIONAL",
})

GLOBAL = frozenset({"PLATFORM_GLOBAL", "IDENTITY_GLOBAL", "REFERENCE_DATA"})

#: The live schema after Phase 3B Batch 0-3. A deliberate change updates this
#: constant *and* the manifest in the same commit — which is the point: the
#: number moving without the manifest moving is the regression.
#:
#: 77 at Phase 3A. Batch 0/1 added five: organisations, workspaces,
#: workspace_memberships, tenant_mfa_policy and legacy_identity_map. Batch 3
#: added one: workspace_role_capabilities. Phase 3B-3.5 added one:
#: schema_migrations, which becomes a real table once the migration command
#: has run — it is no longer created by the application on boot. P2-05 added
#: one: user_mfa_keys, the per-user envelope keys that encrypt TOTP seeds.
#: Phase 2 batch 2 added two: user_credentials and email_verification_tokens.
#: Batch 5 added one: tenant_invitations. Batch 7 added one:
#: mfa_recovery_codes. Batch 9 added one: password_reset_tokens. Batch 10
#: added one: external_identities. Batch 11 added one: auth_events. P4-13 added
#: one: job_runs. P5-01 added two: model_registry and model_pull_allowlist.
#: P6-01 added one: model_licence_register, which holds the licence EVIDENCE
#: MOD-01 asked for and the DETERMINATION only a named human may record. P7-01
#: added two: training_datasets and dataset_checks, which make the dataset
#: lifecycle enforced state rather than a directory layout. P7-03 added two
#: more: model_training_runs and training_run_datasets, which make training
#: itself refuse without licence permission, governed data and owner approval.
#: P10-04 added one: trust_ledger_chain_eras, which records where THIS
#: deployment's Trust Ledger stopped being one global chain and became one per
#: tenant — the boundary an externally-held anchor has to be judged against.
#: The F1 revocation proposal (091) added one: f1_token_revocations, the
#: durable deny list for revoked cloud access tokens, keyed by SHA-256 of jti.
#: ADR-0010 (095) added one: attribution_backfill_095, the record of which
#: Group B rows its single-user backfill attributed, so its rollback is exact.
#: Durable logout (096) added one: auth_token_versions, the per-account token
#: version, moved from Redis so a lost key cannot revive revoked tokens.
#: LangGraph's checkpoint tables (097) added four: checkpoints, checkpoint_blobs,
#: checkpoint_writes and checkpoint_migrations, which the runtime role may not
#: create itself, so conversation-graph state no longer falls back to memory.
EXPECTED_TABLE_COUNT = 160  # +5 P11-01 (078), +2 P11-03 (079), +1 P11-04 (080), +1 B2 (087), +9 F1 (089), +1 F1 revocation (091), +1 ADR-0010 (095), +1 durable logout (096), +4 LangGraph checkpoints (097), +15 IRA CARE data model (099), +1 IRA CARE reports (100), +1 IRA CARE conversation memory (104), +1 IRA CARE device events (106), +2 IRA CARE push tokens and tickets (107), +2 IRA CARE visits and visit tasks (109), +2 IRA CARE communication profiles and reminders (110), +1 IRA CARE user preferences (111), +1 IRA CARE medication corrections (112), +2 IRA CARE alert dispatch and attempt events (113), +1 IRA CARE care plans (116), +1 IRA CARE staff designations (117), +1 IRA CARE OS releases (120), +3 deployment administrators and device instructions (122)

#: Tables whose contents must never leave the database — key material. They
#: still declare correctly-scoped cache and storage patterns so the SHAPE is
#: reviewable, and `test_key_material_declares_itself_uncacheable` is what
#: makes the patterns non-permission.
#: USER_PRIVATE tables that carry NO tenant column, because they exist before
#: any tenant does. `totp_secrets` and `user_mfa_keys` are USER_PRIVATE *and*
#: tenant-scoped — a second factor belongs to a member of a tenant. These two
#: belong to an account that has not been provisioned yet: a registered user
#: holds no membership until provisioning, so a tenant predicate on their
#: credential would make it unreadable at exactly the moment authentication
#: needs it.
#:
#: Pinned, because "this table needs no tenant" is the sentence that precedes
#: most cross-tenant defects. Adding an entry is a claim that the row cannot
#: belong to a tenant at all — not that giving it one would be inconvenient.
PRE_TENANT_TABLES = frozenset({
    "user_credentials", "email_verification_tokens",
    # Batch 9. A password reset happens BEFORE any tenant is selected —
    # the caller cannot sign in, so there is nothing to select with. A
    # tenant predicate would make the row unreadable to the only party
    # entitled to use it.
    "password_reset_tokens",
})

#: USER_PRIVATE tables whose policy keys on POSSESSION of a per-row secret
#: rather than on `app.user_id`, because their legitimate reader is
#: unauthenticated and cannot supply a user id.
#:
#: Two entries, and each had to earn it. `email_verification_tokens` is read by
#: an unauthenticated caller presenting a token, so the user is unknown until
#: the lookup has happened; `password_reset_tokens` is read by a caller who
#: cannot sign in at all, which is the reason they are using it.
#:
#: The bar is not "a user predicate would be inconvenient" — it is that the
#: only legitimate reader CANNOT supply one, so demanding it would make the row
#: unreadable to exactly the party entitled to use it. The resulting policy is STRONGER than
#: the user predicate it replaces: a connection sees exactly the row whose
#: secret it already holds. `tests/readiness/test_cross_tenant_sweep.py`
#: recognises the same mechanism, and the test below requires the two to agree.
POSSESSION_SCOPED_TABLES = frozenset({
    "email_verification_tokens",
    # Batch 9, and the second entry this set was meant to make hard to
    # add. It earns it the same way: the legitimate reader of a reset
    # token is unauthenticated BY DEFINITION — being unable to sign in is
    # the reason they are here — so `app.user_id` cannot be supplied, and
    # possession of the token is a strictly stronger predicate than the
    # user id that is unavailable.
    "password_reset_tokens",
})

UNCACHEABLE_TABLES = frozenset({
    # Durable logout (096). Not key material: a users.id and an integer. Listed
    # because a cached version is the restored-snapshot gap 096 closed, and a
    # stale copy revives every token issued before the last sign-out.
    "auth_token_versions",
    # F1 revocation proposal (091). Not key material: SHA-256 of a token
    # identifier, which authenticates nothing. Listed because a cached
    # "not revoked" answer is a revoked token that keeps working, and there is
    # no reason for a deny record to leave the database.
    "f1_token_revocations",
    # P4-11. Not key material — counts and identifiers only. It is here because
    # the SECOND half of this test pins the set in both directions, and a table
    # that declares `cacheable=false` without being listed would otherwise look
    # like key material that nobody declared. A copy of this outside the
    # database is a map of which tenants exist and how much each is deleting:
    # worth nothing to their users, something to a competitor.
    "job_runs",
    "user_mfa_keys",
    # Phase 2 batch 2. A password hash outside the database is an offline
    # cracking target; a live verification token is a bearer credential for an
    # email address. Neither belongs in a cache or an export.
    "user_credentials",
    "email_verification_tokens",
    # Batch 5. An invitation is read by its 256-bit token on a connection bound
    # to nothing else, and by the tenant that issued it. Caching either read
    # would mean a revoked invitation kept working.
    "tenant_invitations",
    # Batch 7. A recovery code is a second factor; caching one would mean a
    # spent or superseded code kept working.
    "mfa_recovery_codes",
    # Batch 9. A live reset token sets the password on an account —
    # strictly stronger than a verification token, which only proves an
    # address.
    "password_reset_tokens",
    # Batch 10, and the odd one out: this table holds NO secret. The provider
    # authenticates and the row records who the result maps to. It is here
    # because a cached mapping from (issuer, subject) to a user IS a cached
    # authentication decision — and a stale one, after an unlink, means a
    # provider the person removed still signs them in.
    "external_identities",
    # Batch 11, and the second entry here that holds no secret. What a copy of
    # an authentication log is, is a movement pattern: when an account signs in,
    # from how many distinct places, whether the second factor ever failed.
    # `subject_hash` and `ip_hash` are HMACs and so are not addresses — but they
    # are STABLE identifiers, and an exported list of them keeps answering "the
    # same place, again" long after `redact_for` has cleared the live row. A
    # redaction that only reaches the database is not a redaction.
    "auth_events",
})


@pytest.fixture(scope="module")
def manifest() -> dict:
    assert MANIFEST_PATH.is_file(), f"missing manifest at {MANIFEST_PATH}"
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def tables(manifest) -> dict:
    return manifest["tables"]


# ── Completeness ─────────────────────────────────────────────────────────────

def test_the_manifest_accounts_for_every_live_table(manifest, tables):
    """A count change is either a deliberate schema change (update this constant
    and the manifest together) or a table nobody classified."""
    assert manifest["table_count"] == len(tables)
    assert len(tables) == EXPECTED_TABLE_COUNT, (
        f"manifest describes {len(tables)} tables, expected {EXPECTED_TABLE_COUNT}. "
        "If the schema changed deliberately, update EXPECTED_TABLE_COUNT and the "
        "manifest in the same commit."
    )


def test_every_table_has_exactly_one_valid_classification(tables):
    for name, spec in tables.items():
        cls = spec.get("classification")
        assert cls, f"{name} has no classification"
        assert isinstance(cls, str), f"{name} has a non-string classification"
        assert cls in VALID_CLASSIFICATIONS, f"{name}: {cls!r} is not a valid classification"


def test_scopes_compose_rather_than_replace_the_classification(tables):
    """A table may hold more than one scoping property at once (P2-05).

    `totp_secrets` is the first: a second-factor seed is USER_PRIVATE — one
    person's, and a tenant administrator must never read it — and it is also
    TENANT_SCOPED, because the row belongs to a tenant for erasure and export.

    The optional `scopes` list carries both. It does not replace
    `classification`, and the assertion below is that it cannot: dropping
    USER_PRIVATE from a table that has it would leave `TENANT_SCOPED` alone,
    which reads as "tenant administrators may read this" and is the exact
    misreading that turns into an OR-combined RLS policy.
    """
    for name, spec in tables.items():
        scopes = spec.get("scopes")
        if scopes is None:
            continue
        assert isinstance(scopes, list) and scopes, f"{name}: `scopes` must be a non-empty list"
        for s in scopes:
            assert s in VALID_CLASSIFICATIONS, f"{name}: {s!r} in `scopes` is not valid"
        assert spec["classification"] in scopes, (
            f"{name}: `scopes` {scopes} does not include the primary "
            f"classification {spec['classification']!r}. `scopes` composes with "
            f"the classification; it never contradicts it.")
        assert len(set(scopes)) == len(scopes), f"{name}: duplicate entries in `scopes`"


def test_a_user_private_table_never_loses_that_property(tables):
    """USER_PRIVATE is the one property that must not be traded away.

    Adding a tenant to a credential table is a strengthening. Replacing
    USER_PRIVATE *with* TENANT_SCOPED would be the opposite — it would say
    every member of the tenant may read it.
    """
    for name in ("totp_secrets", "voice_profiles"):
        spec = tables.get(name)
        if spec is None:
            continue
        scopes = spec.get("scopes") or [spec["classification"]]
        assert "USER_PRIVATE" in scopes, (
            f"{name} is no longer USER_PRIVATE. That is a credential/biometric "
            f"table; a tenant administrator must not be able to read it.")


def test_no_table_is_unresolved(tables):
    """`UNRESOLVED` is prohibited in the Phase 3A output."""
    unresolved = [n for n, s in tables.items() if s["classification"] == "UNRESOLVED"]
    assert not unresolved, f"UNRESOLVED tables remain: {unresolved}"


def test_every_table_has_a_written_justification(tables):
    for name, spec in tables.items():
        why = (spec.get("justification") or "").strip()
        assert len(why) > 20, f"{name} has no meaningful justification"


# ── Sensitive-data rules ─────────────────────────────────────────────────────

def test_every_authentication_secret_table_is_user_private(tables):
    """A tenant admin must not be able to read another person's second factor."""
    offenders = [
        n for n, s in tables.items()
        if s["data_kind"]["authentication_data"]
        and s["encryption"]["required"]
        and s["classification"] != "USER_PRIVATE"
    ]
    assert not offenders, f"authentication-secret tables not USER_PRIVATE: {offenders}"


def _owned_by_immutable_user_id(spec) -> bool:
    """True when the table is keyed on a user_id — already, or by plan.

    Written to hold in BOTH states on purpose. Before Batch 1A the invariant is
    a planned target column; after it, the column exists and the plan is empty.
    A test that only understood one of those would fail the moment the work it
    demanded was done, which teaches the next reader to delete it.
    """
    return (spec["current"]["has_user_id"]
            or spec["migration"]["requires_user_ownership_conversion"])


def test_totp_secrets_is_user_private_and_encrypted(tables):
    """The specific finding that made this a rule (R-26): keyed on a username
    string, so two tenants both holding 'alice' collide on the second factor."""
    spec = tables["totp_secrets"]
    assert spec["classification"] == "USER_PRIVATE"
    assert spec["encryption"]["required"]
    assert _owned_by_immutable_user_id(spec), \
        "totp_secrets must be keyed on an immutable user_id, not a username"


def test_every_biometric_table_is_user_private_or_audit(tables):
    for name, spec in tables.items():
        if spec["data_kind"]["biometric_data"]:
            assert spec["classification"] in ("USER_PRIVATE", "APPEND_ONLY_AUDIT"), \
                f"{name} holds biometric data but is {spec['classification']}"


def test_voice_profiles_is_user_private_and_encrypted(tables):
    spec = tables["voice_profiles"]
    assert spec["classification"] == "USER_PRIVATE"
    assert spec["encryption"]["required"], "voiceprints must be encrypted at rest"
    assert _owned_by_immutable_user_id(spec)
    assert "HARD DELETE" in spec["deletion"]["user_deletion"], \
        "a voiceprint must be hard-deleted on user deletion, never merely redacted"


def test_no_user_private_table_stays_keyed_only_by_username(tables):
    """The rule the owner generalised from totp_secrets: a username string is
    never a security principal."""
    for name, spec in tables.items():
        if spec["classification"] != "USER_PRIVATE":
            continue
        if not spec["migration"]["requires_user_ownership_conversion"]:
            continue
        target = spec["migration"]["target_columns"]
        if not target:
            # Already migrated — the column exists, which is the stronger form
            # of the same guarantee.
            assert spec["current"]["has_user_id"], f"{name} lost its user_id"
            continue
        assert "user_id" in target, f"{name} must gain an immutable user_id"
        assert target["user_id"]["type"] == "UUID", f"{name}.user_id must be a UUID"
        assert target["user_id"]["fk"] == "users(id)"


# ── Scoping rules ────────────────────────────────────────────────────────────

def test_every_customer_data_table_has_a_scope(tables):
    """A table holding customer data must belong to somebody."""
    for name, spec in tables.items():
        if not spec["data_kind"]["customer_data"]:
            continue
        cls = spec["classification"]
        if cls == "IDENTITY_GLOBAL":
            continue  # users: global by decision, membership-mediated
        assert cls in SCOPED, f"{name} holds customer data but is {cls}"


def test_every_scoped_table_ends_up_with_a_tenant_column(tables):
    """Either it has tenant_id now, or the migration adds it."""
    for name, spec in tables.items():
        if spec["classification"] not in SCOPED or name in PRE_TENANT_TABLES:
            continue
        has_now = spec["current"]["has_tenant_id"]
        gets_it = spec["migration"]["requires_tenant_id"]
        assert has_now or gets_it, f"{name} is {spec['classification']} with no tenant column, now or planned"


def test_every_workspace_scoped_table_ends_up_with_a_workspace_column(tables):
    for name, spec in tables.items():
        if spec["classification"] != "WORKSPACE_SCOPED":
            continue
        assert spec["current"]["has_workspace_id"] or spec["migration"]["requires_workspace_id"], \
            f"{name} is WORKSPACE_SCOPED with no workspace column, now or planned"


def test_global_tables_are_not_given_tenant_columns(tables):
    """Adding tenant_id to reference data is how a catalogue becomes 77 copies."""
    for name, spec in tables.items():
        if spec["classification"] in GLOBAL:
            assert not spec["migration"]["requires_tenant_id"], \
                f"{name} is {spec['classification']} and must not gain tenant_id"
            assert not spec["migration"]["requires_workspace_id"], \
                f"{name} is {spec['classification']} and must not gain workspace_id"


def test_users_stays_global(tables):
    """Owner decision CLOUD-ADR-0006: one person, many tenants."""
    spec = tables["users"]
    assert spec["classification"] == "IDENTITY_GLOBAL"
    assert not spec["current"]["has_tenant_id"]
    assert not spec["migration"]["requires_tenant_id"]
    assert "MUST NOT delete a user who belongs to another tenant" in spec["deletion"]["tenant_deletion"]


def test_billing_records_are_tenant_scoped_not_workspace_scoped(tables):
    """A workspace-scoped invoice would make billing depend on which workspace
    produced it."""
    for name in ("usage_quotas", "model_usage"):
        spec = tables[name]
        assert spec["classification"] in ("TENANT_SCOPED", "APPEND_ONLY_AUDIT")
        assert not spec["migration"]["requires_workspace_id"], \
            f"{name} is a commercial record and must stay tenant-scoped"


# ── RLS, encryption, deletion ────────────────────────────────────────────────

def test_every_scoped_table_has_an_rls_specification(tables):
    for name, spec in tables.items():
        if spec["classification"] not in SCOPED:
            continue
        rls = spec["rls"]
        assert rls["required"], f"{name} is {spec['classification']} but RLS is not required"
        assert rls["force"], f"{name} must FORCE row level security"
        for op in ("select", "insert", "update", "delete"):
            assert rls.get(op), f"{name} has no {op.upper()} policy specification"
        assert rls["application_role_bypassrls"] is False, \
            f"{name}: the application role must never hold BYPASSRLS"


def test_global_tables_state_what_protects_them(tables):
    """A global table is not tenant-scoped, so it needs a stated protection —
    but "global" no longer implies "no row-level security".

    This test originally asserted `rls.required is False` for every global
    table, on the reasoning that they are protected by role grants. Phase 3B
    found that reasoning unsound: the documented provisioning step ends with
    `GRANT ALL ON ALL TABLES IN SCHEMA public TO ira_app`, so a grant-based
    protection written in a migration is handed straight back (R-47). Two global
    tables now use RLS for something other than isolation —
    `legacy_identity_map` as a deny-all, `workspace_role_capabilities` as a
    write lock — and the old assertion would have refused both.

    So the requirement is a stated reason either way, not a particular answer.
    """
    for name, spec in tables.items():
        if spec["classification"] not in GLOBAL:
            continue
        reason = (spec["rls"].get("reason") or "").strip()
        assert len(reason) > 20, (
            f"{name} is {spec['classification']} and does not say what protects it. "
            f"State either why RLS is not required, or what its RLS is for.")
        if spec["rls"]["required"]:
            assert (spec["rls"].get("policy") or "").strip(), (
                f"{name} requires RLS but names no policy. A global table with RLS "
                f"is doing something other than tenant isolation, and what it is "
                f"doing has to be written down.")


def test_user_private_policies_require_a_user_predicate(tables):
    for name, spec in tables.items():
        if (spec["classification"] != "USER_PRIVATE"
                or name in POSSESSION_SCOPED_TABLES):
            continue
        assert "app.user_id" in spec["rls"]["session_context"], \
            f"{name} is USER_PRIVATE but its policy does not require app.user_id"
        assert "user_id = current_setting" in spec["rls"]["select"], \
            f"{name}: a tenant predicate alone lets a tenant admin read another user's row"


def test_workspace_policies_require_a_workspace_predicate(tables):
    for name, spec in tables.items():
        if spec["classification"] != "WORKSPACE_SCOPED":
            continue
        assert "app.workspace_id" in spec["rls"]["session_context"], f"{name}"
        assert "workspace_id = current_setting" in spec["rls"]["select"], f"{name}"


def test_append_only_tables_refuse_delete(tables):
    for name, spec in tables.items():
        if spec["classification"] == "APPEND_ONLY_AUDIT":
            assert "DENIED" in spec["rls"]["delete"], \
                f"{name} is APPEND_ONLY_AUDIT but its DELETE policy is not a denial"


def test_every_table_specifies_encryption(tables):
    for name, spec in tables.items():
        assert "required" in spec["encryption"]
        assert (spec["encryption"].get("specification") or "").strip(), \
            f"{name} has no encryption specification"


def test_every_table_has_deletion_and_retention_behaviour(tables):
    required = {"user_deletion", "tenant_deletion", "workspace_deletion",
                "membership_removal", "legal_hold", "hard_delete"}
    for name, spec in tables.items():
        missing = required - set(spec["deletion"])
        assert not missing, f"{name} is missing deletion rules for {sorted(missing)}"
        assert (spec.get("retention") or "").strip(), f"{name} has no retention rule"


def test_audit_evidence_is_not_cascade_deleted_with_the_tenant(tables):
    """Deleting a tenant must not silently destroy the record of what it did."""
    for name, spec in tables.items():
        if spec["classification"] != "APPEND_ONLY_AUDIT":
            continue
        rule = spec["deletion"]["tenant_deletion"].lower()
        assert "redact" in rule or "retain" in rule, \
            f"{name} is audit evidence but tenant deletion says: {spec['deletion']['tenant_deletion']!r}"


# ── Cache and object-storage boundaries ──────────────────────────────────────

def test_every_table_declares_a_cache_key_pattern(tables):
    for name, spec in tables.items():
        key = spec.get("cache_key_pattern") or ""
        assert key.startswith("ira:{env}:"), f"{name} has no environment-scoped cache key"


def test_cache_keys_never_contain_identifying_or_secret_values(tables):
    """A cache key ends up in logs, metrics and Redis `KEYS` output."""
    forbidden = ("{email}", "{username}", "{token}", "{secret}", "{password}", "{api_key}")
    for name, spec in tables.items():
        key = spec["cache_key_pattern"]
        for bad in forbidden:
            assert bad not in key, f"{name} cache key contains {bad}"


def test_scoped_cache_keys_carry_their_scope(tables):
    for name, spec in tables.items():
        cls, key = spec["classification"], spec["cache_key_pattern"]
        if cls == "WORKSPACE_SCOPED":
            assert "{tenant_id}" in key and "{workspace_id}" in key, f"{name}"
        elif cls == "USER_PRIVATE":
            assert "{user_id}" in key, f"{name}"
        elif cls in SCOPED:
            assert "{tenant_id}" in key, f"{name}"


def test_key_material_declares_itself_uncacheable(tables):
    """A correctly-scoped cache key is not permission to cache the row.

    `user_mfa_keys` holds wrapped DEKs. A copy of one outside the database is a
    copy of the thing that opens every second factor it protects, so no cache
    entry and no export object may ever exist for it — and the manifest has to
    be able to SAY that, rather than declaring a tidy `ira:{env}:user:...` key
    that reads as an intention to use it.

    Pinned in both directions: the listed tables must carry the flags, and no
    table may quietly acquire them (which would exempt it from the scoping
    checks above by looking like key material).
    """
    for name in sorted(UNCACHEABLE_TABLES):
        spec = tables[name]
        assert spec.get("cacheable") is False, (
            f"{name} holds key material but does not declare cacheable=false")
        assert spec.get("exportable") is False, (
            f"{name} holds key material but does not declare exportable=false")

    declared = {n for n, s in tables.items()
                if s.get("cacheable") is False or s.get("exportable") is False}
    assert declared == set(UNCACHEABLE_TABLES), (
        f"tables declaring themselves uncacheable/unexportable are {sorted(declared)}, "
        f"expected {sorted(UNCACHEABLE_TABLES)}. Adding a table here is a "
        f"deliberate claim that its rows must never leave the database — make "
        f"it deliberately, in this constant.")


def test_object_storage_prefixes_carry_their_scope(tables):
    for name, spec in tables.items():
        cls, prefix = spec["classification"], spec["object_storage_prefix"]
        assert prefix, f"{name} has no object-storage prefix"
        if cls == "WORKSPACE_SCOPED":
            assert prefix.startswith("tenant/{tenant_id}/workspace/{workspace_id}/"), f"{name}"
        elif cls == "USER_PRIVATE":
            assert prefix.startswith("user/{user_id}/private/"), f"{name}"
        elif cls in GLOBAL:
            assert prefix.startswith("platform/"), f"{name}"


# ── Migration specification ──────────────────────────────────────────────────

def test_every_migrating_table_specifies_its_target_columns(tables):
    for name, spec in tables.items():
        mig = spec["migration"]
        if not mig["requires_migration"]:
            continue
        assert mig["target_columns"], f"{name} requires migration but specifies no target columns"
        for col, s in mig["target_columns"].items():
            for field in ("type", "nullable_during_transition", "final_nullable",
                          "fk", "on_delete", "backfill_source", "used_by_rls"):
                assert field in s, f"{name}.{col} is missing '{field}'"
            assert (s["backfill_source"] or "").strip(), \
                f"{name}.{col} has no backfill source — rows would be assigned silently"


def test_no_target_column_is_final_non_null_without_a_backfill_source(tables):
    """A NOT NULL column with no deterministic source is a migration that fails
    partway through on real data."""
    for name, spec in tables.items():
        for col, s in spec["migration"]["target_columns"].items():
            if s["final_nullable"] is False:
                assert s["backfill_source"], f"{name}.{col} is final NOT NULL with no backfill source"


def test_security_events_keeps_a_nullable_tenant(tables):
    """A failed login for an unknown user has no tenant. Forcing NOT NULL there
    would mean either inventing a tenant or dropping the event."""
    spec = tables["security_events"]
    target = spec["migration"]["target_columns"]
    assert "tenant_id" in target
    assert target["tenant_id"]["final_nullable"] is True, \
        "platform-scope security events must remain recordable"


def test_biometric_audit_keeps_nullable_tenant_context(tables):
    """A platform-level authentication event may have no active tenant.

    Before Batch 1C this was a planned nullable target column; after it, the
    column exists and nullability is a property of the live schema, checked by
    `test_the_manifest_matches_the_live_current_state` and by
    `test_a_platform_event_may_have_no_tenant` in the Batch 0/1 suite.
    """
    spec = tables["biometric_audit"]
    assert spec["classification"] == "APPEND_ONLY_AUDIT"
    target = spec["migration"]["target_columns"]
    if "tenant_id" in target:
        assert target["tenant_id"]["final_nullable"] is True
    else:
        assert spec["current"]["has_tenant_id"], "biometric_audit lost its tenant context"


# ── Live-schema agreement (needs a database) ─────────────────────────────────

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")


@pytest.fixture(scope="module")
def live_tables():
    if not DSN:
        pytest.skip("IRA_TEST_DATABASE_DSN not set — live-schema agreement skipped")
    psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 not installed")
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                       EXISTS (SELECT 1 FROM information_schema.columns col
                               WHERE col.table_name=c.relname AND col.table_schema='public'
                                 AND col.column_name='tenant_id'),
                       (SELECT count(*) FROM pg_policies p
                         WHERE p.schemaname='public' AND p.tablename=c.relname)
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='public' AND c.relkind='r'
            """)
            return {
                r[0]: {"rls": r[1], "forced": r[2], "tenant_id": r[3], "policies": r[4]}
                for r in cur.fetchall()
            }
    finally:
        conn.close()


def test_a_new_table_must_be_classified(live_tables, tables):
    """The test that stops the manifest going stale.

    A table added to the schema without a manifest entry fails here. That is
    what keeps Phase 3B from migrating against a document that no longer
    describes the database.
    """
    unclassified = sorted(set(live_tables) - set(tables))
    assert not unclassified, (
        f"live tables with no classification: {unclassified}. "
        "Add them to DATABASE_TENANCY_CLASSIFICATION.json before proceeding."
    )


def test_the_manifest_describes_no_table_that_does_not_exist(live_tables, tables):
    phantom = sorted(set(tables) - set(live_tables))
    assert not phantom, f"manifest describes tables absent from the schema: {phantom}"


def test_the_manifest_matches_the_live_current_state(live_tables, tables):
    """Guards against the manifest being edited by hand and drifting.

    `rls_enabled` and `policy_count` are compared as well as `rls_forced`,
    because Phase 3B introduced a protection those two fields are the whole
    expression of: `legacy_identity_map` is DENY-ALL — row-level security
    enabled with **no policy**, so nothing is readable however many grants the
    role holds. `rls_forced` is false there and correctly so (the migrations
    read the table as the owner), so a drift check that looked only at
    `rls_forced` would watch that table's protection vanish and report
    agreement. It found exactly one stale field the first time it was widened.
    """
    mismatches = []
    for name, live in live_tables.items():
        cur = tables[name]["current"]
        for field, key in (
            ("has_tenant_id", "tenant_id"),
            ("rls_forced", "forced"),
            ("rls_enabled", "rls"),
            ("policy_count", "policies"),
        ):
            if cur[field] != live[key]:
                mismatches.append(f"{name}.{field} manifest={cur[field]} live={live[key]}")
    assert not mismatches, "manifest has drifted from the live schema:\n  " + "\n  ".join(mismatches)


#: Tables the manifest scopes to a user whose LIVE policy still names only the
#: tenant. A debt list in the shape `KNOWN_UNERASED` used for Phase 9: it
#: records what is outstanding, and it is not a bin for new entries.
#:
#: Batch 8's predicate guard found three on the day it was written. Batch 11
#: closed all three, and none of them the same way:
#:
#:   connector_accounts     FIXED by `060`. The serious one — Phase 7 OAuth
#:                          refresh tokens, readable by every member of the
#:                          tenant. It took a batch rather than a line because
#:                          DEK rotation re-seals credentials tenant-wide, and a
#:                          user-scoped policy would have made
#:                          `keyring.rotate_tenant` rewrap nothing, count zero
#:                          stragglers, and destroy the old key underneath every
#:                          credential still using it. Silently. The rotation
#:                          rework landed with the migration for that reason.
#:
#:   companion_preferences  FIXED by `060`. Same shape, no entanglement.
#:
#:   notifications          NOT a defect. The table has no `user_id` column at
#:                          all, so the manifest was describing a column that
#:                          does not exist. The live policy — tenant plus
#:                          workspace — was correct all along; the SPECIFICATION
#:                          was wrong, and it is the specification that changed.
#:
#: Empty, and it stays empty. Adding an entry is a decision to leave a table
#: readable by every member of its tenant, and belongs in a PR that argues it.
KNOWN_TENANT_ONLY: frozenset[str] = frozenset()


def test_the_known_tenant_only_list_does_not_grow(tables):
    """The list above is a debt, so it is pinned at exactly its known size.

    A guard with an allow-list that anybody can extend is not a guard. Making
    the count explicit means widening it is a visible edit in a diff rather than
    one more name in a set.
    """
    assert len(KNOWN_TENANT_ONLY) == 0, (
        f"KNOWN_TENANT_ONLY has {len(KNOWN_TENANT_ONLY)} entries, expected 0. "
        f"Batch 11 emptied it. Adding one is a decision to leave a table "
        f"readable by every member of its tenant, and belongs in a PR that "
        f"argues it — not a name appended to a set.")
    for name in KNOWN_TENANT_ONLY:
        assert name in tables, (
            f"{name} is pinned as a known tenant-only table but is not in the "
            f"manifest at all — the debt list has gone stale")


@pytest.fixture(scope="module")
def live_policy_predicates():
    """Every table's policy expressions, concatenated per table."""
    if not DSN:
        pytest.skip("IRA_TEST_DATABASE_DSN not set — live-schema agreement skipped")
    psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 not installed")
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.relname,
                       string_agg(coalesce(pg_get_expr(p.polqual, p.polrelid), '')
                               || coalesce(pg_get_expr(p.polwithcheck, p.polrelid), ''),
                                  ' ')
                  FROM pg_policy p
                  JOIN pg_class c ON c.oid = p.polrelid
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public'
                 GROUP BY c.relname
            """)
            return dict(cur.fetchall())
    finally:
        conn.close()


def test_a_table_the_manifest_scopes_to_a_user_is_scoped_to_a_user_live(
        live_policy_predicates, tables):
    """The drift guard whose absence let `user_sessions` sit wrong since `023`.

    The manifest said, correctly and from the beginning, that `user_sessions`
    and `user_devices` isolate on `app.tenant_id` AND `app.user_id`. The live
    schema had a single `tenant_isolation` policy naming the tenant alone, so
    every member of a tenant could read and revoke every other member's
    sessions — and erasing one account, which deletes `WHERE tenant_id = $1` and
    leans on RLS to narrow it, deleted everybody's.

    Nothing caught it for thirty-four migrations because the only fields
    compared against the live schema were `rls_enabled`, `rls_forced` and
    `policy_count` — and one tenant-only policy is, numerically, one policy. The
    specification was right; nobody checked the database against it.

    So this compares the PREDICATE. A manifest entry naming `app.user_id` is a
    claim that a tenant administrator cannot read the table, and that claim is
    now false out loud rather than quietly.
    """
    offenders = []
    for name, spec in tables.items():
        if name in KNOWN_TENANT_ONLY:
            continue
        declared = " ".join(str(spec.get("rls", {}).get(cmd) or "")
                            for cmd in ("select", "insert", "update", "delete"))
        if "app.user_id" not in declared:
            continue
        live = live_policy_predicates.get(name)
        if live is None:
            continue   # no policies at all is `test_the_manifest_matches…`'s business
        if "app.user_id" not in live:
            offenders.append(
                f"{name}: the manifest isolates it by app.user_id, but no live "
                f"policy mentions app.user_id")

    assert not offenders, (
        "table(s) whose live policies are weaker than the manifest specifies:\n  "
        + "\n  ".join(offenders)
        + "\n\nA table the manifest scopes to a user, isolated live by tenant "
          "alone, is readable and writable by every member of that tenant — and "
          "an account erasure that relies on RLS to narrow a tenant-wide DELETE "
          "will destroy every member's rows."
    )


def test_the_predicate_scanner_reads_real_expressions(live_policy_predicates):
    """Proves the fixture above returns policy text rather than empty strings.

    A scanner that returned nothing would make the assertion pass for every
    table at once, silently — which is the same shape as the defect it exists to
    catch.
    """
    assert live_policy_predicates, "no policies found at all"
    assert any("app.tenant_id" in expr
               for expr in live_policy_predicates.values()), (
        "no policy expression mentions app.tenant_id, so the scanner is not "
        "reading predicates")


def test_no_table_currently_forced_loses_its_protection(live_tables, tables):
    """45 tables have FORCE RLS today. None may be classified in a way that
    would drop it."""
    for name, live in live_tables.items():
        if live["forced"]:
            assert tables[name]["rls"]["required"], \
                f"{name} has FORCE RLS today but the manifest says RLS is not required"


# ── The two exemptions above, made expensive to grant ────────────────────────


def test_the_pre_tenant_table_list_is_exactly_what_is_expected(tables):
    """"This table needs no tenant" is the sentence that precedes most
    cross-tenant defects, so it is pinned rather than merely written down."""
    assert PRE_TENANT_TABLES == frozenset({
        "user_credentials", "email_verification_tokens",
        "password_reset_tokens",
    }), (
        f"the pre-tenant exemption changed to {sorted(PRE_TENANT_TABLES)}. Each "
        f"entry claims a row CANNOT belong to a tenant — not that giving it one "
        f"would be inconvenient. Justify it above and update this assertion."
    )
    for name in PRE_TENANT_TABLES:
        assert name in tables, f"{name} is exempt but is not in the manifest"
        assert tables[name]["current"]["has_tenant_id"] is False, (
            f"{name} is on the pre-tenant list but HAS a tenant column. The "
            f"exemption is now hiding it from the tenant-column check for no "
            f"reason — remove it from the list."
        )



def test_possession_scoped_tables_still_have_a_real_policy(tables):
    """The carve-out replaces the user predicate; it does not remove one.

    A table here must still declare FORCE RLS and a predicate on every command
    — what changes is only which session setting the predicate reads. Without
    this, "possession-scoped" would become a way to declare no policy at all.
    """
    assert POSSESSION_SCOPED_TABLES == frozenset({
        "email_verification_tokens", "password_reset_tokens",
    }), (
        f"the possession-scoped list changed to {sorted(POSSESSION_SCOPED_TABLES)}. "
        f"Each entry is a claim that the legitimate reader CANNOT supply a user "
        f"id. Justify it above and update this assertion."
    )
    from tests.readiness.test_cross_tenant_sweep import POSSESSION_SETTINGS

    for name in POSSESSION_SCOPED_TABLES:
        rls = tables[name]["rls"]
        assert rls["required"] and rls["force"], (
            f"{name} is possession-scoped but does not declare FORCE RLS")
        for op in ("select", "insert", "update", "delete"):
            assert rls.get(op), f"{name} has no {op.upper()} policy specification"
        assert any(s in rls["session_context"] for s in POSSESSION_SETTINGS), (
            f"{name} is possession-scoped but its session context "
            f"{rls['session_context']} names none of the settings the "
            f"cross-tenant sweep recognises ({list(POSSESSION_SETTINGS)}). The "
            f"two registers must agree, or a table is exempt in one place and "
            f"unknown in the other."
        )
