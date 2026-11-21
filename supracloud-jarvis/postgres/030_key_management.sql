-- =============================================================================
-- 030_key_management.sql — Blocker B3: envelope encryption and crypto-shredding
--
-- One table:
--
--   tenant_data_keys   per-tenant data-encryption keys, stored WRAPPED
--
-- ── What was wrong, in the words of the report that found it ─────────────────
--
-- RELEASE_READINESS.md R11: *"there is no KMS and no crypto-shredding. The key
-- is an environment variable, `rewrap` exists with no caller, so rotating the
-- key re-encrypts nothing, and a leaked key compromises every stored credential
-- at once."*
--
-- Three separate faults, and this migration addresses the second and third. The
-- first — no KMS — is NOT fixed here and is not claimed to be; see the closing
-- section.
--
-- ── The shape: one KEK in the environment, many DEKs in this table ───────────
--
-- Before: every credential in the deployment was sealed directly under
-- `IRA_CONNECTOR_ENCRYPTION_KEY`. One key, one blast radius, and nothing to
-- destroy short of the key every tenant depends on.
--
-- After: that variable becomes a *key-encryption key* (KEK) and does exactly one
-- job — wrapping the rows in this table. Credentials are sealed under a
-- *data-encryption key* (DEK) that belongs to a single tenant and a single
-- version. So:
--
--   a leaked DEK      compromises one tenant, one version
--   a rotation        mints a new version and re-seals that tenant's rows
--   a destroyed DEK   makes every ciphertext under it permanently unopenable,
--                     without touching — or even finding — the ciphertext
--
-- That last line is crypto-shredding, and it is the reason this table exists.
--
-- ── Why the key material can be NULL, and why that is the whole point ────────
--
-- `wrapped_dek` is nullable. In most schemas a nullable column holding the one
-- thing the table is for would be a smell. Here it is the mechanism: destroying
-- a key means setting it to NULL, and the CHECK constraint
-- `destroyed_keys_hold_no_material` makes the two states inseparable —
--
--     (destroyed_at IS NULL) = (wrapped_dek IS NOT NULL)
--
-- A row cannot be marked destroyed while still holding its key, and cannot hold
-- its key while marked destroyed. There is no ordering of statements, no partial
-- failure and no clever UPDATE that produces a row claiming to be shredded while
-- the material is still there. The database refuses the state, so no application
-- bug can reach it.
--
-- The row itself is kept. A tombstone is not clutter: it is the only thing that
-- can later distinguish *"this ciphertext will not open because its key was
-- deliberately destroyed on 2026-08-01 for account-erasure"* from *"this
-- ciphertext will not open and nobody knows why"*. The first is a working
-- system; the second is a bug report.
--
-- ── One active key per tenant, enforced by an index ──────────────────────────
--
-- `one_active_key_per_tenant_purpose` is a partial UNIQUE index over
-- (tenant_id, purpose) WHERE the key is neither retired nor destroyed. Two
-- active DEKs would make "which key do I seal under?" ambiguous, and ambiguity
-- there means some rows silently become unrecoverable at the next rotation.
-- A UNIQUE index answers it structurally rather than by convention.
--
-- ── Shred-only: the permitted transitions ────────────────────────────────────
--
-- A key row moves in one direction:
--
--     active  →  retired  →  destroyed
--
-- `tenant_data_keys_shred_only` refuses everything else. Specifically it refuses
-- resurrection — putting material back into a destroyed row — because a
-- crypto-shredding guarantee that an UPDATE can undo is not a guarantee. It also
-- refuses DELETE, for the tombstone reason above.
--
-- ── Per tenant, not per user, and why that is the right grain ────────────────
--
-- `core/privacy/erasure.py` already erases by tenant: every statement is
-- `DELETE FROM … WHERE tenant_id = $1`. A per-user DEK would be a finer key than
-- the deletion that destroys it, which buys nothing and adds a way for one
-- user's erasure to strand another's credentials. The grain matches the erasure.
--
-- ── What this is NOT — stated here so it is not inferred ─────────────────────
--
-- **This is still not a KMS or an HSM.** The KEK is an environment variable. An
-- attacker holding both the database and the process environment gets every
-- live DEK and therefore every live credential. Envelope encryption does not
-- change that; what it changes is everything below the KEK.
--
-- **Crypto-shredding is not retroactive over backups.** Destroying a DEK makes
-- its ciphertext unopenable in the live database and in every backup taken
-- afterwards. A backup taken *before* the destruction still contains the wrapped
-- DEK, and restoring it with the KEK still in the environment restores access.
-- Honest statement of the bound: shredding removes the key from the present, not
-- from the past. `docs/operations/DISASTER_RECOVERY.md` carries the operational
-- consequence.
--
-- What it does buy, precisely:
--
--   1. Blast radius. A DEK is one tenant, one version.
--   2. Deletion that does not depend on finding every copy. A row missed by a
--      DELETE — in a replica, an unvacuumed page, a WAL segment — is ciphertext
--      whose key no longer exists.
--   3. A rotation that actually rotates. `rewrap` finally has a caller.
--
-- Additive. Idempotent. Rollback: rollback/030_key_management_down.sql.
-- =============================================================================

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. The key registry.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS tenant_data_keys (
    tenant_id           UUID NOT NULL,

    -- What the key protects. A single value today; enumerated rather than free
    -- text so a second purpose is a migration somebody reviews, not a typo that
    -- silently creates a key nothing will ever find again.
    purpose             TEXT NOT NULL DEFAULT 'connector_credentials'
                        CHECK (purpose IN ('connector_credentials')),

    -- Monotonic per (tenant, purpose). Referenced from ciphertext rows as
    -- `dek:<version>` in their existing `encryption_key_id` column, which is why
    -- no column had to change in 028.
    key_version         INTEGER NOT NULL CHECK (key_version >= 1),

    -- The DEK, encrypted under the KEK. NULL means destroyed — see the CHECK
    -- below, which is what makes that unambiguous.
    wrapped_dek         BYTEA,
    wrap_nonce          BYTEA,

    -- Which KEK wrapped it. A KEK rotation needs to know which rows it must
    -- re-wrap, and a row wrapped under a KEK that is no longer configured must
    -- report that clearly rather than fail as "tampered".
    kek_id              TEXT NOT NULL CHECK (length(trim(kek_id)) > 0),

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Superseded by a newer version. Still openable: old ciphertext must remain
    -- readable long enough for a rotation to finish, and a half-finished
    -- rotation that has already made rows unreadable is worse than no rotation.
    retired_at          TIMESTAMPTZ,

    -- Shredded. Past this point the ciphertext under this key is gone.
    destroyed_at        TIMESTAMPTZ,
    destruction_reason  TEXT,

    PRIMARY KEY (tenant_id, purpose, key_version),

    -- The load-bearing constraint. Destroyed and holding material is not a
    -- state this table can represent.
    CONSTRAINT destroyed_keys_hold_no_material CHECK (
        (destroyed_at IS NULL) = (wrapped_dek IS NOT NULL)
        AND (destroyed_at IS NULL) = (wrap_nonce IS NOT NULL)
    ),

    -- A destruction with no reason cannot be explained to the user whose data
    -- it was, which is the only audience that matters here.
    CONSTRAINT destruction_states_a_reason CHECK (
        destroyed_at IS NULL
        OR length(trim(COALESCE(destruction_reason, ''))) >= 8
    ),

    -- A key cannot be retired or destroyed before it existed.
    CONSTRAINT key_lifecycle_is_ordered CHECK (
        (retired_at   IS NULL OR retired_at   >= created_at)
        AND (destroyed_at IS NULL OR destroyed_at >= created_at)
    )
);

COMMENT ON TABLE tenant_data_keys IS
    'Per-tenant data-encryption keys, wrapped under the environment KEK. '
    'Destroying a row (wrapped_dek → NULL) is crypto-shredding: every '
    'ciphertext sealed under that version becomes permanently unopenable.';

COMMENT ON COLUMN tenant_data_keys.wrapped_dek IS
    'NULL means destroyed, and the destroyed_keys_hold_no_material CHECK makes '
    'that the only reading. Not a missing value.';

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Exactly one active key per tenant and purpose.
--
-- Not a nicety. Two active DEKs means the seal path picks one arbitrarily, and
-- a rotation that walks "the active key" then strands whatever the other sealed.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE UNIQUE INDEX IF NOT EXISTS one_active_key_per_tenant_purpose
    ON tenant_data_keys (tenant_id, purpose)
    WHERE retired_at IS NULL AND destroyed_at IS NULL;

-- Rotation and shredding both start by asking "what does this tenant have?".
CREATE INDEX IF NOT EXISTS idx_tenant_data_keys_live
    ON tenant_data_keys (tenant_id, purpose, key_version DESC)
    WHERE destroyed_at IS NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Row-level security: a tenant may not read another tenant's key rows.
--
-- The wrapped DEK is useless without the KEK, so this is defence in depth rather
-- than the primary protection — but "useless without another secret" is exactly
-- the reasoning that ages badly, and the sweep in
-- tests/readiness/test_cross_tenant_sweep.py requires every tenant-scoped table
-- to ENABLE and FORCE regardless.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE tenant_data_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_data_keys FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON tenant_data_keys;
CREATE POLICY tenant_isolation ON tenant_data_keys
    USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Shred-only. active → retired → destroyed, and nothing else.
--
-- The transitions this refuses are each a real way the guarantee could be
-- undone, so each has its own message rather than one generic refusal.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION tenant_data_keys_shred_only()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'tenant_data_keys rows are not deletable. A destroyed key leaves a '
            'tombstone on purpose: it is the only thing that can distinguish '
            '"this credential will not open because its key was deliberately '
            'destroyed" from "this credential will not open and nobody knows '
            'why". Destroy the key instead (set destroyed_at and NULL the '
            'material); the row stays.';
    END IF;

    -- Identity is immutable. Changing which tenant or version a key belongs to
    -- would silently repoint ciphertext at the wrong key.
    IF NEW.tenant_id   IS DISTINCT FROM OLD.tenant_id
       OR NEW.purpose  IS DISTINCT FROM OLD.purpose
       OR NEW.key_version IS DISTINCT FROM OLD.key_version
       OR NEW.created_at  IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION
            'A key row''s identity (tenant, purpose, version, creation) is '
            'immutable. Ciphertext refers to it by that identity; changing it '
            'would repoint stored credentials at a different key.';
    END IF;

    -- Resurrection. The one transition that would make crypto-shredding a
    -- claim rather than a property.
    IF OLD.destroyed_at IS NOT NULL THEN
        IF NEW.destroyed_at IS NULL OR NEW.wrapped_dek IS NOT NULL THEN
            RAISE EXCEPTION
                'Key version % for this tenant was destroyed at %. Destruction '
                'is final: restoring key material would mean the '
                'crypto-shredding guarantee could be undone by an UPDATE, which '
                'is not a guarantee. Mint a new version instead.',
                OLD.key_version, OLD.destroyed_at;
        END IF;
        IF NEW.destruction_reason IS DISTINCT FROM OLD.destruction_reason
           OR NEW.destroyed_at IS DISTINCT FROM OLD.destroyed_at THEN
            RAISE EXCEPTION
                'The record of a destruction may not be rewritten.';
        END IF;
        RETURN NEW;
    END IF;

    -- Live key: the material itself may not be swapped. A rotation mints a new
    -- version; it does not overwrite an old one, because ciphertext already
    -- points at the old version and would stop opening.
    IF NEW.destroyed_at IS NULL
       AND NEW.wrapped_dek IS DISTINCT FROM OLD.wrapped_dek THEN
        RAISE EXCEPTION
            'The material of a live key may not be replaced in place. '
            'Credentials already reference version % — overwriting it would '
            'make every one of them unopenable with no record of why. Mint a '
            'new version and re-seal (see core/connectors/keyring.rotate).',
            OLD.key_version;
    END IF;

    -- Un-retiring. Retirement is what stops new seals from choosing this key;
    -- reversing it would let two versions be active despite the unique index
    -- having been satisfied at insert time.
    IF OLD.retired_at IS NOT NULL AND NEW.retired_at IS NULL THEN
        RAISE EXCEPTION
            'A retired key cannot be made active again. Retirement is how a '
            'rotation guarantees no new credential is sealed under the version '
            'it is in the middle of replacing.';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tenant_data_keys_shred_only ON tenant_data_keys;
CREATE TRIGGER tenant_data_keys_shred_only
    BEFORE UPDATE OR DELETE ON tenant_data_keys
    FOR EACH ROW EXECUTE FUNCTION tenant_data_keys_shred_only();

COMMIT;
