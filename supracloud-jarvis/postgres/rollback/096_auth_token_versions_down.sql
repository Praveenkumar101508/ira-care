-- =============================================================================
-- rollback/096_auth_token_versions_down.sql
--
-- Reverses 096: the per-account token version goes back to Redis.
--
-- ── Run the copy-back FIRST ─────────────────────────────────────────────────
--
--     python supracloud-jarvis/scripts/token_versions_to_redis.py
--
-- writes every row of auth_token_versions to Redis as
-- `ira:token_ver:<username>`, keeping the higher value where a key already
-- exists. Dropping the table without it would put every account back at the
-- version Redis happens to hold, which for an account whose key was lost is 0:
-- every token issued before its last "log out everywhere" would work again.
-- That is the defect 096 fixed, and the rollback must not reintroduce it.
--
-- ── What this gives back, said plainly ──────────────────────────────────────
--
-- The behaviour before 096: the version lives only in Redis, and a lost key
-- revives old tokens. The code that reads PostgreSQL must be reverted with this
-- file; the application refuses requests (503) while it expects a table that
-- is gone.
--
-- The `users` rows the start-up seeder created for legacy delegated accounts
-- are KEPT. Dropping them would orphan the credentials and memberships that
-- refer to those accounts.
-- =============================================================================

BEGIN;

-- The table's policies and trigger go with it; the function is separate.
DROP TABLE IF EXISTS public.auth_token_versions;
DROP FUNCTION IF EXISTS public.auth_token_versions_monotonic();

COMMIT;
