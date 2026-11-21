-- =============================================================================
-- 031_pretenancy_isolation.sql — Blocker B1: the tables Phase 2 did not reach
--
-- **Ten** tables predate `023_tenancy.sql` and were not part of its retrofit.
-- They hold user data keyed by a TEXT owner column, with no `tenant_id` and no
-- row-level security:
--
--   cognitive_events                022_cognitive_loop.sql
--   cognitive_cycles                022_cognitive_loop.sql        ← found late
--   cognitive_goals                 022_cognitive_loop.sql        ← found late
--   cognitive_predictions           022_cognitive_loop.sql        ← found late
--   cognitive_prediction_outcomes   022_cognitive_loop.sql        ← found late
--   commitments                     018_lifeos_commitments_events.sql
--   envelope_members                019_privacy_envelopes.sql
--   files                           005_files.sql
--   lifeos_events                   018_lifeos_commitments_events.sql
--   privacy_envelopes               019_privacy_envelopes.sql     ← found late
--
-- `RELEASE_READINESS.md` R03 put it plainly: their queries *do* filter —
-- `WHERE user_id = $1` is present at every call site — *and that is precisely
-- the problem*. Application-level filtering is the dependency Phase 2 removed
-- for the other 34 tables, on the grounds that one forgotten WHERE clause is a
-- cross-tenant leak with nothing underneath it.
--
-- ── Five, not one: why the blocker undercounted by half ──────────────────────
--
-- `RELEASE_READINESS.md` R03 named five tables. There are ten.
--
-- `tests/readiness/test_cross_tenant_sweep.py` searched for tables having a
-- column literally named `user_id` and lacking `tenant_id`. The five it missed
-- — `privacy_envelopes` and four of the cognitive-loop tables — call their owner
-- column `owner_id`. A sweep whose entire purpose was finding unprotected owner
-- data missed half of it by spelling.
--
-- The sweep is widened in the same change that fixes the tables: it now searches
-- every owner-column name the schema uses, and a companion test requires each of
-- those names to actually exist so a typo cannot make the search vacuous again.
-- A gap found by a guard is worth much less than the same gap found and the
-- guard repaired.
--
-- Retrofitting `privacy_envelopes` here rather than later also avoids a
-- half-fix: `envelope_members` and `commitments` both reference
-- `privacy_envelopes(id)`, and scoping the children while leaving the parent
-- open would protect the membership list and not the envelope it describes.
--
-- ── Backfill, and the five append-only tables ────────────────────────────────
--
-- Every existing row belongs to the canonical private tenant — these are
-- Private-edition subsystems and no cloud write path exists (see
-- `docs/privacy/DATA_PROCESSING_INVENTORY.md`). The UUID below is the same one
-- 023 uses: uuid5(IRA_UUID_NAMESPACE, "tenant:private"), which
-- `ira/core/identity/principal.py` derives at runtime.
--
-- Five of the ten carry an append-only trigger that refuses UPDATE:
-- `lifeos_events`, `cognitive_events`, `cognitive_cycles`,
-- `cognitive_predictions` and `cognitive_prediction_outcomes`. A backfill is an
-- UPDATE, so those triggers are **momentarily disabled around the backfill and
-- re-enabled before COMMIT**. Stated loudly
-- because a migration quietly disabling an immutability trigger is exactly the
-- kind of thing that should never pass unremarked:
--
--   * it happens inside the transaction, so an abort restores the trigger;
--   * it re-enables unconditionally, and the migration re-asserts afterwards
--     that every one of them is enabled — if any is not, the migration RAISES
--     and rolls back rather than leaving an append-only table mutable;
--   * it touches only rows where `tenant_id IS NULL`, which after the first run
--     is none.
--
-- The alternative — dropping and recreating the trigger — has the same effect
-- with a longer window and no guarantee the recreation matches. This is the
-- narrower operation.
--
-- ── The application side, which is the actual work ───────────────────────────
--
-- Turning RLS on is one migration. Making it not break everything is the rest:
-- every read path for LifeOS, cognition, privacy envelopes and files used a
-- bare `acquire()` with no tenant bound, and under FORCE RLS an unbound
-- connection matches **no rows** — silently, without erroring. Those call sites
-- now use `core/identity/db_context.owner_connection`, which binds the private
-- tenant and **refuses in the cloud edition**, because none of these subsystems
-- is offered there and a cloud process reaching them is a bug that must fail
-- loudly rather than read the owner's data.
--
-- `ira/tests/tenancy/test_pretenancy_isolation_rls.py` writes and then reads
-- through each real application function against a real database, so a call
-- site that silently returns nothing fails CI rather than a user's evening.
--
-- Additive. Idempotent. Rollback: rollback/031_pretenancy_isolation_down.sql.
-- =============================================================================

BEGIN;

-- The canonical private tenant. NOT random — see 023_tenancy.sql.
-- The tenant UUID below was a `\set` variable. It is now a literal, because
-- `\set` is a psql METACOMMAND and the migration runner applies files through
-- asyncpg — a protocol driver, not psql — which rejects it outright:
--
--     asyncpg.exceptions.PostgresSyntaxError: syntax error at or near "\"
--
-- So this file could only ever be applied by `docker-entrypoint-initdb.d` on a
-- brand-new volume, and `python -m scripts.migrate apply` could not install a
-- fresh database at all. That was recorded as R-45 while the application still
-- applied its own migrations on boot through psql-seeded volumes; making the
-- migration command the only sanctioned path turns it into a blocker.
--
-- The value is unchanged: uuid5(IRA_UUID_NAMESPACE, 'tenant:private').

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Add the column. Nullable first, so an existing install migrates without
--    losing a row — the same order 023 used.
--
--    ON DELETE CASCADE throughout: unlike `trust_ledger`, none of these is an
--    audit record whose survival matters more than the tenant's departure.
--    `lifeos_events` and `cognitive_events` are append-only against the
--    *application*, which is a different claim from surviving the deletion of
--    the account they belong to.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE cognitive_events  ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;
ALTER TABLE cognitive_cycles  ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;
ALTER TABLE cognitive_goals   ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;
ALTER TABLE cognitive_predictions         ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;
ALTER TABLE cognitive_prediction_outcomes ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;
ALTER TABLE commitments       ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;
ALTER TABLE envelope_members  ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;
ALTER TABLE files             ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;
ALTER TABLE lifeos_events     ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;
ALTER TABLE privacy_envelopes ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Backfill. The five append-only tables need their triggers stood down for
--    the duration — see the header. Everything is inside this transaction.
-- ─────────────────────────────────────────────────────────────────────────────

UPDATE commitments       SET tenant_id = 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid WHERE tenant_id IS NULL;
UPDATE envelope_members  SET tenant_id = 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid WHERE tenant_id IS NULL;
UPDATE files             SET tenant_id = 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid WHERE tenant_id IS NULL;
UPDATE privacy_envelopes SET tenant_id = 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid WHERE tenant_id IS NULL;
UPDATE cognitive_goals   SET tenant_id = 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid WHERE tenant_id IS NULL;

ALTER TABLE lifeos_events                 DISABLE TRIGGER lifeos_events_immutable;
ALTER TABLE cognitive_events              DISABLE TRIGGER cognitive_events_immutable;
ALTER TABLE cognitive_cycles              DISABLE TRIGGER cognitive_cycles_immutable;
ALTER TABLE cognitive_predictions         DISABLE TRIGGER cognitive_predictions_immutable;
ALTER TABLE cognitive_prediction_outcomes DISABLE TRIGGER cognitive_outcomes_immutable;

UPDATE lifeos_events                 SET tenant_id = 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid WHERE tenant_id IS NULL;
UPDATE cognitive_events              SET tenant_id = 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid WHERE tenant_id IS NULL;
UPDATE cognitive_cycles              SET tenant_id = 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid WHERE tenant_id IS NULL;
UPDATE cognitive_predictions         SET tenant_id = 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid WHERE tenant_id IS NULL;
UPDATE cognitive_prediction_outcomes SET tenant_id = 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid WHERE tenant_id IS NULL;

ALTER TABLE lifeos_events                 ENABLE TRIGGER lifeos_events_immutable;
ALTER TABLE cognitive_events              ENABLE TRIGGER cognitive_events_immutable;
ALTER TABLE cognitive_cycles              ENABLE TRIGGER cognitive_cycles_immutable;
ALTER TABLE cognitive_predictions         ENABLE TRIGGER cognitive_predictions_immutable;
ALTER TABLE cognitive_prediction_outcomes ENABLE TRIGGER cognitive_outcomes_immutable;

-- Belt and braces: refuse to commit with either immutability trigger disabled.
-- A migration that left an append-only table mutable would be worse than one
-- that failed.
DO $$
DECLARE
    still_off TEXT;
BEGIN
    SELECT string_agg(c.relname || '.' || t.tgname, ', ')
      INTO still_off
      FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
     WHERE t.tgname IN ('lifeos_events_immutable', 'cognitive_events_immutable',
                        'cognitive_cycles_immutable', 'cognitive_predictions_immutable',
                        'cognitive_outcomes_immutable')
       AND t.tgenabled = 'D';
    IF still_off IS NOT NULL THEN
        RAISE EXCEPTION
            'Refusing to commit: append-only trigger(s) % are still disabled '
            'after the backfill. The whole migration is rolled back rather '
            'than leaving an immutable table mutable.', still_off;
    END IF;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Default the column from the bound tenant.
--
--    Phase 2's tables pass `tenant_id` explicitly at every INSERT. That is fine
--    for six tables written in one migration; it is a poor bet across ten tables
--    and roughly forty INSERT statements spread over LifeOS, cognition, privacy
--    envelopes and files, where the failure mode of missing one is a NOT NULL
--    violation in production on a code path nobody tested.
--
--    So the column defaults to the tenant bound on the connection — the same
--    expression the policy checks. Three consequences, all of them wanted:
--
--      * an INSERT that omits `tenant_id` gets the caller's own tenant, which
--        is the only value the WITH CHECK would have accepted anyway;
--      * an INSERT that passes one explicitly still works, and still has to
--        match the policy;
--      * an INSERT with **no** tenant bound resolves to NULL, hits NOT NULL and
--        **fails loudly**. That is the fail-closed direction: an unscoped write
--        is refused rather than landing somewhere arbitrary.
--
--    The DEFAULT is convenience. The NOT NULL and the policy are the guarantee,
--    and neither depends on it.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    t TEXT;
    scoped TEXT[] := ARRAY[
        'cognitive_events', 'cognitive_cycles', 'cognitive_goals',
        'cognitive_predictions', 'cognitive_prediction_outcomes',
        'commitments', 'envelope_members', 'files', 'lifeos_events',
        'privacy_envelopes'
    ];
BEGIN
    FOREACH t IN ARRAY scoped LOOP
        EXECUTE format(
            'ALTER TABLE %I ALTER COLUMN tenant_id SET DEFAULT '
            'NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid', t);
    END LOOP;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Constrain. After this a row cannot be written without a tenant, which is
--    what makes the policy below load-bearing rather than advisory.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE cognitive_events  ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE cognitive_cycles  ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE cognitive_goals   ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE cognitive_predictions         ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE cognitive_prediction_outcomes ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE commitments       ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE envelope_members  ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE files             ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE lifeos_events     ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE privacy_envelopes ALTER COLUMN tenant_id SET NOT NULL;

-- The existing per-user indexes stay; these lead with the tenant, which is the
-- column every query now filters on first.
CREATE INDEX IF NOT EXISTS idx_cognitive_events_tenant  ON cognitive_events  (tenant_id, at DESC);
CREATE INDEX IF NOT EXISTS idx_cognitive_cycles_tenant  ON cognitive_cycles  (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cognitive_goals_tenant   ON cognitive_goals   (tenant_id, state);
CREATE INDEX IF NOT EXISTS idx_cognitive_predictions_tenant ON cognitive_predictions (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cognitive_outcomes_tenant    ON cognitive_prediction_outcomes (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_commitments_tenant       ON commitments       (tenant_id, deadline);
CREATE INDEX IF NOT EXISTS idx_envelope_members_tenant  ON envelope_members  (tenant_id, user_id);
CREATE INDEX IF NOT EXISTS idx_files_tenant             ON files             (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_lifeos_events_tenant     ON lifeos_events     (tenant_id, at);
CREATE INDEX IF NOT EXISTS idx_privacy_envelopes_tenant ON privacy_envelopes (tenant_id, owner_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. Row-level security. ENABLE *and* FORCE, with the same policy shape as
--    every other scoped table — so the sweep in
--    tests/readiness/test_cross_tenant_sweep.py sees no difference between
--    these and the 34 Phase 2 protected, which is the point.
--
--    FORCE matters as much as ENABLE: without it the table's owner bypasses the
--    policy, and in a single-role deployment the owner is the application.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    t TEXT;
    scoped TEXT[] := ARRAY[
        'cognitive_events',
        'cognitive_cycles',
        'cognitive_goals',
        'cognitive_predictions',
        'cognitive_prediction_outcomes',
        'commitments',
        'envelope_members',
        'files',
        'lifeos_events',
        'privacy_envelopes'
    ];
BEGIN
    FOREACH t IN ARRAY scoped LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format($f$
            CREATE POLICY tenant_isolation ON %I
                USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        $f$, t);
    END LOOP;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 6. Append-only means "not revisable", not "outlives the account".
--
-- Found by running the erasure rather than reasoning about it: adding these
-- tables to the `account` scope made account deletion FAIL, because
-- `lifeos_events_no_mutate` refuses DELETE as well as UPDATE. A user asking to
-- be deleted would have got an error, not a deletion.
--
-- `027_realtime.sql` already settled this question for the same shape of table:
--
--     realtime_events is a measurement log: rows may be inserted and (for
--     erasure) deleted, but never edited. A latency figure changed after the
--     fact is worse than no figure at all.
--
-- The same reasoning applies here. What append-only protects is the record
-- being *rewritten* — an event log whose entries can be edited proves nothing.
-- It was never meant to mean an account cannot be closed, and neither of these
-- logs is an accountability record about a third party (the trust ledger is,
-- and it is redacted rather than deleted — ADR-0004, unchanged).
--
-- So the triggers narrow from UPDATE-OR-DELETE to UPDATE. Deleting is now
-- possible; editing is still not, and the tests plant an UPDATE to prove it.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION lifeos_events_no_mutate()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'This log is append-only: rows may be inserted and (for erasure) '
        'deleted, but never edited. An event log whose entries can be rewritten '
        'proves nothing about what happened.';
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    spec TEXT[];
    pairs TEXT[][] := ARRAY[
        ARRAY['lifeos_events',                 'lifeos_events_immutable'],
        ARRAY['cognitive_events',              'cognitive_events_immutable'],
        ARRAY['cognitive_cycles',              'cognitive_cycles_immutable'],
        ARRAY['cognitive_predictions',         'cognitive_predictions_immutable'],
        ARRAY['cognitive_prediction_outcomes', 'cognitive_outcomes_immutable']
    ];
BEGIN
    FOREACH spec SLICE 1 IN ARRAY pairs LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', spec[2], spec[1]);
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE ON %I '
            'FOR EACH ROW EXECUTE FUNCTION lifeos_events_no_mutate()',
            spec[2], spec[1]);
    END LOOP;
END
$$;

COMMIT;
