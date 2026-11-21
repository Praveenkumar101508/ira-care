-- =============================================================================
-- 038_biometric_audit_context.sql — Phase 3B Batch 1C
--
-- The voiceprint-matching audit trail gains owner and tenant context, and
-- becomes append-only in the database rather than by convention.
--
-- ── Why nullable tenant, and why that is not a loophole ──────────────────────
--
-- A biometric match happens during *authentication*. At that moment there may
-- be no tenant yet — the caller has not been resolved to one, or the attempt
-- failed and there is no membership to resolve. Forcing `tenant_id NOT NULL`
-- would mean either inventing a tenant for the row or discarding the event, and
-- discarding failed authentication events removes exactly the ones an
-- investigation needs.
--
-- So `tenant_id` and `workspace_id` are nullable, and the policy below turns
-- that into a *security property* rather than a gap:
--
--     tenant_id = current_setting('app.tenant_id')
--
-- For a platform-scope row, `tenant_id` is NULL, and `NULL = <uuid>` evaluates
-- to NULL — which is not true — so **no tenant can see it**. Platform-scope
-- events are invisible to every customer by construction, and readable only by
-- a role that is not the application role.
--
-- A tenant-context action must still record its verified tenant. Nullable means
-- "there was genuinely no tenant", not "we did not bother".
--
-- ── Append-only in the database ──────────────────────────────────────────────
--
-- `realtime_events` (027) and `permission_decisions` (025) already use a
-- refusing trigger. The same pattern here: UPDATE and DELETE are refused by the
-- database, so an application bug, a compromised credential and a careless
-- cleanup script all fail identically.
--
-- ── What must never land in this table ───────────────────────────────────────
--
-- No biometric template, no embedding, no raw audio, no customer content. An
-- audit row records that a match was attempted and how it scored. Copying the
-- template into the audit trail would defeat the encryption work in 037 by
-- leaving a plaintext copy in a table with different access rules.
--
-- The CHECK on `similarity` and `threshold` is the only numeric detail kept —
-- those are match statistics, not biometric data, and they are what makes a
-- threshold-tuning question answerable after the fact.
--
-- Rollback: rollback/038_biometric_audit_context_down.sql
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Expand
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE biometric_audit ADD COLUMN IF NOT EXISTS user_id      UUID REFERENCES users(id)      ON DELETE SET NULL;
ALTER TABLE biometric_audit ADD COLUMN IF NOT EXISTS tenant_id    UUID REFERENCES tenants(id)    ON DELETE CASCADE;
ALTER TABLE biometric_audit ADD COLUMN IF NOT EXISTS workspace_id UUID REFERENCES workspaces(id) ON DELETE SET NULL;

-- `tenant_id` CASCADEs where `user_id` and `workspace_id` SET NULL, and the
-- asymmetry is forced rather than chosen.
--
-- `SET NULL` on the tenant would clear `tenant_id` while `user_id` stayed set,
-- which violates `biometric_audit_attribution_implies_tenant` below — so
-- `DELETE FROM tenants` would fail outright for any tenant whose members had
-- ever been matched, and tenant offboarding would be impossible. CASCADE is
-- also the right answer on its own terms: a tenant's audit trail is the
-- tenant's, and it goes when they do, exactly as `organisations`, `workspaces`
-- and `tenant_mfa_policy` do.
--
-- Re-stated for a database that already has the column from an earlier run of
-- this migration, where `ADD COLUMN IF NOT EXISTS` is a no-op and would leave
-- the old `SET NULL` rule in place.
DO $$
DECLARE
    con text;
BEGIN
    SELECT c.conname INTO con
      FROM pg_constraint c
      JOIN pg_class t ON t.oid = c.conrelid
     WHERE t.relname = 'biometric_audit' AND c.contype = 'f'
       AND c.confrelid = 'tenants'::regclass
       AND c.confdeltype <> 'c';
    IF con IS NOT NULL THEN
        EXECUTE format('ALTER TABLE biometric_audit DROP CONSTRAINT %I', con);
        ALTER TABLE biometric_audit
            ADD CONSTRAINT biometric_audit_tenant_id_fkey
            FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE;
    END IF;
END
$$;
ALTER TABLE biometric_audit ADD COLUMN IF NOT EXISTS event_type   TEXT NOT NULL DEFAULT 'match_attempt';
ALTER TABLE biometric_audit ADD COLUMN IF NOT EXISTS enrollment_version INTEGER;

-- ON DELETE SET NULL, not CASCADE. Deleting a user removes their biometric
-- MATERIAL (037), but the security record that a match was attempted is
-- evidence — and evidence that disappears when the subject is deleted is not
-- evidence. The row survives with its identifying column cleared.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'biometric_audit_event_type_check') THEN
        ALTER TABLE biometric_audit ADD CONSTRAINT biometric_audit_event_type_check
            CHECK (event_type IN (
                'match_attempt', 'enrollment', 're_enrollment',
                'revocation', 'deletion', 'threshold_change'));
    END IF;

    -- A workspace context implies a tenant context. Recording a workspace
    -- without the tenant it belongs to would make the row unfilterable.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'biometric_audit_workspace_implies_tenant') THEN
        ALTER TABLE biometric_audit ADD CONSTRAINT biometric_audit_workspace_implies_tenant
            CHECK (workspace_id IS NULL OR tenant_id IS NOT NULL);
    END IF;

    -- ── Attribution implies a tenant context, and why ────────────────────────
    --
    -- A row naming a person but no tenant cannot be erased. That is not a
    -- policy choice, it is a PostgreSQL one: an UPDATE requires the resulting
    -- row to still satisfy the table's SELECT policy, and a row with both
    -- attribution columns cleared satisfies no arm of it — so redacting it is
    -- refused, and the row naming the person survives their deletion request.
    --
    -- The alternative was to make wholly-unattributed rows readable by any
    -- authenticated caller so that they stay visible after redaction. That
    -- trades a deletion problem for a disclosure one: those rows are the
    -- residue of OTHER people's failed authentications, and their session ids
    -- and match scores would become readable across tenants. Widening a read
    -- to make a delete work is the wrong direction on a biometric audit trail.
    --
    -- So the shape that cannot be erased is made unrepresentable instead.
    -- `tenant_id` stays nullable — a match that failed before any tenant was
    -- resolved is still recorded, and is exactly the event an investigation
    -- needs — but such a row carries no `user_id` either, so there is nothing
    -- about a person in it to erase.
    --
    -- **What this costs, stated plainly:** a pre-tenant authentication failure
    -- can no longer record WHO was being claimed. That is real investigative
    -- detail, and it is given up deliberately, because the alternative is
    -- holding a person's name in a row the system cannot honour a deletion
    -- request for. Once the authentication path resolves a tenant before
    -- recording the match — application work, not this migration — the
    -- attributed rows come back with their tenant and the detail returns.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'biometric_audit_attribution_implies_tenant') THEN
        ALTER TABLE biometric_audit ADD CONSTRAINT biometric_audit_attribution_implies_tenant
            CHECK (user_id IS NULL OR tenant_id IS NOT NULL);
    END IF;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Backfill
--
--    Existing rows carry only `session_id`, which cannot be resolved to a user
--    after the fact. They stay NULL, which is correct: an audit row whose
--    subject is unknown must say so rather than be attributed to whoever seems
--    likely. Attribution invented during a migration is worse than absent
--    attribution, because it looks authoritative.
--
--    In a single-owner install these rows all belong to the one owner, but that
--    inference does not survive the install becoming multi-tenant — so it is
--    not applied.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_biometric_audit_user
    ON biometric_audit (user_id, created_at DESC) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_biometric_audit_tenant
    ON biometric_audit (tenant_id, created_at DESC) WHERE tenant_id IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Append-only, enforced by the database
-- ─────────────────────────────────────────────────────────────────────────────

-- Append-only means DELETE is always refused and UPDATE is refused EXCEPT for
-- redaction — clearing an identifying column while every evidential column
-- stays byte-identical.
--
-- The exception is not a softening; it is what makes erasure possible at all.
-- `user_id` carries ON DELETE SET NULL, and PostgreSQL implements that as an
-- UPDATE. A trigger that refuses every UPDATE therefore makes DELETE FROM users
-- fail for any user with a biometric audit row — so the append-only guarantee
-- would have blocked the GDPR erasure path, and the first symptom would have
-- been a deletion request that could not be honoured.
--
-- The check is on the evidence, not on the caller: whatever issues the update,
-- the similarity, threshold, result, timestamps and session must be unchanged.
-- Redaction can only ever remove attribution, never alter what was recorded.
CREATE OR REPLACE FUNCTION biometric_audit_refuse_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        -- One carve-out: the row's tenant no longer exists, which means this
        -- DELETE is the CASCADE from `DELETE FROM tenants` and not an
        -- application trying to erase evidence. Without it the append-only rule
        -- and the CASCADE above contradict each other and tenant offboarding
        -- fails on the trigger instead of the constraint — a different error
        -- for the same impossibility.
        --
        -- `OLD.tenant_id IS NOT NULL` is load-bearing: with a NULL tenant the
        -- NOT EXISTS is trivially true, and platform-scope rows — the ones no
        -- tenant can see and therefore the ones with the least oversight —
        -- would become freely deletable.
        IF OLD.tenant_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM tenants WHERE id = OLD.tenant_id) THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'biometric_audit is append-only: DELETE is refused'
            USING HINT = 'Audit evidence is retained and redacted, never deleted. '
                         'To remove attribution, clear user_id. The whole trail '
                         'goes only when the tenant itself is deleted.';
    END IF;

    -- Every evidential column must be untouched.
    IF NEW.id           IS DISTINCT FROM OLD.id
       OR NEW.session_id  IS DISTINCT FROM OLD.session_id
       OR NEW.similarity  IS DISTINCT FROM OLD.similarity
       OR NEW.threshold   IS DISTINCT FROM OLD.threshold
       OR NEW.result      IS DISTINCT FROM OLD.result
       OR NEW.source      IS DISTINCT FROM OLD.source
       OR NEW.created_at  IS DISTINCT FROM OLD.created_at
       OR NEW.event_type  IS DISTINCT FROM OLD.event_type
    THEN
        RAISE EXCEPTION 'biometric_audit is append-only: evidential columns cannot change'
            USING HINT = 'Only redaction is permitted — clearing user_id, '
                         'tenant_id or workspace_id to NULL.';
    END IF;

    -- Attribution columns may only be cleared, never repointed. Repointing an
    -- audit row at a different user is falsification, not redaction.
    IF (NEW.user_id      IS NOT NULL AND NEW.user_id      IS DISTINCT FROM OLD.user_id)
       OR (NEW.tenant_id    IS NOT NULL AND NEW.tenant_id    IS DISTINCT FROM OLD.tenant_id)
       OR (NEW.workspace_id IS NOT NULL AND NEW.workspace_id IS DISTINCT FROM OLD.workspace_id)
    THEN
        RAISE EXCEPTION 'biometric_audit: attribution may be cleared, never reassigned'
            USING HINT = 'Set the column to NULL to redact. Pointing it at a '
                         'different subject would falsify the record.';
    END IF;

    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS biometric_audit_append_only ON biometric_audit;
CREATE TRIGGER biometric_audit_append_only
    BEFORE UPDATE OR DELETE ON biometric_audit
    FOR EACH ROW EXECUTE FUNCTION biometric_audit_refuse_mutation();

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Row-level security — APPEND_ONLY_AUDIT, subject OR tenant
--
--    Tenant-scoped for tenant-context rows; platform-scope rows (NULL tenant)
--    are invisible to every tenant because NULL = <uuid> is NULL, not true.
--
--    ── Why the subject is a second route in ─────────────────────────────────
--
--    A tenant-only predicate has a consequence that only shows up when somebody
--    asks to be deleted. Every row written today has `tenant_id` NULL, because
--    the match happens during authentication and no tenant is resolved yet — so
--    a tenant-scoped erasure reaches NONE of them, and the rows naming the
--    person survive their own deletion request. The erasure would report
--    success while redacting nothing.
--
--    Adding `user_id = app.user_id` fixes that at the source rather than with a
--    privileged side channel. It is also the correct access rule on its own
--    terms: these are records ABOUT the subject, and the subject reading them is
--    a data-protection right, not a leak. It widens nothing else — a tenant
--    administrator still matches only via `app.tenant_id`, so they still cannot
--    see another member's rows.
--
--    ── Why this is FOUR policies and not one ────────────────────────────────
--
--    Two PostgreSQL behaviours make a single `FOR ALL` policy wrong here, and
--    both fail in the direction that stays hidden until somebody asks to be
--    deleted.
--
--    **A `FOR ALL` policy checks an UPDATE's NEW row against its USING clause,
--    not only against WITH CHECK.** Redaction clears `user_id`, so the new row
--    matches neither arm of USING and the update is refused regardless of what
--    WITH CHECK permits.
--
--    **An UPDATE also requires the resulting row to satisfy the SELECT policy.**
--    This one survives splitting the policies — it was reproduced with the
--    UPDATE policy set to `WITH CHECK (true)`, which still refused, and widening
--    only the SELECT policy is what let the redaction through. A row that
--    becomes invisible when redacted therefore cannot be redacted at all. The
--    constraint `biometric_audit_attribution_implies_tenant` above is the answer
--    to that: the redacted row keeps its tenant and stays visible to it.
--
--    In both cases the error is `new row violates row-level security policy`,
--    which points at WITH CHECK and away from the actual cause.
--
--    Split per command, each clause says one thing:
--
--      SELECT  the subject, or the tenant the row belongs to. The subject arm
--              is a read right — a person may see records about themselves in a
--              tenant they are no longer working in.
--      INSERT  the same, plus a genuinely unattributed row — a match that
--              failed before any tenant was resolved. Written as
--              `tenant_id IS NULL AND user_id IS NULL` rather than the looser
--              `user_id IS NULL`, which would let a caller write an
--              unattributed row into ANOTHER tenant.
--      UPDATE  tenant-scoped on BOTH sides, deliberately narrower than SELECT.
--              Redaction is a tenant-scoped operation like every other erasure
--              step, the row stays in the tenant it belongs to, and the result
--              is still visible there — which is what makes the update legal at
--              all. Clearing `tenant_id` is refused by the WITH CHECK, so a row
--              cannot be redacted out of every tenant's sight.
--              Repointing attribution at a different person is refused by the
--              trigger rather than here: it needs to compare OLD with NEW, which
--              a policy cannot do and a trigger can. Each mechanism enforces the
--              part it is able to express.
--      DELETE  the same visibility as SELECT — deliberately not `true`. The
--              trigger refuses every DELETE, and a row the caller can see
--              reaches the trigger and gets that explicit refusal. A row they
--              cannot see is filtered out and the statement reports `DELETE 0`.
--              Loud where it can be, silent-but-harmless where it cannot.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE biometric_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE biometric_audit FORCE  ROW LEVEL SECURITY;

-- The old single-policy name is dropped explicitly: re-running this migration
-- over a database that has the earlier version must not leave it in place
-- beside the four below, where it would re-add the ALL-policy UPDATE behaviour.
DROP POLICY IF EXISTS tenant_isolation           ON biometric_audit;
DROP POLICY IF EXISTS subject_or_tenant_select   ON biometric_audit;
DROP POLICY IF EXISTS subject_or_tenant_insert   ON biometric_audit;
DROP POLICY IF EXISTS redaction_only_update      ON biometric_audit;
DROP POLICY IF EXISTS subject_or_tenant_delete   ON biometric_audit;

CREATE POLICY subject_or_tenant_select ON biometric_audit FOR SELECT
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
           OR user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);

CREATE POLICY subject_or_tenant_insert ON biometric_audit FOR INSERT
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                OR user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
                OR (tenant_id IS NULL AND user_id IS NULL));

CREATE POLICY redaction_only_update ON biometric_audit FOR UPDATE
    USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY subject_or_tenant_delete ON biometric_audit FOR DELETE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
           OR user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);

COMMENT ON TABLE biometric_audit IS
    'APPEND_ONLY_AUDIT. Match statistics only — never a template, an embedding '
    'or raw audio. tenant_id is nullable because a failed authentication may '
    'have no tenant; such rows are invisible to every tenant by NULL semantics.';
COMMENT ON COLUMN biometric_audit.tenant_id IS
    'NULL means genuinely platform-scope (authentication before a tenant was '
    'resolved), not "unknown". Never invented.';
COMMENT ON COLUMN biometric_audit.user_id IS
    'ON DELETE SET NULL — deleting a user removes their biometric material but '
    'not the record that a match was attempted.';
