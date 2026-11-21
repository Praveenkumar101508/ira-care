-- =============================================================================
-- 034_identity_mapping.sql — Phase 3B Batch 0B
--
-- The username → immutable user_id mapping that every later ownership backfill
-- reads, plus the email-policy constraints on the global `users` table.
--
-- ── Why a mapping table and not a JOIN ───────────────────────────────────────
--
-- Six tables key their owner on a TEXT string — `totp_secrets.username`,
-- `voice_profiles.owner_name`, `delegated_users.username`,
-- `access_audit_log.actor`, `command_runs.actor`, and `owner_profile` by
-- implication. Each has to become an immutable `user_id`.
--
-- Resolving those strings inline, in six separate migrations, means six chances
-- to resolve them differently. Worse, a string that resolves ambiguously —
-- two users, or none — has to be *detected*, and an inline JOIN silently drops
-- the row or silently picks one. This table makes the resolution happen ONCE,
-- makes it inspectable before anything depends on it, and makes ambiguity a
-- recorded fact rather than a lost row.
--
-- It is **migration compatibility data, not the permanent authentication
-- design**. It is dropped in Batch 10. It must never be exposed through a
-- public API, and no authorisation decision may read it.
--
-- ── Deterministic identity ───────────────────────────────────────────────────
--
-- `core/identity/principal.py` defines:
--
--     private_user_id(username) = uuid5(IRA_UUID_NAMESPACE, f"user:private:{username}")
--
-- PostgreSQL's `uuid_generate_v5(namespace, name)` produces byte-identical
-- output to Python's `uuid.uuid5`, verified before this migration was written.
-- So the mapping is computed here rather than passed in, and re-running the
-- migration produces exactly the same identifiers — which is what makes the
-- whole chain idempotent.
--
-- ── Ambiguity blocks; it does not guess ──────────────────────────────────────
--
-- `resolution` records how each mapping was derived, and `MAPPED` is the only
-- value downstream migrations accept. A username that cannot be resolved
-- deterministically is written with a quarantine reason and **no user_id**.
-- Batch 1 refuses to constrain a security-secret table while any relevant row
-- is unresolved — see 036.
--
-- A guess made inside a migration is invisible and permanent. That is the
-- specific failure this design exists to prevent.
--
-- ── Email ────────────────────────────────────────────────────────────────────
--
-- Email is a MUTABLE VERIFIED ATTRIBUTE, never the identity (CLOUD-ADR-0008).
-- Normalisation here is deliberately conservative — lowercase the domain, trim
-- whitespace — and nothing else:
--
--   * the local part is case-sensitive by RFC 5321, however providers treat it;
--   * dot-insensitivity (`a.b@` ≡ `ab@`) is a Gmail behaviour, not a standard;
--   * plus-addressing is widely but not universally supported.
--
-- Treating `a.b@example.com` and `ab@example.com` as one mailbox is correct for
-- one provider and wrong for another, and the failure mode is one person
-- receiving another's password reset. **Nothing here merges accounts.**
--
-- Uniqueness applies only to a VERIFIED email, so an unverified value cannot be
-- used to seize an identity by claiming someone else's address.
--
-- Rollback: rollback/034_identity_mapping_down.sql
-- ── No psql metacommands in this file ────────────────────────────────────────
--
-- The UUIDs below are written as literals rather than `\set` variables, and
-- that is deliberate. `utils/migrations.py` — the durable upgrade path — applies
-- migrations through **asyncpg**, which is a PostgreSQL protocol driver and not
-- psql: it rejects `\set` with `syntax error at or near "\"`.
--
-- Migrations 023 and 031 use `\set` and are therefore rejected by that runner
-- (recorded as R-45). They apply only because docker-entrypoint-initdb.d runs
-- them through psql on a brand-new volume. This file must not inherit that.
--
-- =============================================================================


-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Conservative email normalisation
--
--    IMMUTABLE so it can back an index. Lowercases the domain only; the local
--    part is preserved byte-for-byte.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION normalise_email(addr TEXT)
RETURNS TEXT
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN addr IS NULL THEN NULL
        WHEN position('@' in btrim(addr)) = 0 THEN btrim(addr)
        ELSE left(btrim(addr), position('@' in btrim(addr)) - 1)
             || '@'
             || lower(substring(btrim(addr) from position('@' in btrim(addr)) + 1))
    END
$$;

COMMENT ON FUNCTION normalise_email(TEXT) IS
    'Conservative: trims whitespace and lowercases the DOMAIN only. Does not '
    'remove dots or strip plus-tags — those are provider-specific behaviours, '
    'and normalising them would silently merge distinct mailboxes.';

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. users — expand, never replace
--
--    `users.id` is already a UUID primary key, so no destructive key change is
--    needed. These columns support the migration and the email policy.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE users ADD COLUMN IF NOT EXISTS normalised_email TEXT
    GENERATED ALWAYS AS (normalise_email(email)) STORED;
ALTER TABLE users ADD COLUMN IF NOT EXISTS legacy_username TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS migrated_from   TEXT;

COMMENT ON COLUMN users.legacy_username IS
    'The single-owner account name this user was migrated from. COMPATIBILITY '
    'ONLY — never a security principal. Retired with the identity map in Batch 10.';

-- ── Replacing 023's email index ──────────────────────────────────────────────
--
-- `023_tenancy.sql` created:
--
--     UNIQUE INDEX users_email_key ON users (lower(email))
--       WHERE email IS NOT NULL AND deleted_at IS NULL
--
-- It is dropped here, and that is a deliberate security fix rather than
-- housekeeping. It has two defects against the approved policy:
--
--   1. **It constrains UNVERIFIED addresses.** Anyone who registers with
--      victim@example.com — without ever proving they control it — permanently
--      blocks the real owner from using their own address. An unverified claim
--      must never be able to seize or deny an identity, and under that index it
--      can.
--
--   2. **`lower(email)` lowercases the LOCAL part**, which is exactly the
--      aggressive normalisation CLOUD-ADR-0008 rules out. The local part is
--      case-sensitive by RFC 5321; collapsing it merges mailboxes that some
--      providers route to different people.
--
-- The replacement constrains only VERIFIED addresses, and normalises
-- conservatively.
DROP INDEX IF EXISTS users_email_key;

-- A VERIFIED email is globally unique. An unverified one is not constrained,
-- so claiming someone else's address cannot block or seize their account.
CREATE UNIQUE INDEX IF NOT EXISTS users_verified_email_key
    ON users (normalised_email)
    WHERE normalised_email IS NOT NULL
      AND email_verified_at IS NOT NULL
      AND deleted_at IS NULL;

-- External identity is unique by (issuer, subject), never by email
-- (CLOUD-ADR-0008). `users.auth_provider` and `users.external_subject` already
-- exist; this constrains them. The dedicated `external_identities` table
-- arrives with OIDC in Phase 2 — it is NOT created here.
CREATE UNIQUE INDEX IF NOT EXISTS users_external_identity_key
    ON users (auth_provider, external_subject)
    WHERE external_subject IS NOT NULL AND deleted_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS users_legacy_username_key
    ON users (legacy_username) WHERE legacy_username IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. legacy_identity_map — the single resolution point
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS legacy_identity_map (
    legacy_username TEXT PRIMARY KEY,
    user_id         UUID REFERENCES users(id) ON DELETE RESTRICT,
    source_table    TEXT NOT NULL,
    resolution      TEXT NOT NULL
                    CHECK (resolution IN (
                        'MAPPED',              -- resolved; safe to use
                        'MISSING_USER',        -- no user account found
                        'DUPLICATE_USERNAME',  -- the string matches more than one
                        'AMBIGUOUS_OWNER',     -- cannot determine which user
                        'INVALID_REFERENCE'    -- malformed or unusable
                    )),
    quarantine_reason TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- A mapping is only usable when it resolved AND carries an id. The
    -- constraint makes "MAPPED with no user" unrepresentable rather than
    -- something a later query has to remember to check.
    CONSTRAINT legacy_identity_map_mapped_has_user
        CHECK ((resolution = 'MAPPED') = (user_id IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_legacy_identity_map_user
    ON legacy_identity_map (user_id) WHERE user_id IS NOT NULL;

COMMENT ON TABLE legacy_identity_map IS
    'MIGRATION COMPATIBILITY ONLY. Maps single-owner username strings to '
    'immutable user_ids for Batch 1 backfills. Never exposed through an API; '
    'no authorisation decision may read it. Dropped in Batch 10.';

-- ─────────────────────────────────────────────────────────────────────────────
-- 3a. The owner seeded by 023
--
--     `023_tenancy.sql` already inserts the private owner at the UUID
--     `principal.py` derives for username "admin", carrying the account name in
--     `external_subject`. That row IS the identity these migrations resolve to;
--     it just predates `legacy_username`. Backfilling the column and mapping the
--     name here means Batch 1 has a resolution for `admin` without inventing a
--     second user for the same person.
-- ─────────────────────────────────────────────────────────────────────────────

UPDATE users
   SET legacy_username = external_subject,
       migrated_from   = COALESCE(migrated_from, '023_tenancy')
 WHERE auth_provider = 'password'
   AND external_subject IS NOT NULL
   AND legacy_username IS NULL
   AND NOT EXISTS (
       SELECT 1 FROM users u2
       WHERE u2.legacy_username = users.external_subject AND u2.id <> users.id
   );

INSERT INTO legacy_identity_map (legacy_username, user_id, source_table, resolution)
SELECT u.legacy_username, u.id, 'users', 'MAPPED'
FROM users u
WHERE u.legacy_username IS NOT NULL AND u.deleted_at IS NULL
ON CONFLICT (legacy_username) DO NOTHING;

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Backfill: create a global user for each existing account
--
--    Source is `delegated_users`, which is the single-owner account table.
--    Identifiers are deterministic, so re-running produces the same rows and
--    the ON CONFLICT is a no-op.
--
--    Usernames differing only by case are treated as DUPLICATE and are NOT
--    mapped — `Alice` and `alice` may be one person or two, and a migration is
--    the wrong place to decide.
-- ─────────────────────────────────────────────────────────────────────────────

INSERT INTO users (id, display_name, auth_provider, status, legacy_username, migrated_from)
SELECT
    uuid_generate_v5('6f3a1c2e-0b4d-5e8a-9c17-2d4b8e6f0a35'::uuid, 'user:private:' || d.username),
    d.username,
    'local',
    CASE WHEN d.active THEN 'active' ELSE 'suspended' END,
    d.username,
    'delegated_users'
FROM delegated_users d
WHERE NOT EXISTS (
    SELECT 1 FROM delegated_users d2
    WHERE lower(d2.username) = lower(d.username) AND d2.id <> d.id
)
ON CONFLICT (id) DO NOTHING;

-- Resolved mappings.
INSERT INTO legacy_identity_map (legacy_username, user_id, source_table, resolution)
SELECT
    d.username,
    uuid_generate_v5('6f3a1c2e-0b4d-5e8a-9c17-2d4b8e6f0a35'::uuid, 'user:private:' || d.username),
    'delegated_users',
    'MAPPED'
FROM delegated_users d
WHERE NOT EXISTS (
    SELECT 1 FROM delegated_users d2
    WHERE lower(d2.username) = lower(d.username) AND d2.id <> d.id
)
ON CONFLICT (legacy_username) DO NOTHING;

-- Quarantined: case-insensitive duplicates. Recorded, not guessed.
INSERT INTO legacy_identity_map
    (legacy_username, user_id, source_table, resolution, quarantine_reason)
SELECT
    d.username,
    NULL,
    'delegated_users',
    'DUPLICATE_USERNAME',
    'more than one delegated_users row differs only by case; a migration '
    'cannot decide whether these are one person or two'
FROM delegated_users d
WHERE EXISTS (
    SELECT 1 FROM delegated_users d2
    WHERE lower(d2.username) = lower(d.username) AND d2.id <> d.id
)
ON CONFLICT (legacy_username) DO NOTHING;

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. Owner strings referenced by the Batch 1 tables but absent from
--    `delegated_users`. Recorded as MISSING_USER so 036/037 can refuse rather
--    than inventing an owner.
-- ─────────────────────────────────────────────────────────────────────────────

INSERT INTO legacy_identity_map
    (legacy_username, user_id, source_table, resolution, quarantine_reason)
SELECT DISTINCT s.name, NULL::uuid, s.src, 'MISSING_USER',
       'referenced as an owner but no matching account exists in delegated_users'
FROM (
    SELECT username  AS name, 'totp_secrets'   AS src FROM totp_secrets
    UNION
    SELECT owner_name, 'voice_profiles'                FROM voice_profiles
) s
WHERE NOT EXISTS (SELECT 1 FROM legacy_identity_map m WHERE m.legacy_username = s.name)
ON CONFLICT (legacy_username) DO NOTHING;

-- ─────────────────────────────────────────────────────────────────────────────
-- 6. RLS
--
--    `users` is IDENTITY_GLOBAL (CLOUD-ADR-0006) — one person, many tenants —
--    so it is NOT tenant-scoped. Cross-tenant visibility is prevented by only
--    ever reaching users through a verified membership, never by a broad join,
--    and Batch 3's membership policy is what enforces that.
--
--    `legacy_identity_map` holds no tenant column and no secret, but it does
--    hold `user_id` — it is a directory of who exists. The application role has
--    no reason to read it at all.
--
--    The first version of this migration expressed that with `REVOKE ALL FROM
--    PUBLIC` alone. That is not durable. Every documented provisioning step for
--    this schema — including the one in the header of
--    `tests/tenancy/test_tenant_isolation_rls.py` — ends with
--
--        GRANT ALL ON ALL TABLES IN SCHEMA public TO ira_app;
--
--    which runs AFTER the migrations and silently hands the grant straight back.
--    A protection that the standard setup procedure undoes is not a protection;
--    it is a comment. The cross-tenant sweep caught it.
--
--    So the table is DENY-ALL by row-level security instead: RLS enabled, and
--    **no policy at all**. With no permissive policy, every row fails the
--    default-deny and the application role reads zero rows however many grants
--    it holds. A later blanket GRANT cannot reopen it, because grants and RLS
--    are independent gates and both must pass.
--
--    `FORCE` is deliberately NOT set. The table owner runs the migrations, and
--    036/037/035 must read this table to backfill ownership. Without FORCE the
--    owner is exempt and the backfills work; with FORCE they would resolve zero
--    rows and every `user_id` would stay NULL — a silent, total failure of the
--    thing this table exists for. The REVOKE stays as well, so a role that
--    somehow acquires BYPASSRLS still has to have been granted access.
-- ─────────────────────────────────────────────────────────────────────────────

REVOKE ALL ON legacy_identity_map FROM PUBLIC;

ALTER TABLE legacy_identity_map ENABLE ROW LEVEL SECURITY;
-- No CREATE POLICY. The absence is the mechanism, so it is stated rather than
-- left to be inferred from a missing line.

COMMENT ON TABLE legacy_identity_map IS
    'MIGRATION COMPATIBILITY ONLY. Maps single-owner username strings to '
    'immutable user_ids for Batch 1 backfills. Never exposed through an API; '
    'no authorisation decision may read it. Dropped in Batch 10. '
    'RLS is enabled with NO policy: the application role reads zero rows '
    'regardless of grants. Migrations reach it as the owner, which is exempt '
    'because FORCE is deliberately not set.';

COMMENT ON COLUMN users.normalised_email IS
    'Generated. Conservative normalisation — see normalise_email(). Unique only '
    'when the address is VERIFIED, so an unverified claim cannot seize an identity.';
