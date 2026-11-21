"""Phase 2 — the migration directory must be safe to execute wholesale.

`docker-compose.portable.yml` mounts `postgres/` as `/docker-entrypoint-initdb.d`,
and PostgreSQL runs **every** `.sql` file it finds there, in alphabetical order,
on first start. Anything in that directory is therefore executed on every fresh
install whether or not that was intended.

A rollback script placed beside the migrations runs straight after the migration
it reverses. `023_tenancy_down.sql` was originally committed there; CI's
migration chain applied it and the isolation tests failed. On a real portable
install the same ordering would have dropped row-level security silently, with
nothing in the logs to suggest anything was wrong.

These tests make that class of mistake fail in CI instead of in a deployment.
They need no database — they read the directory.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# tests/tenancy/ -> tests/ -> ira/ -> supracloud-jarvis/
POSTGRES_DIR = Path(__file__).resolve().parents[3] / "postgres"
ROLLBACK_DIR = POSTGRES_DIR / "rollback"

#: Statements that undo schema. Safe inside a rollback script; a landmine in a
#: file that auto-runs on every fresh install.
#
# `[ \t]*` rather than `\s*`: `\s` matches newlines, so `^\s*DROP` can begin
# matching on a blank line and run into a DROP several lines later — which makes
# match.start() point at the wrong line and the reported statement come out
# empty.
_DESTRUCTIVE = re.compile(
    r"^[ \t]*(DROP[ \t]+(TABLE|POLICY|COLUMN)|ALTER[ \t]+TABLE[ \t]+\S+[ \t]+DISABLE[ \t]+ROW)",
    re.IGNORECASE | re.MULTILINE,
)


def _auto_run_sql() -> list[Path]:
    """Files PostgreSQL would execute from the mounted init directory.

    Top level only: the entrypoint does not recurse, which is exactly why
    rollback/ is a safe place to keep the down scripts.
    """
    return sorted(p for p in POSTGRES_DIR.glob("*.sql"))


def test_postgres_dir_exists():
    assert POSTGRES_DIR.is_dir(), f"missing {POSTGRES_DIR}"
    assert _auto_run_sql(), "no migrations found"


def test_no_rollback_scripts_in_the_auto_run_directory():
    """The specific failure this suite exists for."""
    offenders = [
        p.name for p in _auto_run_sql()
        if "_down" in p.stem or "rollback" in p.stem or p.stem.endswith("_undo")
    ]
    assert not offenders, (
        f"Rollback script(s) in the auto-run migration directory: {offenders}.\n"
        "docker-compose.portable.yml mounts postgres/ as "
        "/docker-entrypoint-initdb.d, so PostgreSQL runs every .sql there in "
        "alphabetical order on a fresh install — a down script would undo the "
        "migration it follows, silently. Move it to postgres/rollback/."
    )


def test_rollback_directory_holds_the_down_script():
    """The counterpart: the rollback must still exist somewhere, or 'no down
    scripts in postgres/' would pass by deleting the rollback entirely."""
    assert ROLLBACK_DIR.is_dir(), "postgres/rollback/ is missing"
    downs = list(ROLLBACK_DIR.glob("*_down.sql"))
    assert downs, "postgres/rollback/ contains no rollback scripts"
    assert any(p.name == "023_tenancy_down.sql" for p in downs), (
        "023_tenancy_down.sql is missing — 023_tenancy.sql must remain reversible"
    )


def test_no_auto_run_migration_is_destructive():
    """Broader than the filename check: catches a file that drops schema without
    'down' in its name."""
    offenders: list[str] = []
    for path in _auto_run_sql():
        text = path.read_text(encoding="utf-8")
        # Strip comments so documentation about DROP does not trip this.
        code = re.sub(r"--[^\n]*", "", text)
        # `DROP ... IF EXISTS` immediately followed by a CREATE of the same
        # object is the normal idempotent-redefinition idiom, not a rollback.
        # Check the whole LINE, not the regex match: the pattern stops at
        # "DROP POLICY" and would never see the "IF EXISTS" that follows it.
        for match in _DESTRUCTIVE.finditer(code):
            line_start = code.rfind("\n", 0, match.start()) + 1
            line_end = code.find("\n", match.start())
            line = code[line_start: line_end if line_end != -1 else len(code)].strip()
            if re.search(r"DROP\s+\w+\s+IF\s+EXISTS", line, re.IGNORECASE):
                continue
            offenders.append(f"{path.name}: {line}")
    assert not offenders, (
        "Destructive statement(s) in a migration that auto-runs on fresh installs:\n"
        + "\n".join(offenders)
    )


def test_migrations_are_uniquely_numbered():
    """Two files with the same prefix run in an order nobody chose."""
    seen: dict[str, str] = {}
    dupes: list[str] = []
    for path in _auto_run_sql():
        if path.name == "init.sql":
            continue
        prefix = path.name.split("_", 1)[0]
        if prefix in seen:
            dupes.append(f"{prefix}: {seen[prefix]} and {path.name}")
        else:
            seen[prefix] = path.name
    assert not dupes, "Duplicate migration numbers: " + "; ".join(dupes)


def test_tenancy_migration_is_present_and_enables_rls():
    """Guards against the migration being weakened rather than removed."""
    sql = (POSTGRES_DIR / "023_tenancy.sql").read_text(encoding="utf-8")
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql, (
        "FORCE is what stops the table owner bypassing the policy — without it "
        "the isolation tests pass while protecting nothing."
    )
    assert "WITH CHECK" in sql, (
        "Without WITH CHECK, a tenant can INSERT or UPDATE rows into another tenant."
    )
    assert "NULLIF(current_setting('app.tenant_id', true), '')" in sql, (
        "NULLIF keeps a cleared context denying cleanly instead of raising a "
        "uuid cast error."
    )


@pytest.mark.parametrize(
    "table",
    [
        "conversations", "messages", "memory_embeddings", "people", "tasks",
        "trust_ledger", "approval_requests", "agent_runs", "tool_calls",
        "privacy_events", "tenant_memberships", "user_devices", "user_sessions",
        "companions",
    ],
)
def test_every_scoped_table_is_listed_in_the_rls_loop(table):
    """The policy loop is driven by an array literal. A table added to the schema
    but forgotten there gets no policy at all — and, unlike a syntax error, that
    fails open."""
    sql = (POSTGRES_DIR / "023_tenancy.sql").read_text(encoding="utf-8")
    loop = sql.split("scoped TEXT[] := ARRAY[", 1)[1].split("]", 1)[0]
    assert f"'{table}'" in loop, f"{table} is missing from the RLS policy loop"


# ── Phase 3: 024_companion_catalogue.sql ─────────────────────────────────────
#
# Same class of guard as the ones above, extended to the migration Phase 3 adds.
# The rollback-in-the-wrong-directory check already covers 024 (it globs the
# whole directory), so what is added here is the pair of assertions specific to
# this migration: it must stay reversible, and the customer-data tables it
# creates must not lose their policies to a careless edit.


@pytest.mark.parametrize(
    "migration,rollback",
    [
        ("023_tenancy.sql", "023_tenancy_down.sql"),
        ("024_companion_catalogue.sql", "024_companion_catalogue_down.sql"),
    ],
)
def test_every_schema_migration_has_a_rollback(migration, rollback):
    """A migration whose rollback does not exist is a migration you cannot undo
    on a deployment that is already running it."""
    assert (POSTGRES_DIR / migration).is_file(), f"{migration} is missing"
    assert (ROLLBACK_DIR / rollback).is_file(), (
        f"{migration} has no rollback at rollback/{rollback}"
    )


def test_companion_migration_enables_and_forces_rls_on_its_customer_tables():
    """Guards against the migration being weakened rather than removed."""
    sql = (POSTGRES_DIR / "024_companion_catalogue.sql").read_text(encoding="utf-8")
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql, (
        "FORCE is what stops the table owner bypassing the policy — without it "
        "the isolation tests pass while protecting nothing."
    )
    assert "WITH CHECK" in sql, (
        "Without WITH CHECK, a tenant can INSERT or UPDATE rows into another tenant."
    )
    assert "NULLIF(current_setting('app.tenant_id', true), '')" in sql, (
        "NULLIF keeps a cleared context denying cleanly instead of raising a "
        "uuid cast error."
    )


@pytest.mark.parametrize("table", ["companion_preferences", "companion_versions"])
def test_every_new_scoped_table_is_listed_in_the_rls_loop(table):
    """The policy loop is driven by an array literal. A table added to the
    schema but forgotten there gets no policy at all — and, unlike a syntax
    error, that fails OPEN."""
    sql = (POSTGRES_DIR / "024_companion_catalogue.sql").read_text(encoding="utf-8")
    loop = sql.split("scoped TEXT[] := ARRAY[", 1)[1].split("]", 1)[0]
    assert f"'{table}'" in loop, f"{table} is missing from the RLS policy loop"


def test_the_companion_migration_does_not_introduce_a_rival_profile_table():
    """023 created `companions` for the user's companion identity and 024
    EXTENDS it. A second table meaning the same thing is how a query ends up
    reading the stale one — the same call Phase 2 made when it promoted the
    pre-existing `tenants` table instead of adding a rival.
    """
    sql = (POSTGRES_DIR / "024_companion_catalogue.sql").read_text(encoding="utf-8")
    code = re.sub(r"--[^\n]*", "", sql)
    assert "CREATE TABLE IF NOT EXISTS companion_profiles" not in code, (
        "024 creates a `companion_profiles` table alongside 023's `companions`. "
        "Extend `companions` instead, and record the naming in DATA_MODEL.md."
    )
    assert "ALTER TABLE companions" in code, (
        "024 no longer extends `companions` — has the profile moved elsewhere?"
    )


# ── Phase 4: 025_governance.sql ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "migration,rollback",
    [("025_governance.sql", "025_governance_down.sql")],
)
def test_governance_migration_has_a_rollback(migration, rollback):
    assert (POSTGRES_DIR / migration).is_file(), f"{migration} is missing"
    assert (ROLLBACK_DIR / rollback).is_file(), (
        f"{migration} has no rollback at rollback/{rollback}"
    )


def test_governance_migration_enables_and_forces_rls():
    sql = (POSTGRES_DIR / "025_governance.sql").read_text(encoding="utf-8")
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "WITH CHECK" in sql
    assert "NULLIF(current_setting('app.tenant_id', true), '')" in sql


@pytest.mark.parametrize("table", [
    "constitution_rules", "permission_grants", "permission_decisions",
    "approval_events",
])
def test_every_governance_table_is_listed_in_the_rls_loop(table):
    """A table added to the schema but forgotten in the policy loop gets no
    policy at all — and, unlike a syntax error, that fails OPEN."""
    sql = (POSTGRES_DIR / "025_governance.sql").read_text(encoding="utf-8")
    loop = sql.split("scoped TEXT[] := ARRAY[", 1)[1].split("]", 1)[0]
    assert f"'{table}'" in loop, f"{table} is missing from the RLS policy loop"


def test_the_decision_log_is_append_only():
    """permission_decisions is evidence of what IRA was allowed to do. A trigger
    refusing UPDATE and DELETE is what makes it evidence rather than notes."""
    sql = (POSTGRES_DIR / "025_governance.sql").read_text(encoding="utf-8")
    assert "permission_decisions_append_only" in sql
    assert "BEFORE UPDATE OR DELETE ON permission_decisions" in sql


# ── Phase 5: 026_privacy.sql ─────────────────────────────────────────────────


def test_privacy_migration_has_a_rollback():
    assert (POSTGRES_DIR / "026_privacy.sql").is_file()
    assert (ROLLBACK_DIR / "026_privacy_down.sql").is_file()


def test_privacy_migration_enables_and_forces_rls():
    sql = (POSTGRES_DIR / "026_privacy.sql").read_text(encoding="utf-8")
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "NULLIF(current_setting('app.tenant_id', true), '')" in sql


@pytest.mark.parametrize("table", [
    "retention_settings", "deletion_requests", "export_requests",
])
def test_every_privacy_table_is_listed_in_the_rls_loop(table):
    sql = (POSTGRES_DIR / "026_privacy.sql").read_text(encoding="utf-8")
    loop = sql.split("scoped TEXT[] := ARRAY[", 1)[1].split("]", 1)[0]
    assert f"'{table}'" in loop, f"{table} is missing from the RLS policy loop"


def test_the_rollback_refuses_to_strand_redacted_audit_records():
    """A redacted row's content is already gone. Rolling the schema back around
    it would leave a tombstone nobody can account for."""
    sql = (ROLLBACK_DIR / "026_privacy_down.sql").read_text(encoding="utf-8")
    assert "already been redacted" in sql
    assert "RAISE EXCEPTION" in sql


def test_the_rollback_keeps_the_chain_v2_column():
    """Dropping body_digest would strand every chain-v2 record: their hash is
    sha256(prev_hash ++ body_digest), so without it the chain cannot be
    verified at all."""
    sql = (ROLLBACK_DIR / "026_privacy_down.sql").read_text(encoding="utf-8")
    code = re.sub(r"--[^\n]*", "", sql)
    assert "DROP COLUMN IF EXISTS body_digest" not in code
    assert "body_digest" in sql, "the rollback must explain why it keeps the column"


def test_the_ledger_tenant_default_is_the_private_tenant():
    """023 made trust_ledger.tenant_id NOT NULL with no default, so every ledger
    write failed silently (the callers are fail-soft). The default must be the
    same value 023 backfilled every existing row with, or new records would
    disagree with old ones about which tenant they belong to."""
    tenancy = (POSTGRES_DIR / "023_tenancy.sql").read_text(encoding="utf-8")
    privacy = (POSTGRES_DIR / "026_privacy.sql").read_text(encoding="utf-8")
    assert "e91aceeb-89fb-5ad2-b43c-4eee2bad079c" in tenancy
    assert "ALTER COLUMN tenant_id SET DEFAULT" in privacy
    assert "e91aceeb-89fb-5ad2-b43c-4eee2bad079c" in privacy


def test_026_does_not_redefine_columns_an_earlier_migration_owns():
    """A column added twice with different meanings is a silent no-op.

    `ADD COLUMN IF NOT EXISTS` on a column that already exists does nothing —
    quietly. Phase 5 originally declared `sensitivity` this way, not realising
    017_temporal_memory.sql had owned it since long before with a completely
    different vocabulary:

        017:      a CONFIDENTIALITY level — public | shared | private | secret
        Phase 5:  a CATEGORY             — health | financial | family | …

    The ADD did nothing, the inline CHECK added a second contradictory
    constraint, and every insert failed on a clean migration chain. It passed
    locally only because the 026 rollback had dropped 017's column first — a
    second bug. CI caught both; this test makes the class fail earlier.
    """
    owned_by_017 = {"memory_type", "source", "observed_at", "valid_from",
                    "valid_until", "confidence", "sensitivity",
                    "owner_confirmed", "superseded_by", "deleted_at",
                    "embedding_version"}

    sql = (POSTGRES_DIR / "026_privacy.sql").read_text(encoding="utf-8")
    code = re.sub(r"--[^\n]*", "", sql)

    added = set(re.findall(
        r"ALTER TABLE memory_embeddings\s+ADD COLUMN IF NOT EXISTS\s+(\w+)",
        code,
    ))
    assert added, "no memory_embeddings columns parsed — the scan is wrong"

    # `deleted_at` is the one deliberate overlap: same name, same meaning, no
    # new constraint. Anything else that collides is redefining another
    # migration's column.
    collisions = (added & owned_by_017) - {"deleted_at"}
    assert not collisions, (
        f"026 re-declares column(s) owned by 017_temporal_memory.sql: "
        f"{sorted(collisions)}. ADD COLUMN IF NOT EXISTS will silently do "
        f"nothing, and any CHECK added alongside will contradict the existing "
        f"one. Use a distinct name (as `sensitivity_category` does)."
    )


def test_the_026_rollback_only_drops_columns_026_added():
    """Dropping another migration's column destroys a working feature.

    The first draft of the 026 rollback dropped `sensitivity`, `confidence` and
    `deleted_at` — all 017's — which is what masked the collision above: the
    rollback removed 017's column and the next apply re-created it with Phase
    5's definition.
    """
    owned_by_017 = {"confidence", "sensitivity", "memory_type", "source",
                    "observed_at", "valid_from", "valid_until",
                    "owner_confirmed", "superseded_by", "embedding_version"}

    sql = (ROLLBACK_DIR / "026_privacy_down.sql").read_text(encoding="utf-8")
    code = re.sub(r"--[^\n]*", "", sql)
    dropped = set(re.findall(
        r"ALTER TABLE memory_embeddings\s+DROP COLUMN IF EXISTS\s+(\w+)", code
    ))
    assert dropped, "no dropped columns parsed — the scan is wrong"

    trespass = dropped & owned_by_017
    assert not trespass, (
        f"the 026 rollback drops column(s) owned by 017: {sorted(trespass)}"
    )


# ── Phase 6: 027_realtime.sql ────────────────────────────────────────────────


def test_realtime_migration_has_a_rollback():
    assert (POSTGRES_DIR / "027_realtime.sql").is_file()
    assert (ROLLBACK_DIR / "027_realtime_down.sql").is_file()


def test_realtime_migration_enables_and_forces_rls():
    sql = (POSTGRES_DIR / "027_realtime.sql").read_text(encoding="utf-8")
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "WITH CHECK" in sql
    assert "NULLIF(current_setting('app.tenant_id', true), '')" in sql


@pytest.mark.parametrize("table", ["realtime_sessions", "realtime_events"])
def test_every_realtime_table_is_listed_in_the_rls_loop(table):
    sql = (POSTGRES_DIR / "027_realtime.sql").read_text(encoding="utf-8")
    loop = sql.split("scoped TEXT[] := ARRAY[", 1)[1].split("]", 1)[0]
    assert f"'{table}'" in loop, f"{table} is missing from the RLS policy loop"


def test_the_realtime_rollback_refuses_to_discard_multi_tenant_history():
    """`realtime_sessions` is the evidence for which external provider carried
    each user's live microphone. Dropping it does not undo the routing — it
    only removes the ability to account for it."""
    sql = (ROLLBACK_DIR / "027_realtime_down.sql").read_text(encoding="utf-8")
    assert "RAISE EXCEPTION" in sql
    assert "Export per tenant first" in sql


def test_027_does_not_redefine_columns_an_earlier_migration_owns():
    """Same class of guard as the 026 check above.

    027 creates only new `realtime_*` tables, so the correct answer is that it
    ALTERs nothing that already existed. An ALTER appearing here later is the
    moment to check for a collision, not after CI applies a clean chain.
    """
    sql = (POSTGRES_DIR / "027_realtime.sql").read_text(encoding="utf-8")
    code = re.sub(r"--[^\n]*", "", sql)
    altered = set(re.findall(r"ALTER TABLE\s+(?:IF EXISTS\s+)?(\w+)", code))
    foreign = {t for t in altered if not t.startswith("realtime_")}
    assert not foreign, (
        f"027 alters table(s) it does not own: {sorted(foreign)}. Check for a "
        f"column collision before trusting ADD COLUMN IF NOT EXISTS — see "
        f"test_026_does_not_redefine_columns_an_earlier_migration_owns."
    )


# ── Phase 7: 028_connectors.sql ──────────────────────────────────────────────


def test_connector_migration_has_a_rollback():
    assert (POSTGRES_DIR / "028_connectors.sql").is_file()
    assert (ROLLBACK_DIR / "028_connectors_down.sql").is_file()


def test_connector_migration_enables_and_forces_rls():
    sql = (POSTGRES_DIR / "028_connectors.sql").read_text(encoding="utf-8")
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "WITH CHECK" in sql
    assert "NULLIF(current_setting('app.tenant_id', true), '')" in sql


@pytest.mark.parametrize("table", [
    "connector_accounts", "connector_oauth_states", "action_receipts",
])
def test_every_connector_table_is_listed_in_the_rls_loop(table):
    sql = (POSTGRES_DIR / "028_connectors.sql").read_text(encoding="utf-8")
    loop = sql.split("scoped TEXT[] := ARRAY[", 1)[1].split("]", 1)[0]
    assert f"'{table}'" in loop, f"{table} is missing from the RLS policy loop"


def test_there_is_no_plaintext_credential_column():
    """The whole credential design rests on this. A column named for a token,
    or a TEXT column that could hold one, would make the encryption optional."""
    sql = (POSTGRES_DIR / "028_connectors.sql").read_text(encoding="utf-8")
    code = re.sub(r"--[^\n]*", "", sql)
    creates = re.findall(
        r"CREATE TABLE IF NOT EXISTS (connector_\w+|action_receipts)\s*\((.*?)\n\);",
        code, re.DOTALL,
    )
    assert creates, "no connector tables parsed — the scan is wrong"
    for table, body in creates:
        for banned in ("refresh_token", "access_token", "client_secret",
                       "credential_plaintext", "token TEXT"):
            assert banned not in body, (
                f"{table} declares {banned!r}. Credentials are stored ONLY as "
                f"`credential_ciphertext BYTEA` with a key id and nonce — see "
                f"ADR-0006."
            )


def test_the_credential_columns_cannot_be_written_unencrypted():
    sql = (POSTGRES_DIR / "028_connectors.sql").read_text(encoding="utf-8")
    assert "credentials_are_encrypted" in sql
    assert "credential_is_not_obvious_plaintext" in sql
    assert "revoked_accounts_hold_no_credential" in sql, (
        "a revoked account that still holds a token is 'disconnected' in name "
        "only"
    )


def test_oauth_state_is_single_use_at_the_database():
    """The handler checks this too. The trigger is what makes a replay fail even
    if the handler's check were wrong or raced."""
    sql = (POSTGRES_DIR / "028_connectors.sql").read_text(encoding="utf-8")
    assert "oauth_state_single_use" in sql
    assert "BEFORE UPDATE ON connector_oauth_states" in sql


def test_receipts_are_append_only_and_redact_only():
    sql = (POSTGRES_DIR / "028_connectors.sql").read_text(encoding="utf-8")
    assert "action_receipts_append_only" in sql
    assert "BEFORE UPDATE OR DELETE ON action_receipts" in sql


def test_only_s256_pkce_is_expressible():
    sql = (POSTGRES_DIR / "028_connectors.sql").read_text(encoding="utf-8")
    assert "code_challenge_method = 'S256'" in sql, (
        "a `plain` PKCE downgrade must not be storable"
    )


def test_no_write_scope_is_seeded_for_cloud():
    """The edition gate (ADR-0001), checked in the seed rather than only in the
    application."""
    sql = (POSTGRES_DIR / "028_connectors.sql").read_text(encoding="utf-8")
    block = sql.split("INSERT INTO connector_scopes", 1)[1].split("ON CONFLICT", 1)[0]
    for row in re.findall(r"\('(\w+)',\s*'([^']+)',\s*\n?\s*'[^']*',\s*\n?\s*"
                          r"'(read|write)',\s*(TRUE|FALSE),\s*(TRUE|FALSE)\)", block):
        _connector, _scope, kind, _private, cloud = row
        if kind == "write":
            assert cloud.upper() == "FALSE", (
                f"{_scope} is a write scope offered to cloud in Phase 7"
            )


def test_no_seeded_scope_permits_sending():
    sql = (POSTGRES_DIR / "028_connectors.sql").read_text(encoding="utf-8")
    block = sql.split("INSERT INTO connector_scopes", 1)[1].split("ON CONFLICT", 1)[0]
    for banned in ("gmail.send", "https://mail.google.com/", "gmail.modify"):
        assert banned not in block, f"{banned} is seeded as an offered scope"


def test_the_connector_rollback_warns_about_unrevoked_credentials():
    """Dropping the table removes IRA's copy but leaves the grant live at Google.
    That is a revocation problem, not a data-loss problem, and the script must
    say so."""
    sql = (ROLLBACK_DIR / "028_connectors_down.sql").read_text(encoding="utf-8")
    assert "RAISE WARNING" in sql
    assert "myaccount.google.com/permissions" in sql
    assert "RAISE EXCEPTION" in sql, "multi-tenant receipts must refuse"


def test_028_does_not_redefine_columns_an_earlier_migration_owns():
    sql = (POSTGRES_DIR / "028_connectors.sql").read_text(encoding="utf-8")
    code = re.sub(r"--[^\n]*", "", sql)
    altered = set(re.findall(r"ALTER TABLE\s+(?:IF EXISTS\s+)?(\w+)", code))
    foreign = {t for t in altered
               if not (t.startswith("connector_") or t == "action_receipts")}
    assert not foreign, f"028 alters table(s) it does not own: {sorted(foreign)}"


# ── Phase 8: 029_commercial.sql ──────────────────────────────────────────────


def test_commercial_migration_has_a_rollback():
    assert (POSTGRES_DIR / "029_commercial.sql").is_file()
    assert (ROLLBACK_DIR / "029_commercial_down.sql").is_file()


def test_commercial_migration_enables_and_forces_rls():
    sql = (POSTGRES_DIR / "029_commercial.sql").read_text(encoding="utf-8")
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "WITH CHECK" in sql
    assert "NULLIF(current_setting('app.tenant_id', true), '')" in sql


@pytest.mark.parametrize("table", [
    "usage_quotas", "model_usage", "admin_access_grants", "admin_access_log",
])
def test_every_commercial_table_is_listed_in_the_rls_loop(table):
    sql = (POSTGRES_DIR / "029_commercial.sql").read_text(encoding="utf-8")
    loop = sql.split("scoped TEXT[] := ARRAY[", 1)[1].split("]", 1)[0]
    assert f"'{table}'" in loop, f"{table} is missing from the RLS policy loop"


def test_retains_prompts_is_nullable_and_never_defaulted():
    """The single most misleading line this migration could contain would be
    `NOT NULL DEFAULT FALSE` here: it would silently convert "we have not
    checked" into "it does not retain", and the routing policy would then send
    health notes to that provider."""
    sql = (POSTGRES_DIR / "029_commercial.sql").read_text(encoding="utf-8")
    code = re.sub(r"--[^\n]*", "", sql)
    match = re.search(r"retains_prompts\s+BOOLEAN([^,\n]*)", code)
    assert match, "retains_prompts is not declared as a nullable BOOLEAN"
    tail = match.group(1).upper()
    assert "NOT NULL" not in tail, (
        "retains_prompts became NOT NULL. NULL is a real answer — 'the provider "
        "has not stated' — and the Phase 1 seam requires it be shown as such."
    )
    assert "DEFAULT" not in tail, "retains_prompts must not be defaulted"


def test_the_usage_table_cannot_hold_a_prompt():
    """Metering is the table most likely to be joined, exported and shipped to a
    billing system. Nothing in it may carry what the user said."""
    sql = (POSTGRES_DIR / "029_commercial.sql").read_text(encoding="utf-8")
    code = re.sub(r"--[^\n]*", "", sql)
    body = code.split("CREATE TABLE IF NOT EXISTS model_usage", 1)[1].split("\n);", 1)[0]

    assert "JSONB" not in body.upper() and "BYTEA" not in body.upper(), (
        "model_usage gained a JSONB or BYTEA column — that is where a prompt "
        "would sit"
    )
    # Every TEXT column must be CHECK-constrained, a foreign key, or
    # length-bounded. Split on top-level commas so a constraint on the next line
    # still counts.
    definitions, depth, current = [], 0, []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            definitions.append("".join(current))
            current = []
        else:
            current.append(char)
    definitions.append("".join(current))

    unbounded = []
    for definition in definitions:
        flat = " ".join(definition.split())
        m = re.match(r"(\w+)\s+TEXT\b(.*)", flat, re.IGNORECASE)
        if not m:
            continue
        column, rest = m.group(1), m.group(2).upper()
        if "CHECK" in rest or "REFERENCES" in rest:
            continue
        unbounded.append(f"model_usage.{column}")
    assert not unbounded, (
        f"unbounded TEXT column(s) in model_usage: {unbounded}. Every TEXT "
        f"column here must be a fixed vocabulary, a key, or length-bounded."
    )


def test_the_metering_record_cannot_contradict_the_routing_guarantee():
    """The routing promise written into the billing record, so a violation is
    visible in the data and not only in the code that produced it."""
    sql = (POSTGRES_DIR / "029_commercial.sql").read_text(encoding="utf-8")
    assert "local_only_stayed_local" in sql
    assert "credentials_never_left_the_machine" in sql
    assert "refusals_consume_nothing" in sql


def test_break_glass_access_is_time_bound_user_authorised_and_logged():
    """The three words from the threat model, each a constraint."""
    sql = (POSTGRES_DIR / "029_commercial.sql").read_text(encoding="utf-8")
    code = re.sub(r"--[^\n]*", "", sql)
    # time-bound
    assert "grant_window_is_bounded" in code
    assert "grant_expires_after_it_starts" in code
    # user-authorised: NOT NULL, and no column that lets an admin self-authorise
    assert re.search(r"user_authorised_at\s+TIMESTAMPTZ\s+NOT NULL", code)
    assert "admin_authorised_at" not in code, (
        "a column allowing an administrator to authorise their own access"
    )
    # logged, and the log is bounded by the grant
    assert "admin_access_within_grant" in code
    assert "BEFORE INSERT ON admin_access_log" in code


def test_the_access_log_and_grants_are_append_only():
    sql = (POSTGRES_DIR / "029_commercial.sql").read_text(encoding="utf-8")
    assert "BEFORE UPDATE OR DELETE ON admin_access_log" in sql
    assert "BEFORE UPDATE OR DELETE ON admin_access_grants" in sql


def test_the_grant_window_cap_matches_the_python_constant():
    """A schema that permits longer than the code, or vice versa, means one of
    the two is decorative."""
    from core.models.admin_access import MAX_GRANT_HOURS

    sql = (POSTGRES_DIR / "029_commercial.sql").read_text(encoding="utf-8")
    assert f"INTERVAL '{MAX_GRANT_HOURS} hours'" in sql, (
        f"the schema's grant cap and MAX_GRANT_HOURS ({MAX_GRANT_HOURS}) disagree"
    )


def test_the_commercial_rollback_refuses_to_drop_administrator_access_records():
    """A deployment that has exercised break-glass access has something a
    regulator or a user may ask about."""
    sql = (ROLLBACK_DIR / "029_commercial_down.sql").read_text(encoding="utf-8")
    assert "administrator access(es) have been logged" in sql
    assert "RAISE EXCEPTION" in sql


def test_029_does_not_redefine_columns_an_earlier_migration_owns():
    sql = (POSTGRES_DIR / "029_commercial.sql").read_text(encoding="utf-8")
    code = re.sub(r"--[^\n]*", "", sql)
    altered = set(re.findall(r"ALTER TABLE\s+(?:IF EXISTS\s+)?(\w+)", code))
    owned = {"model_providers", "usage_quotas", "model_usage",
             "admin_access_grants", "admin_access_log"}
    foreign = altered - owned
    assert not foreign, f"029 alters table(s) it does not own: {sorted(foreign)}"


# ── The backfill window (Phase 3B batches 0–3) ───────────────────────────────

_NO_FORCE = re.compile(
    r"^[ \t]*ALTER[ \t]+TABLE[ \t]+(\S+)[ \t]+NO[ \t]+FORCE[ \t]+ROW[ \t]+LEVEL[ \t]+SECURITY",
    re.IGNORECASE | re.MULTILINE,
)
_FORCE = re.compile(
    r"^[ \t]*ALTER[ \t]+TABLE[ \t]+(\S+)[ \t]+FORCE[ \t]+ROW[ \t]+LEVEL[ \t]+SECURITY",
    re.IGNORECASE | re.MULTILINE,
)


def test_every_migration_that_lifts_force_restores_it():
    """**A migration may lift FORCE to backfill. It may not leave it lifted.**

    A data migration cannot read the rows it is migrating while FORCE ROW LEVEL
    SECURITY applies to the role running it: the migration binds no
    `app.tenant_id`, so every predicate evaluates against NULL and the statement
    matches zero rows *without failing*. `035_memberships.sql` section 0 has the
    full reasoning, and the symptom that exposed it — a `SET NOT NULL` failing on
    rows a backfill was supposed to have filled.

    So `NO FORCE` is legitimate, briefly, inside one transaction. Forgetting the
    matching `FORCE` is not: it leaves the table owner — which is the migration
    role, and in this deployment the application role — able to bypass every
    policy on that table, permanently and silently. Nothing else would notice.
    """
    offenders: dict[str, list[str]] = {}
    for path in _auto_run_sql():
        sql = path.read_text(encoding="utf-8")
        lifted = set(_NO_FORCE.findall(sql))
        if not lifted:
            continue
        # findall on _FORCE also matches the "NO FORCE" lines, so subtract by
        # position: a table is restored only if a FORCE appears that is not part
        # of a NO FORCE match.
        no_force_spans = {m.start() for m in _NO_FORCE.finditer(sql)}
        restored = {
            m.group(1) for m in _FORCE.finditer(sql)
            if not any(abs(m.start() - s) < 40 and m.start() >= s for s in no_force_spans)
        }
        missing = sorted(lifted - restored)
        if missing:
            offenders[path.name] = missing

    assert not offenders, (
        f"migration(s) lift FORCE ROW LEVEL SECURITY and never restore it: "
        f"{offenders}. Add `ALTER TABLE <t> FORCE ROW LEVEL SECURITY` at the end "
        f"of the file. Until then the table owner bypasses every policy on that "
        f"table, and no test that binds a tenant would ever notice."
    )


def test_the_force_pairing_check_is_not_vacuous():
    """The check above passes trivially if no migration lifts FORCE at all —
    which is exactly what a regex typo would produce."""
    lifting = [p.name for p in _auto_run_sql() if _NO_FORCE.search(p.read_text(encoding="utf-8"))]
    assert lifting, (
        "no migration lifts FORCE ROW LEVEL SECURITY, so the pairing check above "
        "is asserting nothing. If the backfill window was removed, remove these "
        "two tests with it; if the pattern changed, update the regex."
    )
