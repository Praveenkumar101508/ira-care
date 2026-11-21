-- =============================================================================
-- 075_usage_outcomes.sql — Phase 8, batch P8-04.
--
-- Two values the code writes and the schema rejects.
--
-- ── The defect, and it is older than this phase ────────────────────────────
--
-- `029_commercial.sql` closed `model_usage.outcome` to four values:
--
--     CHECK (outcome IN ('ok', 'refused_privacy', 'refused_quota', 'error'))
--
-- and `core/models/gateway.py` writes a fifth, `refused_policy`, in the two
-- places that matter most:
--
--   * the REGISTRY refused — an unapproved model was stopped before any
--     provider was called (P5-07);
--   * the IDENTITY check refused — a provider ran something other than the
--     model the registry approved, and its answer was discarded (MOD-03).
--
-- Both rows violate the CHECK. `core/models/store.record_usage` is
-- deliberately fail-soft, so the INSERT raises, the warning is logged and the
-- row is dropped. The two events an operator would most want in the usage
-- table — "we blocked an unapproved model" and "a provider substituted one" —
-- have never been recorded, and nothing failed loudly enough to say so.
--
-- Phase 8 found it by wiring the gateway to the live path: the moment real
-- traffic could produce those outcomes, "silently dropped" stopped being
-- theoretical. It is fixed here rather than in `029`, which is applied and
-- immutable.
--
-- ── The second value ───────────────────────────────────────────────────────
--
-- `abandoned` is new, and it exists because streaming does. A stream can end
-- three ways: exhausted, failed, or the caller stopped reading — a closed tab,
-- a cancelled request. The third is not success and it is not an error: the
-- provider did the work and nobody read the answer.
--
-- Mapping it onto `error` was the cheaper option and would have made a healthy
-- deployment look like a failing one. Mapping it onto `ok` would have made a
-- tenant who abandons a thousand streams invisible. It gets its own value.
--
-- ── What this migration does NOT relax ─────────────────────────────────────
--
-- `refusals_consume_nothing` is extended to cover `refused_policy`, so a
-- refusal that claims to have consumed tokens is still impossible. It is NOT
-- extended to `abandoned`: an abandoned stream really did consume provider
-- work, and forbidding it from recording that would build the wrong invariant
-- into the billing record.
--
-- `credentials_never_left_the_machine` and `local_only_stayed_local` are
-- untouched. Both are scoped to `outcome = 'ok'`, and neither new value is
-- `ok`, so their guarantee is unchanged in both directions.
-- =============================================================================

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'model_usage') THEN
        RAISE EXCEPTION
            '075 REFUSING: model_usage does not exist. 029_commercial.sql has '
            'not been applied, so there is nothing to widen and applying this '
            'would leave a half-built commercial schema.';
    END IF;
END $$;

ALTER TABLE model_usage
    DROP CONSTRAINT IF EXISTS model_usage_outcome_check;

ALTER TABLE model_usage
    ADD CONSTRAINT model_usage_outcome_check CHECK (
        outcome IN ('ok', 'refused_privacy', 'refused_quota', 'error',
                    -- The registry or the identity check refused. Written by
                    -- gateway.complete() since Phase 5 and rejected until now.
                    'refused_policy',
                    -- A stream the caller stopped reading. Phase 8.
                    'abandoned')
    );

-- A refusal consumed no tokens and cost nothing — now including the two
-- refusals that were being dropped. `abandoned` is deliberately absent: that
-- one DID consume provider work.
ALTER TABLE model_usage
    DROP CONSTRAINT IF EXISTS refusals_consume_nothing;

ALTER TABLE model_usage
    ADD CONSTRAINT refusals_consume_nothing CHECK (
        outcome NOT IN ('refused_privacy', 'refused_quota', 'refused_policy')
        OR (prompt_tokens = 0 AND completion_tokens = 0 AND cost = 0)
    );

COMMENT ON COLUMN model_usage.outcome IS
    'ok | refused_privacy | refused_quota | refused_policy | error | abandoned. '
    'refused_policy is the registry or identity check stopping an unapproved '
    'or substituted model; it was written by the gateway from Phase 5 and '
    'rejected by the CHECK until 075. abandoned is a stream the caller stopped '
    'reading — provider work happened and no answer was delivered.';

-- ── Prove the widened vocabulary actually accepts what the code writes ──────
--
-- A CHECK that was edited and a CHECK that works look identical in a diff.
--
-- Two checks, and the weaker one is unconditional on purpose. The strong one
-- INSERTs a row of each new outcome and rolls it back; it needs a tenant AND
-- the privilege to write through `model_usage`'s row-level security, and the
-- MIGRATOR HAS NEITHER. That is not a defect to work around — a migration role
-- that could write tenant rows would be a much worse problem than an unproven
-- probe — so when the insert is refused for want of privilege, this says so and
-- falls back to reading the constraint.
--
-- Found the honest way: the first version of this probe assumed it could
-- insert, and the whole upgrade-path suite went red with
-- `new row violates row-level security policy for table "model_usage"` the
-- first time the chain was applied as `ira_migrator` rather than as `postgres`.
DO $$
DECLARE
    definition TEXT;
    probe_tenant UUID;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO definition
      FROM pg_constraint WHERE conname = 'model_usage_outcome_check';

    IF definition IS NULL
       OR definition NOT LIKE '%refused_policy%'
       OR definition NOT LIKE '%abandoned%' THEN
        RAISE EXCEPTION
            '075 FAILED: model_usage.outcome does not accept the values the '
            'gateway writes. Definition: %', COALESCE(definition, '<absent>');
    END IF;

    SELECT id INTO probe_tenant FROM tenants LIMIT 1;
    IF probe_tenant IS NULL THEN
        RAISE NOTICE '075: no tenant to insert-probe with; the constraint text '
                     'is verified, the insert is not.';
        RETURN;
    END IF;

    BEGIN
        INSERT INTO model_usage (tenant_id, provider_id, locality, tier,
                                 sensitivity, privacy_mode, outcome)
        VALUES (probe_tenant, 'ollama-local', 'local', 'fast', 'normal',
                'local_first', 'refused_policy'),
               (probe_tenant, 'ollama-local', 'local', 'fast', 'normal',
                'local_first', 'abandoned');
        RAISE EXCEPTION 'probe-ok';
    EXCEPTION
        WHEN check_violation THEN
            RAISE EXCEPTION
                '075 FAILED: model_usage still rejects the outcomes the '
                'gateway writes, despite a constraint definition that names '
                'them.';
        WHEN insufficient_privilege THEN
            -- The migrator cannot write through RLS, which is correct.
            RAISE NOTICE '075: insert-probe skipped (no privilege through '
                         'model_usage RLS); the constraint text is verified.';
        WHEN OTHERS THEN
            IF SQLERRM <> 'probe-ok' THEN
                RAISE;
            END IF;
    END;
END $$;

COMMIT;
