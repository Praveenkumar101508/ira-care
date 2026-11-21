#!/usr/bin/env bash
#
# Rollback migration applies cleanly
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# PRECONDITION: this one is NOT self-contained. It runs against the shared
# `iratest` database and needs the tenancy suites to have populated it first —
# several of the guards below refuse only while multi-tenant fixture data
# exists, and without it they have nothing to refuse. See README.md.
# A migration whose rollback has never been executed is a rollback plan,
# not a rollback. This proves the down script runs, then re-applies the
# up script so the two are known to compose.
#
# This step has now been stale THREE times, for the same reason every
# time: a batch added migrations and nobody extended the cycle, because
# CI was not running on the branch that added them. It stopped at 032
# while the chain was 044; it then stopped at 044 while the chain was
# 046, and the first run of this job against a Batch 5B branch failed on
# exactly that — 039's rollback could not drop `workspaces` with
# seventeen `*_workspace_id_fkey` constraints from 045 still pointing at
# it. It then stopped at 050 while the chain was 051, and the first run
# that ever executed against a tree carrying 051 failed the same way:
# `notifications_workspace_id_fkey` and
# `notifications_workspace_in_tenant_fkey` still pointing at
# `workspaces`. Every occurrence was found by the first execution rather
# than by review, which is an argument about CI coverage and not about
# this list.
#
# Neither gap was cosmetic: 023 creates
# `tenant_memberships`, and 038 and 040 hang a constraint and two views
# off it, so rolling 023 back while 033–044 were still applied failed on
# a dependency that could not have existed when the step was authored.
# The newest layers therefore come off FIRST, in reverse order, and go
# back on last.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: the repository root`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../../.."

# ── State handed to chain-recompose.sh ──────────────────────────────────────
# This script captures the chain-era boundary before 077 is re-applied, and
# chain-recompose.sh asserts it did not move. In CI that value travelled through
# $GITHUB_ENV, which only exists inside a runner; CHAIN_STATE makes the handoff
# work the same way when the two are run by hand, and the CI write is kept so
# nothing about the runner behaviour changes.
CHAIN_STATE="${CHAIN_STATE:-${TMPDIR:-/tmp}/ira-chain-state.env}"
: > "${CHAIN_STATE}"
export CHAIN_STATE

cd supracloud-jarvis/postgres
# 046 and 045 come off FIRST — they are the newest layers, and 045
# gives all seventeen content tables a `workspace_id` REFERENCES
# workspaces. Until those columns go, 039's rollback cannot drop
# `workspaces` at all: PostgreSQL lists seventeen dependent foreign
# keys and refuses. That is what failed here the first time this job
# ever ran against a branch carrying Batch 5B.
#
# 046's rollback refuses to restore the GLOBAL UNIQUE constraints
# while duplicate values exist, which is correct — restoring them
# would mean deleting or renaming somebody's rows — but the suites
# above deliberately create two tenants holding the same agent name,
# external id and project slug, because that is the R-65 defect 046
# exists to fix. Clearing those fixture rows is a test teardown.
# Children first: edges and reminders reference entities and tasks.
for t in edges entities reminders decisions calendar_events agents projects; do
  psql -h localhost -U postgres -d iratest -c "DELETE FROM $t"
done
# 052 is the newest layer and comes off first. Unlike 051's, its guard
# asks the precise question — how many tenants hold a TOTP enrolment,
# not how many tenants exist — so it can genuinely reverse here once
# the fixture enrolments are cleared. The refusal path is proved in the
# dedicated step above, on a database seeded with two tenants' worth.
#
# The rows go first for the same reason every other DELETE in this step
# exists: they are disposable test enrolments, and with none present
# the re-apply at the end has nothing to attribute and nothing to
# refuse. 052 refuses rather than guessing a tenant for a second
# factor, which is correct and is why the table is emptied rather than
# the guard being loosened.
psql -h localhost -U postgres -d iratest -c "DELETE FROM totp_secrets"
# 054 comes off after 055 and 056. Its guard
# refuses while any account holds a credential — dropping the table
# destroys the only copy of those password hashes, which cannot be
# reconstructed — so the registration fixtures are cleared first. The
# refusal itself is proved on its own database in the dedicated step
# above; both halves are real somewhere.
# 055 is newer than 054 and comes off before it. This step's own comment
# above records it going stale three times; this was the fourth, and
# found the same way — by the first CI execution against a branch
# carrying the new migration, not by review. `tenant_invitations`
# REFERENCES users(id) ON DELETE RESTRICT via `invited_by`, so 054's
# rollback cannot drop `users`-dependent state while invitations
# exist.
#
# Its guard refuses while any invitation has been ACCEPTED, because
# those rows are the audit trail for memberships that survive the
# drop. The suites above accept invitations deliberately, so the
# fixture rows are cleared first — a test teardown, exactly as the
# credential DELETE below is. The refusal itself is proved on its own
# disposable database in the dedicated step above, so both halves of
# the script run somewhere and neither is assumed.
#
# 056 is the newest layer of all, so it comes off FIRST — and this is
# the ONE comment in this step that may say so. Three consecutive
# migrations each claimed to be "the newest layer" here, because every
# batch added its own note and none corrected the one above it. That
# prose is what a reader checks the ordering against, so when it says
# three different things it says nothing, and the ordering has to be
# rederived from scratch every time. `scripts/migration_workflow.py`
# now checks the executable ordering mechanically; these comments only
# have to stay honest.
#
# Its guard refuses while any
# UNUSED recovery code exists, and the suites above issue them — so
# the fixture rows are cleared first, exactly as the invitation and
# credential DELETEs below are. The refusal itself is proved on its
# own disposable database in the dedicated step above.
# 066 is the newest layer of all, so it comes off FIRST — and this is
# the ONE comment in this step that may say so.
#
# Its rollback REFUSES while any job run has not finished, because
# dropping the table then destroys the only record that the pass
# began. The suites above leave completed runs only, so nothing needs
# clearing here; the refusal is proved on its own disposable database
# in the dedicated step above.
# 077 is deliberately ABSENT from this cycle, and its file says why:
# IRREVERSIBLE-ONCE-USED. By the time this step runs, the suites above
# have written ledger rows above the recorded chain-era boundary, so
# `rollback/077` refuses — correctly, because a drop-and-re-apply would
# stamp a LATER boundary and classify tenant-era records as legacy.
# Both halves of its cycle are proved on a disposable database in the
# dedicated 077 step above, where the boundary is still unused.
#
# 084 is the newest layer, so it comes off FIRST. Like 083 it widens a
# CHECK constraint and destroys nothing, so it genuinely reverses.
#
# `rollback/084` REFUSES while any realtime_sessions row records
# `unknown`. Unlike 083's guard this one CAN be cleared —
# `realtime_sessions` is not append-only — but no suite above ends a
# session through the route with an unrecognised reason against this
# database, so there is nothing to clear and the peel is unguarded on
# purpose. If one ever does, this step fails with 084's own message,
# which names the rows and the two ways out.
# 085 comes off FIRST — it is the newest layer. It drops a partial
# unique index and a nullable column on `business_events`, so unlike
# most peels in this step it has no guard and nothing to clear: no
# row is deleted and no constraint is restored over existing data.
# What it DOES lose is the duplicate protection from FINDING AQ, which
# is why the re-apply below asserts the index is back.
# 098 comes off FIRST — it is the newest layer. A data migration: it removes
# the memory embedder, the memory reranker and the voice-print encoder from the
# allowlist and their licence evidence from the register. It REFUSES while any
# of those rows carries a human licence determination; nothing in the suites
# above records one, so there is nothing to clear.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/098_memory_and_speaker_model_pins_down.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT (SELECT count(*) FROM model_pull_allowlist
            WHERE identifier IN ('BAAI/bge-large-en-v1.5', 'BAAI/bge-reranker-v2-m3',
                         'speechbrain/spkrec-ecapa-voxceleb'))
        + (SELECT count(*) FROM model_licence_register
            WHERE identifier IN ('BAAI/bge-large-en-v1.5', 'BAAI/bge-reranker-v2-m3',
                         'speechbrain/spkrec-ecapa-voxceleb'))" \
  | grep -qx 0 \
  || { echo "::error::rollback/098 left memory/voice-print artefact rows behind"; exit 1; }

# 097 comes off next, under 098 (LangGraph's checkpoint
# tables). It drops all four; the conversation-graph state the suites above
# wrote goes with them, which is what its header says a rollback deletes.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/097_langgraph_checkpoint_tables_down.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT to_regclass('public.checkpoints') IS NULL
          AND to_regclass('public.checkpoint_blobs') IS NULL
          AND to_regclass('public.checkpoint_writes') IS NULL
          AND to_regclass('public.checkpoint_migrations') IS NULL" \
  | grep -qx t \
  || { echo "::error::rollback/097 left a LangGraph checkpoint table behind"; exit 1; }
# 096 comes off next (durable logout). It drops
# auth_token_versions and its trigger function. In production the copy-back
# (scripts/token_versions_to_redis.py) runs before it; this database has no
# live sessions to keep.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/096_auth_token_versions_down.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT to_regclass('public.auth_token_versions') IS NULL
          AND to_regprocedure('public.auth_token_versions_monotonic()') IS NULL" \
  | grep -qx t \
  || { echo "::error::rollback/096 left auth_token_versions or its trigger function behind"; exit 1; }
# 095 comes off next. It clears every Group B author
# its single-user backfill wrote (listed in attribution_backfill_095), drops
# that record, and drops the five Group C owner_user_id columns (ADR-0010).
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/095_owners_and_single_user_backfill_down.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT count(*) FROM information_schema.columns WHERE column_name = 'owner_user_id'
     AND table_name IN ('tasks','people','notifications','briefings','heartbeat_surfaced')" \
  | grep -qx 0 \
  || { echo "::error::rollback/095 left an owner_user_id column behind"; exit 1; }
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT to_regclass('public.attribution_backfill_095') IS NULL" \
  | grep -qx t \
  || { echo "::error::rollback/095 left attribution_backfill_095 behind"; exit 1; }

# 094 comes off next. A data migration: it moves
# every fact 094 put in the Memory Vault back to the name it came from. No
# suite above saves a fact with the owner's chat, so there is normally
# nothing to move; the step proves the script runs and leaves no marker.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/094_remembered_facts_into_vault_down.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT count(*) FROM memory_embeddings WHERE metadata ? 'moved_to_vault'" \
  | grep -qx 0 \
  || { echo "::error::rollback/094 left a moved fact in the Memory Vault"; exit 1; }

# 093 comes off next. It restores 049's four
# workspace-only policies on `conversations` and `messages` and drops
# `owner_user_id`. Nothing is deleted.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/093_conversation_ownership_down.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT count(*) FROM information_schema.columns
    WHERE table_name IN ('conversations', 'messages') AND column_name = 'owner_user_id'" \
  | grep -qx 0 \
  || { echo "::error::rollback/093 left owner_user_id behind"; exit 1; }

# 092 comes off next (091 has no rollback, by design: its deny records must
# never be dropped). It drops the two private-account columns from
# `tenant_memberships`. It REFUSES while any membership
# carries a private role, because that is a granted account the owner could
# neither use nor re-grant afterwards. tests/tenancy/ purges the accounts it
# grants and test_zz_no_fixture_residue.py asserts none is left, so there is
# nothing to clear here.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/092_private_account_memberships_down.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT count(*) FROM information_schema.columns
    WHERE table_name = 'tenant_memberships' AND column_name IN ('private_role', 'person_id')" \
  | grep -qx 0 \
  || { echo "::error::rollback/092 left a private-account column on tenant_memberships"; exit 1; }

# 090 comes off next. A data migration: it removes
# the five voice artefacts from the allowlist and their licence evidence from
# the register. It REFUSES while any of those rows carries a human licence
# determination; nothing in the suites above records one, so there is nothing
# to clear.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/090_voice_model_pins_down.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT (SELECT count(*) FROM model_pull_allowlist
            WHERE identifier IN ('Systran/faster-whisper-small', 'Supertone/supertonic-3'))
        + (SELECT count(*) FROM model_licence_register
            WHERE identifier IN ('Systran/faster-whisper-small', 'Supertone/supertonic-3'))" \
  | grep -qx 0 \
  || { echo "::error::rollback/090 left voice artefact rows behind"; exit 1; }

# 089 comes off next. It drops the nine evidence
# tables (empty here: the F1 suites delete their tenants, and the down script
# refuses while any evidence version exists) and removes the
# `workspace.approve_changes` capability, restoring 040's capability CHECK.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/089_evidence_repair_down.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT count(*) FROM pg_tables WHERE tablename LIKE 'evidence\\_%'" \
  | grep -qx 0 \
  || { echo "::error::rollback/089 left evidence tables in place"; exit 1; }
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT count(*) FROM workspace_role_capabilities WHERE capability = 'workspace.approve_changes'" \
  | grep -qx 0 \
  || { echo "::error::rollback/089 left the workspace.approve_changes capability behind"; exit 1; }

# 088 comes off next. It drops one partial
# full-text index on `memory_embeddings` and nothing else, so it has no
# guard and nothing to clear. What it loses is speed, not rows: the
# re-apply at the end of this step asserts the index comes back with the
# exact definition the library's keyword query depends on.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/088_technical_library_fulltext_down.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT count(*) FROM pg_indexes
    WHERE indexname = 'idx_memory_embeddings_techlib_fts'" \
  | grep -qx 0 \
  || { echo "::error::rollback/088 left idx_memory_embeddings_techlib_fts in place"; exit 1; }

# 087 comes off FIRST — it is the newest layer. It drops
# billing_checkout_sessions and two columns on tenant_subscriptions.
# Its rollback REFUSES while any checkout session is still open, on
# the ground that an open session is a payment that may yet complete
# and the webhook completing it would have nothing to attribute
# itself to. Nothing above leaves one open against this database, so
# the peel is unguarded here; if one ever does, this step fails with
# 087's own message, which names the count and the way out.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/087_billing_provider_lifecycle_down.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT count(*) FROM information_schema.tables
    WHERE table_name = 'billing_checkout_sessions'" \
  | grep -qx 0 \
  || { echo "::error::rollback/087 left billing_checkout_sessions in place"; exit 1; }

# 086 next. Its rollback REFUSES while a tenant holds a live ira_pro
# subscription — removing the plan would leave those rows naming a
# plan that does not exist, and no invoice issued under it could be
# reconstructed. Several suites above create such subscriptions, so
# they are cancelled first, DELIBERATELY and in the open: that is the
# commercial act 086's rollback says must happen before the schema
# one, done here because iratest is a test database.
#
# The refusal itself is proved in its own dedicated step, on a
# disposable database, so clearing the rows here does not cost the
# guarantee.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -q -c \
  "DELETE FROM tenant_subscriptions WHERE plan_id = 'ira_pro'"
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/086_ira_pro_plan_and_price_down.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT count(*) FROM billing_plan_prices WHERE plan_id = 'ira_pro'" \
  | grep -qx 0 \
  || { echo "::error::rollback/086 left the ira_pro price in place"; exit 1; }

psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/085_business_events_dedupe_key_down.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT count(*) FROM pg_indexes
    WHERE indexname = 'uq_business_events_dedupe'" \
  | grep -qx 0 \
  || { echo "::error::rollback/085 left uq_business_events_dedupe in place"; exit 1; }
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/084_realtime_unknown_end_reason_down.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT pg_get_constraintdef(oid) FROM pg_constraint
    WHERE conname = 'realtime_sessions_end_reason_check'" \
  | grep -qv "'unknown'" \
  || { echo "::error::rollback/084 left unknown in the end_reason vocabulary"; exit 1; }
# 083 is next — and unlike 081 and 082 it genuinely reverses here: it
# widens a CHECK constraint and destroys nothing.
#
# `rollback/083` REFUSES while any biometric_audit row records
# `consent_missing`. Unlike every other guarded rollback in this step,
# that one CANNOT be unblocked by clearing fixture rows — `041` makes
# `biometric_audit` append-only and refuses DELETE outright. The first
# version of this step tried, and CI said so:
#
#   ERROR: biometric_audit is append-only: DELETE is refused
#
# So there is no teardown here, deliberately. No suite above records a
# `consent_missing` refusal today, and if one ever does this peel will
# fail loudly with 083's own message — which is the correct outcome
# rather than a bug in this step. The refusal and its permanence are
# both proved on a disposable database in `Migration 083` above.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/083_biometric_consent_reason_down.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT pg_get_constraintdef(oid) FROM pg_constraint
    WHERE conname = 'biometric_audit_failure_reason_check'" \
  | grep -qv consent_missing \
  || { echo "::error::rollback/083 left consent_missing in the vocabulary"; exit 1; }
# 081 is the newest layer this cycle CANNOT peel, so it comes next.
#
# `rollback/081` is NOT peeled here, and 081 declares
# IRREVERSIBLE-ONCE-USED to say so. It refuses on a deployment with
# more than one tenant — reversing it re-opens finding G — and this
# database holds thousands by now, so the cycle would hit the refusal
# and stop. Both halves of the script are proved on a disposable
# single-tenant database in the dedicated `Migration 081` step above,
# where the reversal is a legitimate operation.
#
# What still has to happen here is the FIXTURE TEARDOWN. 081's two
# foreign keys point at `workspaces`, and `033`'s rollback cannot drop
# that table while they exist — which is exactly how this job first
# went red: `cannot drop table workspaces because other objects depend
# on it ... constraint briefings_workspace_in_tenant_fkey`. Dropping
# them is not a rollback, which is why the columns, policies and RLS
# are deliberately left alone; they go back on after the re-apply
# below, and the recompose step asserts both definitions against what
# 081 declares rather than merely counting them.
# 082's FKs point at `workspaces` too, and are torn down for the same
# reason and on the same terms — a fixture teardown, not a rollback.
psql -h localhost -U postgres -d iratest -c "DELETE FROM heartbeat_surfaced"
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -c \
  "ALTER TABLE heartbeat_surfaced
       DROP CONSTRAINT IF EXISTS heartbeat_surfaced_workspace_in_tenant_fkey,
       DROP CONSTRAINT IF EXISTS heartbeat_surfaced_tenant_id_fkey"
psql -h localhost -U postgres -d iratest -c "DELETE FROM briefings"
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -c \
  "ALTER TABLE briefings
       DROP CONSTRAINT IF EXISTS briefings_workspace_in_tenant_fkey,
       DROP CONSTRAINT IF EXISTS briefings_tenant_id_fkey"
# 080 is next.
#
# `rollback/080` REFUSES once any payment event has been acted on or
# is awaiting a person, so the fixtures are cleared first. The DELETE
# goes through `session_replication_role` because 080's own trigger
# refuses to delete an arrival — the fixtures are removed by switching
# the control off for ONE CONNECTION, not by weakening it.
psql -h localhost -U postgres -d iratest -c \
  "SET session_replication_role = replica; DELETE FROM payment_events"
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/080_payment_events_down.sql
# 079 is next.
#
# `rollback/079` REFUSES once any invoice has been ISSUED, which is
# correct — dropping the table deletes the record of what a customer
# was told they owe while the obligation continues to exist. The
# tenancy suites above issue invoices, so their rows are cleared
# first, the same way 025's, 031's and 078's are. The DELETE has to
# go through `session_replication_role` because 079's own trigger
# refuses to delete an issued invoice — the fixtures are removed by
# switching the control off for ONE CONNECTION, not by weakening it.
psql -h localhost -U postgres -d iratest -c \
  "SET session_replication_role = replica; DELETE FROM invoice_lines; DELETE FROM invoices"
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/079_invoices_down.sql
# 078 is next, and must come off AFTER 079: `invoices.subscription_id`
# references `tenant_subscriptions`, so peeling 078 first would fail
# on a foreign key rather than on anything meaningful.
#
# `rollback/078` REFUSES while any subscription or price row exists,
# which is correct — a subscription is the record of a commercial
# relationship and dropping it deletes the record while the
# relationship continues. The suites above create subscriptions, so
# the fixtures are cleared first, the same way 025's and 031's are.
# Prices are cleared too although nothing seeds them: if a future
# batch ever does, this cycle must fail on THAT rather than here.
psql -h localhost -U postgres -d iratest -c "DELETE FROM tenant_subscriptions"
psql -h localhost -U postgres -d iratest -c "DELETE FROM billing_plan_prices"
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/078_billing_plans_and_entitlements_down.sql
# 076 is next. It only REVOKES, so peeling it
# restores a privilege rather than dropping an object — which is why
# its rollback needs no refusal and why the recompose step below
# checks a privilege rather than a table.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/076_provider_disclosure_is_not_writable_down.sql
# 075 is next — it widens `model_usage.outcome`
# by two values. Its rollback REFUSES while any row carries one of
# them, which is correct and would stop this cycle dead on a database
# with real traffic; here the table is empty, so it narrows cleanly.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/075_usage_outcomes_down.sql
# 074 is next — a data migration on the licence
# register. It comes off before the layers it depends on for the same
# reason as every other: newest-first, so nothing is left standing on
# a dropped one.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/074_omnivoice_card_evidence_down.sql
# 073 is next. It only alters
# `model_pull_allowlist`, so nothing depends on it — which is exactly
# why it comes off first: a rollback order that is only correct by
# accident stops being correct at the next migration.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/073_artefact_manifests_down.sql
# 072 is next. It hangs `training_run_id` off
# `model_registry` with a foreign key to `model_training_runs`, so it
# must come off before 071 — peeling 071 first would try to drop a
# table that a live foreign key still references.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/072_model_promotion_gate_down.sql
# 071 is next, and it must come off before 070: training_run_datasets
# references training_datasets, and 071's binding trigger reads a
# table 070's rollback would drop.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/071_model_training_runs_down.sql
# 070 is next. Its two tables are independent of 067-069, but the
# cycle peels newest-first and a later migration must never be left
# standing on a dropped one.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/070_training_datasets_down.sql
# 069 is next. It only seeds rows, so it comes off before 068 — peeling
# 068 first would drop the table its DELETE targets.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/069_model_licence_evidence_down.sql
# 068 is next — before 067,
# whose `model_registry` it hangs a column, a foreign key and a
# trigger off. Peeling 067 first would try to drop a table that
# `model_registry.licence_id` still references.
#
# Its rollback REFUSES while a model serves under a recorded approval,
# because dropping the register removes the record of WHO approved it
# and on what evidence while leaving the model serving. The suites
# above leave nothing serving — serving now needs an approved licence
# row, and recording one is a named human's act that no test performs
# — so nothing needs clearing here. The refusal is proved on its own
# disposable database in the dedicated step above.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/068_model_licence_register_down.sql
# 067 comes off next.
#
# Its rollback REFUSES while any model version is `serving`, because
# dropping the registry then destroys both the record of what is
# running and — through `rollback_target` — the only record of what to
# go back to. The suites above register models but leave none serving
# (setting `serving` requires a recorded commercial licence, which is
# Phase 6 and deliberately absent), so nothing needs clearing here.
# The refusal is proved on its own disposable database in the
# dedicated step above.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/067_model_registry_down.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/066_job_runs_down.sql
# 065 is next.
#
# Its rollback REFUSES while any user holds a live second factor in
# more than one tenant, because restoring 036's global unique would
# mean destroying one enrolment each and locking those people out of
# an organisation. The suites above create exactly that state, so the
# surplus enrolments are REVOKED rather than deleted — revoked_at is
# what the guard reads, and a revoked enrolment is still the audit
# trail for a second factor that existed. The refusal itself is proved
# on its own disposable database in the dedicated step above.
psql -h localhost -U postgres -d iratest -c "UPDATE totp_secrets t SET revoked_at = NOW() WHERE revoked_at IS NULL AND EXISTS (SELECT 1 FROM totp_secrets o WHERE o.user_id = t.user_id AND o.id <> t.id AND o.revoked_at IS NULL)"
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/065_totp_enrolment_is_per_tenant_down.sql
# 064 is next.
#
# Its rollback restores 061's sixteen-value event CHECK as NOT VALID,
# deliberately: `auth_events` is append-only for every role, so a
# `log_expired` row written by an expiry pass cannot be removed to let
# a validated constraint through. Going backwards cannot unwrite
# history. The reasoning is in the file and both halves are asserted
# in tests/tenancy/test_p4_auth_event_expiry.py.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/064_auth_event_expiry_down.sql
# 062 is next.
#
# `contract/063` is deliberately absent from this cycle: it is not a
# chain migration. It lives in `postgres/contract/`, an operator
# applies it, and it is proved on its own disposable database in the
# dedicated step above.
#
# Its guard refuses while any enrolment holds a wrapped key, and the
# suites above create them. The keys are cleared rather than the rows
# deleted: 037's table is biometric, and a cycle step that deleted
# enrolments would be practising the destruction this chain exists to
# prevent. The refusal itself is proved on its own disposable database
# in the dedicated step above.
psql -h localhost -U postgres -d iratest -c "UPDATE voice_profiles SET wrapped_dek = NULL, wrap_nonce = NULL, template_nonce = NULL, migration_status = 'PLAINTEXT', template_ciphertext = NULL, encryption_key_version = NULL, encrypted_at = NULL WHERE wrapped_dek IS NOT NULL"
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/062_voiceprint_encryption_down.sql
# 061 is next.
#
# Its guard refuses on ANY row, not on a subset, so the log is
# TRUNCATEd rather than DELETEd first: 061's trigger is FOR EACH ROW
# and refuses DELETE outright, so a DELETE here would fail the cycle
# on the guard that is working correctly. TRUNCATE does not fire a
# row trigger, and is precisely the deliberate operator action the
# rollback's HINT describes. The refusal itself is proved on its own
# disposable database in the dedicated step above.
psql -h localhost -U postgres -d iratest -c "TRUNCATE auth_events"
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/061_auth_events_down.sql
# 060 is next.
#
# Its guard refuses while any LIVE OAuth credential exists, and the
# suites above create them. Revoking is enough: a revoked grant is
# already dead at the provider.
psql -h localhost -U postgres -d iratest -c "UPDATE connector_accounts SET revoked_at = NOW(), credential_ciphertext = NULL, credential_nonce = NULL, encryption_key_id = NULL WHERE revoked_at IS NULL"
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/060_user_scoped_credentials_down.sql
# 059 is next — and this is
# the ONE comment in this step that may say so.
#
# Its guard refuses while any account can ONLY sign in through a
# provider, and the suites above create exactly those — so the fixture
# rows are cleared first, as every DELETE below. The refusal itself is
# proved on its own disposable database in the dedicated step above.
psql -h localhost -U postgres -d iratest -c "DELETE FROM external_identities"
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/059_external_identities_down.sql
# 058 is next — and this is
# the ONE comment in this step that may say so. See the note above
# 056: three consecutive migrations each claimed to be "the newest
# layer" here, because every batch added its own note and none
# corrected the one above it.
#
# Its guard refuses while any UNEXPIRED reset link is outstanding, and
# the suites above issue them — so the fixture rows are cleared first,
# exactly as every DELETE below. The refusal itself is proved on its
# own disposable database in the dedicated step above.
psql -h localhost -U postgres -d iratest -c "DELETE FROM password_reset_tokens"
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/058_password_reset_down.sql
# 057 is next.
#
# Its guard refuses while any LIVE session exists, and the suites
# above sign in — so the fixture rows are cleared first, exactly as
# the recovery-code, invitation and credential DELETEs below are. The
# refusal itself is proved on its own disposable database in the
# dedicated step above.
psql -h localhost -U postgres -d iratest -c "DELETE FROM user_sessions"
psql -h localhost -U postgres -d iratest -c "DELETE FROM user_devices"
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/057_session_isolation_down.sql
psql -h localhost -U postgres -d iratest -c "DELETE FROM mfa_recovery_codes"
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/056_mfa_recovery_down.sql
psql -h localhost -U postgres -d iratest -c "DELETE FROM tenant_invitations"
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/055_invitations_down.sql
psql -h localhost -U postgres -d iratest -c "DELETE FROM user_credentials"
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/054_registration_down.sql
# 053 is newer than 052 and therefore comes off BEFORE it. Getting
# this order wrong is the specific staleness that made PR #63 red
# three times: a migration added to the chain but not to this cycle.
#
# Its guard counts ENCRYPTED rows, and the DELETE above has just
# removed every enrolment, so it reverses here rather than refusing.
# The refusal path is proved on its own database in the dedicated step
# above — both halves of the script run somewhere, neither is assumed.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/053_mfa_key_material_down.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f rollback/052_totp_tenancy_down.sql
# 051 is the next layer. Its rollback REFUSES here and must — this
# database holds thousands of tenants, and reversing 051 would strip
# `notifications` of its tenancy columns and RLS for every future row
# as well as the existing ones. That is finding 3C-10 re-opened.
#
# So the refusal is asserted, exactly as 048's is below, and the
# REVERSAL is verified in the dedicated single-tenant step above,
# where it is a legitimate operation. Both halves of the script are
# therefore executed somewhere; neither is assumed.
if psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
        -f rollback/051_notifications_tenancy_down.sql 2>/tmp/rollback051.err; then
  echo "::error::rollback/051 succeeded against a multi-tenant database. It must refuse — reversing it re-opens 3C-10."
  exit 1
fi
if ! grep -q "051-down REFUSING" /tmp/rollback051.err; then
  echo "::error::rollback/051 failed for the wrong reason:"
  cat /tmp/rollback051.err
  exit 1
fi
echo "✅ rollback/051 refuses, as designed"
# The refusal leaves 051's two foreign keys to `workspaces` in place,
# and 033's rollback cannot drop that table while they exist. They are
# removed here as a fixture teardown — not as a rollback, which is why
# the columns, policies and RLS are deliberately left alone — and
# restored after the re-apply below. `notifications` rows go first
# because the workspaces they name are about to be deleted; they are
# disposable test rows like every other DELETE in this step.
#
# Hand-written DDL goes stale silently, so the recompose step at the
# end of this job asserts both definitions against what 051 declares
# rather than merely counting them. If 051 ever changes a constraint,
# that step fails and names it.
psql -h localhost -U postgres -d iratest -c "DELETE FROM notifications"
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -c \
  "ALTER TABLE notifications
       DROP CONSTRAINT IF EXISTS notifications_workspace_in_tenant_fkey,
       DROP CONSTRAINT IF EXISTS notifications_workspace_id_fkey"
# 050 comes off next. Its rollback is a
# clean reversal: dropping a NOT NULL deletes nothing and moves
# nothing. It must precede 049's rollback so the database is never
# left with the constraint gone AND the policy gone in the wrong
# order — see the script's own header.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f rollback/050_workspace_not_null_down.sql
# 049 next. Its rollback is also a real reversal — dropping
# the workspace arm from a predicate deletes nothing and moves
# nothing — so it runs rather than being asserted. It also WIDENS
# access back to the pre-5C state, which is the correct behaviour for
# a rollback and is stated in the script's own header so nobody reads
# this step as evidence that the boundary is optional.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f rollback/049_workspace_policies_down.sql
# 048 is next and the only one whose rollback is a
# REFUSAL by design rather than a reversal. It is a data migration: it
# places rows and creates no schema object, and there is nothing safe
# to restore — a NULL workspace_id is precisely the invisibility 048
# exists to remove, because a workspace policy's predicate evaluates
# to NULL rather than false for such a row and denies. There is also
# no marker to reverse: a row 048 placed is indistinguishable from one
# that already carried that workspace.
#
# So the cycle asserts the refusal instead of executing a reversal. A
# rollback that "worked" here would mean somebody had made it clear
# the column, which is the tempting fix when this step goes red.
if psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
        -f rollback/048_workspace_backfill_down.sql 2>/tmp/rollback048.err; then
  echo "::error::rollback/048 succeeded. It must refuse — clearing workspace_id manufactures rows no workspace policy can read."
  exit 1
fi
if ! grep -q "Refusing to reverse 048" /tmp/rollback048.err; then
  echo "::error::rollback/048 failed for the wrong reason:"
  cat /tmp/rollback048.err
  exit 1
fi
echo "✅ rollback/048 refuses, as designed"
# 047 next — its rollback refuses while two workspaces hold the same
# entity name. The suites above create exactly that, which is the R-68
# state 047 exists to permit, so the rows are cleared in the same loop
# above.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f rollback/047_graph_scope_constraints_down.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f rollback/046_workspace_constraints_down.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f rollback/045_workspace_columns_down.sql
# 039's rollback REFUSES while more than one tenant holds a workspace,
# because dropping `workspaces` would destroy the content boundary
# those rows define. That guard is right and stays. What changed is
# the fixtures: the erasure suites now provision a workspace per test
# tenant, because `erase_postgres` runs one bound pass per workspace
# and a tenant with none is a refusal rather than a no-op.
#
# So the fixture rows are cleared here, children first, exactly as
# this step already does for every other guard it has to satisfy.
# Emptying the tables is a test teardown; the guard still protects a
# real database, where those rows would not be disposable.
# Guarded: this may only ever run against the disposable CI database,
# and only when every row present is attributable to this run. It
# asserts rather than assumes, and refuses rather than deleting
# something it did not expect.
db="$(psql -h localhost -U postgres -d iratest -tAc 'SELECT current_database()')"
if [ "${db}" != "iratest" ]; then
  echo "::error::fixture cleanup is only for the disposable CI database, not ${db}"
  exit 1
fi
before_t="$(psql -h localhost -U postgres -d iratest -tAc 'SELECT count(DISTINCT tenant_id) FROM workspaces')"
before_w="$(psql -h localhost -U postgres -d iratest -tAc 'SELECT count(*) FROM workspaces')"
echo "fixture cleanup: ${before_w} workspace(s) across ${before_t} tenant(s)"
# Every workspace here must belong to a tenant this run created. A row
# whose tenant is missing from `tenants` would be something else
# entirely, and the right response is to stop, not to delete it.
orphans="$(psql -h localhost -U postgres -d iratest -tAc \
  'SELECT count(*) FROM workspaces w LEFT JOIN tenants t ON t.id = w.tenant_id WHERE t.id IS NULL')"
if [ "${orphans}" != "0" ]; then
  echo "::error::${orphans} workspace row(s) belong to no known tenant — refusing to delete them"
  exit 1
fi
# Children before parents.
for t in workspace_memberships workspaces organisations; do
  psql -h localhost -U postgres -d iratest -c "DELETE FROM $t"
done
after_t="$(psql -h localhost -U postgres -d iratest -tAc 'SELECT count(DISTINCT tenant_id) FROM workspaces')"
if [ "${after_t}" != "0" ]; then
  echo "::error::${after_t} tenant(s) still hold workspaces after cleanup — 039 will refuse"
  exit 1
fi
echo "fixture cleanup: 0 tenants hold workspaces"
# 044 → 033 come off before anything below them is touched. No fixture
# teardown is needed for this range: their guards are satisfied by the
# data the suites above leave behind, and 044's guard — which refuses
# while two tenants have calibrated the same domain — has nothing to
# refuse, because the key it restores made that unrepresentable.
for n in 044_strategy_calibration_tenant_key \
         043_tenant_scope_operational_content \
         042_role_ownership_separation \
         041_biometric_claim_telemetry \
         040_membership_grants \
         039_workspace_substrate \
         038_biometric_audit_context \
         037_voice_profile_ownership \
         036_totp_ownership \
         035_memberships \
         034_identity_mapping \
         033_organisations_workspaces; do
  psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f "rollback/${n}_down.sql"
done
# The guard refuses while several tenants hold data — clear the test
# tenants' rows first (trust_ledger is append-only and is left alone).
for t in conversations memory_embeddings people tasks; do
  psql -h localhost -U postgres -d iratest -c "DELETE FROM $t"
done
# 024 first: it depends on 023's `companions` table, so it must come
# off before 023 goes and back on after 023 returns. Its own guard
# refuses while several tenants hold companion data, so clear those
# rows the same way.
for t in companion_versions companion_preferences companions; do
  psql -h localhost -U postgres -d iratest -c "DELETE FROM $t"
done
# 025 comes off first — it is the newest layer, and permission_decisions
# references constitution_rules and permission_grants. Its own guard
# refuses while several tenants hold rules or grants, so clear those
# too. permission_decisions is append-only (a trigger refuses DELETE),
# which is exactly why the rollback drops the trigger before the table
# rather than trying to empty it.
#
# The trigger has to come off HERE too, and P9-07 is what showed it.
# `permission_decisions.rule_id` is
# `REFERENCES constitution_rules(id) ON DELETE SET NULL`, so deleting a
# rule makes PostgreSQL issue an UPDATE against permission_decisions —
# and the trigger refuses UPDATE as well as DELETE. The cleanup below
# therefore failed with "permission_decisions is append-only" naming a
# statement nobody wrote.
#
# It only became reachable when something first recorded a decision
# that MATCHED A RULE: `action_bridge.decide` calls
# `store.record_decision`, and the governed-path suite writes tenant
# rules for it to match. Before that, `rule_id` was always NULL here
# and the cascade had nothing to update.
#
# This is a fixture-teardown ordering fact, not a product defect: no
# code path deletes a constitution rule — `store.py` offers
# `set_rule_enabled`, not a delete — so production never issues the
# cascade at all.
#
# DISABLED and re-ENABLED, not dropped. Dropping it was the first fix
# and it moved the failure rather than removing it: a later step does
# `ALTER TABLE permission_decisions DISABLE TRIGGER
# permission_decisions_no_update`, which then failed with "trigger
# does not exist". DISABLE is this repository's idiom for suspending
# an append-only guard around fixture teardown, and the guard is put
# straight back so nothing after this line runs without it.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -c "ALTER TABLE permission_decisions DISABLE TRIGGER permission_decisions_no_update"
for t in approval_events permission_grants constitution_rules; do
  psql -h localhost -U postgres -d iratest -c "DELETE FROM $t"
done
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -c "ALTER TABLE permission_decisions ENABLE TRIGGER permission_decisions_no_update"
# 031 first — it is the newest layer. Its guard refuses while any of
# the ten pre-tenancy tables holds rows for more than one tenant, and
# the B1 suite creates several tenants' worth. Five of them are
# append-only against UPDATE (not DELETE — that narrowing is what lets
# account erasure reach them), so a plain DELETE clears the fixtures.
# privacy_envelopes goes last: three of the others reference it.
for t in cognitive_prediction_outcomes cognitive_predictions \
         cognitive_cycles cognitive_events cognitive_goals \
         files envelope_members commitments lifeos_events \
         privacy_envelopes; do
  psql -h localhost -U postgres -d iratest -c "DELETE FROM $t"
done
# 030 next. Its guard refuses while any LIVE
# data-encryption key exists, and the key-management suite creates a
# great many. Key rows cannot be deleted (the shred-only trigger is
# precisely the property tested), so the trigger is momentarily
# disabled to clear the fixture rows: a test teardown, not something
# the application can do. Re-enabled immediately.
psql -h localhost -U postgres -d iratest -c "ALTER TABLE tenant_data_keys DISABLE TRIGGER tenant_data_keys_shred_only"
psql -h localhost -U postgres -d iratest -c "DELETE FROM tenant_data_keys"
psql -h localhost -U postgres -d iratest -c "ALTER TABLE tenant_data_keys ENABLE TRIGGER tenant_data_keys_shred_only"
# 029 next. Its guard REFUSES outright while
# any administrator access has been logged, and the isolation suite
# logs some. Both access tables are append-only, so the triggers are
# momentarily disabled to clear the fixture rows — a test teardown, not
# something the application can do. The triggers are the whole point
# and are re-enabled immediately.
psql -h localhost -U postgres -d iratest -c "ALTER TABLE admin_access_log DISABLE TRIGGER admin_access_log_no_rewrite"
psql -h localhost -U postgres -d iratest -c "DELETE FROM admin_access_log"
psql -h localhost -U postgres -d iratest -c "ALTER TABLE admin_access_log ENABLE TRIGGER admin_access_log_no_rewrite"
psql -h localhost -U postgres -d iratest -c "ALTER TABLE admin_access_grants DISABLE TRIGGER admin_grants_revoke_only"
psql -h localhost -U postgres -d iratest -c "DELETE FROM admin_access_grants"
psql -h localhost -U postgres -d iratest -c "ALTER TABLE admin_access_grants ENABLE TRIGGER admin_grants_revoke_only"
for t in model_usage usage_quotas; do
  psql -h localhost -U postgres -d iratest -c "DELETE FROM $t"
done
# 028 next. Its guard refuses while more than
# one tenant holds Action Receipts, and the isolation suite creates
# two tenants' worth. Receipts are append-only, so the trigger is
# momentarily disabled to clear the fixture rows: that is a test
# teardown, not something the application can do — the trigger is the
# whole point and is re-enabled immediately.
psql -h localhost -U postgres -d iratest -c "ALTER TABLE action_receipts DISABLE TRIGGER action_receipts_no_rewrite"
psql -h localhost -U postgres -d iratest -c "DELETE FROM action_receipts"
psql -h localhost -U postgres -d iratest -c "ALTER TABLE action_receipts ENABLE TRIGGER action_receipts_no_rewrite"
for t in connector_oauth_states connector_accounts; do
  psql -h localhost -U postgres -d iratest -c "DELETE FROM $t"
done
# 027 next — realtime_events references
# realtime_sessions. Its guard refuses while more than one tenant
# holds session history, and the isolation suite creates two tenants'
# worth, so those rows are cleared. No trigger juggling is needed:
# realtime_events refuses UPDATE but permits DELETE precisely so
# erasure (and this teardown) can reach it. Deleting the sessions
# cascades to the events.
for t in realtime_events realtime_sessions; do
  psql -h localhost -U postgres -d iratest -c "DELETE FROM $t"
done
# 026 next. Its guard refuses while redacted
# audit records exist, and the isolation suite creates some, so those
# test rows are cleared with the triggers momentarily disabled. That
# is a test-fixture teardown, not something the application can do:
# the triggers are the whole point and are re-enabled immediately.
for t in export_requests deletion_requests retention_settings; do
  psql -h localhost -U postgres -d iratest -c "DELETE FROM $t"
done
psql -h localhost -U postgres -d iratest -c "ALTER TABLE trust_ledger DISABLE TRIGGER trust_ledger_redactable"
psql -h localhost -U postgres -d iratest -c "DELETE FROM trust_ledger"
psql -h localhost -U postgres -d iratest -c "ALTER TABLE trust_ledger ENABLE TRIGGER trust_ledger_redactable"
psql -h localhost -U postgres -d iratest -c "ALTER TABLE permission_decisions DISABLE TRIGGER permission_decisions_no_update"
psql -h localhost -U postgres -d iratest -c "DELETE FROM permission_decisions"
psql -h localhost -U postgres -d iratest -c "ALTER TABLE permission_decisions ENABLE TRIGGER permission_decisions_no_update"
# 032 comes off before 031: it only adds columns to usage_quotas, so
# it is the newest and shallowest layer.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f rollback/032_atomic_request_quota_down.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f rollback/031_pretenancy_isolation_down.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f rollback/030_key_management_down.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f rollback/029_commercial_down.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f rollback/028_connectors_down.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f rollback/027_realtime_down.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f rollback/026_privacy_down.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f rollback/025_governance_down.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f rollback/024_companion_catalogue_down.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f rollback/023_tenancy_down.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f 023_tenancy.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f 024_companion_catalogue.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f 025_governance.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f 026_privacy.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f 027_realtime.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f 028_connectors.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f 029_commercial.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f 030_key_management.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f 031_pretenancy_isolation.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f 032_atomic_request_quota.sql
# …and 033 → 050 back on, so the database ends where it started.
#
# 049 re-applies for the same reason 048 does: the teardown above has
# emptied all seventeen workspace-scoped tables, so its preflight —
# which refuses while any row has no workspace — has nothing to
# refuse. If this step fails on 049, the message names the tables
# still holding such rows, and that is the finding rather than
# something to route around.
#
# 048 re-applies cleanly here because the teardown above has already
# emptied all seventeen workspace-scoped tables — every one of them is
# cleared to satisfy some earlier guard, and `messages` cascades from
# `conversations`. With no rows there is nothing to place and nothing
# to refuse, which is exactly what 048 does on a fresh database.
# If this step ever fails on 048, the message names the tenants and
# tables still holding rows with no workspace, and that is the finding
# rather than something to work around.
for n in 033_organisations_workspaces 034_identity_mapping \
         035_memberships 036_totp_ownership \
         037_voice_profile_ownership 038_biometric_audit_context \
         039_workspace_substrate 040_membership_grants \
         041_biometric_claim_telemetry 042_role_ownership_separation \
         043_tenant_scope_operational_content \
         044_strategy_calibration_tenant_key \
         045_workspace_columns 046_workspace_constraints \
         047_graph_scope_constraints 048_workspace_backfill \
         049_workspace_policies 050_workspace_not_null; do
  psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -f "${n}.sql"
done
# 051's two `workspaces` foreign keys go back on now that `workspaces`
# exists again. This is the other half of the teardown above, not a
# re-application of 051 — 051 itself was never reversed here, so its
# columns, indexes, RLS and policies are still in place and re-running
# the whole script would fail on the first CREATE POLICY.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -c \
  "ALTER TABLE notifications
       ADD CONSTRAINT notifications_workspace_id_fkey
       FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT"
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -c \
  "ALTER TABLE notifications
       ADD CONSTRAINT notifications_workspace_in_tenant_fkey
       FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces (tenant_id, id)
       ON DELETE RESTRICT"
# 052 goes back on LAST — it is the newest layer, so it is the last to
# return, and unlike 051 this is a real re-application of the script
# rather than hand-written DDL: it was genuinely reversed above. The
# table is empty by now, so the backfill has nothing to attribute.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 052_totp_tenancy.sql
# …and 053 after it, mirroring the peel order in reverse. 053 depends
# on 052 having restored `totp_secrets.tenant_id`, so applying it
# first would fail on the very column 052 puts back.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 053_mfa_key_material.sql
# …and 054 last of all, mirroring the peel order in reverse. It
# references `users`, which 023 restores earlier in this cycle.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 054_registration.sql
# …and 055 after it, which is the true last layer. It adds
# `tenants.display_name` as well as the invitation table, and its
# backfill reads `organisations` — restored earlier in this cycle.
# Applied AS ira_schema_owner, as scripts/migrate.py applies it, so the
# re-created `tenant_invitations` has the owner it has on every install.
# Applied as the superuser, the table came back owned by `postgres`, and
# 095, which lifts forced RLS on it to read accepted invitations, could
# not: a state no install is in.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -c "SET ROLE ira_schema_owner" -f 055_invitations.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 056_mfa_recovery.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 057_session_isolation.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 058_password_reset.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 059_external_identities.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 060_user_scoped_credentials.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 061_auth_events.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 062_voiceprint_encryption.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 064_auth_event_expiry.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 065_totp_enrolment_is_per_tenant.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 066_job_runs.sql
# 067 then 068 go back on LAST, mirroring the peel order in reverse.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 067_model_registry.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 068_model_licence_register.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 069_model_licence_evidence.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 070_training_datasets.sql
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 071_model_training_runs.sql
# …and 072 last of all, mirroring the peel order in reverse. It alters
# `model_registry` and references `model_training_runs`, so both have
# to be back before it can go on.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 072_model_promotion_gate.sql
# …and 073 last of all, mirroring the peel order in reverse. Its
# UPDATEs need the allowlist rows 067 restores earlier in this cycle.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 073_artefact_manifests.sql
# …then 074. It updates a row 069 seeds, so both 068 and
# 069 have to be back before it can run.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 074_omnivoice_card_evidence.sql
# …then 075. It alters `model_usage`, which 029 creates,
# and its apply-time probe inserts a row — so the table and a tenant
# both have to be back before it runs.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 075_usage_outcomes.sql
# …and 076 last of all. It revokes on `model_providers`, so 029 has
# to be back — and it must come after 042, whose default privileges
# are what granted the DML this takes away. Re-applying it before 042
# would revoke and then be silently re-granted.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 076_provider_disclosure_is_not_writable.sql
# …and 077, which is NEVER PEELED and still has to go back on.
#
# 077 is IRREVERSIBLE-ONCE-USED, so nothing above reversed it and its
# table has stood untouched through the whole cycle. That made it look
# like it needed nothing here, and this step went red proving
# otherwise: 042 is re-applied above, its GRANT is
# `… ON ALL TABLES IN SCHEMA public`, and it therefore handed `ira_app`
# back the UPDATE that 077 revokes. The process that writes the ledger
# came out of the cycle able to move the boundary that decides which
# chain model its own records belong to.
#
# Exempt from the peel is not exempt from going back on. A migration
# that only ADDS survives 042; one that TAKES AWAY does not.
#
# Re-applying cannot move the boundary — both inserts are
# ON CONFLICT DO NOTHING and the apply-time assertion that no row sits
# above the boundary is scoped to the apply that STAMPED it. The
# recompose step checks the boundary is byte-identical afterwards
# rather than taking that on trust.
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT ends_at_seq FROM trust_ledger_chain_eras WHERE era = 1" \
  | tr -d '[:space:]' \
  | sed 's/^/CHAIN_ERA_BOUNDARY_BEFORE=/' | tee -a "${GITHUB_ENV:-/dev/null}" >> "${CHAIN_STATE}"
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 077_ledger_chain_eras.sql
# …and 078 last of all, mirroring the peel order in reverse. It
# REVOKES on five tables, so like 076 and 077 it must land AFTER 042 —
# whose `GRANT … ON ALL TABLES` is what hands those privileges out in
# the first place. Re-applying it before 042 would revoke and then be
# silently re-granted, which is the defect the recompose step below
# exists to catch.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 078_billing_plans_and_entitlements.sql
# …and 079 after it, mirroring the peel order in reverse. It must land
# after 078 because it references `tenant_subscriptions`, and after
# 042 for the same reason 076, 077 and 078 do: it REVOKES DELETE on
# its two tables, and 042's `GRANT … ON ALL TABLES` would silently
# hand that privilege straight back if 079 went on first.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 079_invoices.sql
# …and 080 after it, mirroring the peel order in reverse. It must land
# after 042 for a reason worth stating: 080 protects
# `tenants.provider_customer_ref` with a TRIGGER rather than with
# column-level privileges, precisely BECAUSE 042's
# `GRANT … ON ALL TABLES` would supersede column grants and silently
# unprotect it. A GRANT cannot remove a trigger, so the order matters
# less here than it does for 076-079 — but it is kept consistent so
# there is one rule rather than a per-migration exception.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 080_payment_events.sql
# …and 081's two `workspaces` foreign keys go back on last, mirroring
# the peel order in reverse. This is the other half of the teardown
# above and NOT a re-application of 081: 081 refused to reverse here,
# so its columns, index, RLS and policies never came off, and
# re-running the whole script would fail on the first CREATE POLICY.
#
# ON DELETE CASCADE on both, matching what 081 declares — a briefing
# is derived data, and keeping the summary alive after its workspace
# is torn down would outlive the thing it describes. The recompose
# step asserts these definitions against 081 rather than counting
# them, so this hand-written DDL cannot go stale silently.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -c \
  "ALTER TABLE briefings
       ADD CONSTRAINT briefings_tenant_id_fkey
       FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE"
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -c \
  "ALTER TABLE briefings
       ADD CONSTRAINT briefings_workspace_in_tenant_fkey
       FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces (tenant_id, id)
       ON DELETE CASCADE"
# …and 082's pair, the other half of its teardown above.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -c \
  "ALTER TABLE heartbeat_surfaced
       ADD CONSTRAINT heartbeat_surfaced_tenant_id_fkey
       FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE"
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -c \
  "ALTER TABLE heartbeat_surfaced
       ADD CONSTRAINT heartbeat_surfaced_workspace_in_tenant_fkey
       FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces (tenant_id, id)
       ON DELETE CASCADE"
# …and 083 goes back on next, mirroring the peel order in reverse.
# Unlike 081 and 082 this IS a re-application of the script, because
# 083 genuinely came off above — which is the whole point of the
# cycle: down and up have to compose.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 083_biometric_consent_reason.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT pg_get_constraintdef(oid) FROM pg_constraint
    WHERE conname = 'biometric_audit_failure_reason_check'" \
  | grep -q consent_missing \
  || { echo "::error::083 did not re-apply its widened vocabulary"; exit 1; }
# …and 084 last of all, since it came off first.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 084_realtime_unknown_end_reason.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT pg_get_constraintdef(oid) FROM pg_constraint
    WHERE conname = 'realtime_sessions_end_reason_check'" \
  | grep -q "'unknown'" \
  || { echo "::error::084 did not re-apply its widened vocabulary"; exit 1; }
# …and 085 truly last, since it came off first. P14-R3, FINDING AQ.
#
# The assertion names the INDEX and not just the column, and that is
# the point: `dedupe_key` alone deduplicates nothing. The intake's
# `ON CONFLICT (tenant_id, event_type, dedupe_key)` needs an arbiter,
# and without the index it does not fall back to inserting twice —
# it RAISES, so a recomposed schema missing the index turns every
# webhook into a 500 rather than a silent duplicate.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 085_business_events_dedupe_key.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT count(*) FROM pg_indexes
    WHERE indexname = 'uq_business_events_dedupe'" \
  | grep -qx 1 \
  || { echo "::error::085 did not re-apply uq_business_events_dedupe — a redelivered webhook is two events again (FINDING AQ)"; exit 1; }
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT count(*) FROM information_schema.columns
    WHERE table_name = 'business_events' AND column_name = 'dedupe_key'" \
  | grep -qx 1 \
  || { echo "::error::085 did not re-apply business_events.dedupe_key"; exit 1; }
# …then 086, and 087 truly last, since 087 came off first.
#
# 086 is the owner's commercial decision — IRA Pro at 1500 GBP minor
# units, monthly — and the assertion names the AMOUNT rather than the
# presence of a row. A price row that came back with a different
# figure would satisfy "the migration re-applied" and charge somebody
# the wrong amount, which is the worst defect this system can have.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 086_ira_pro_plan_and_price.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT unit_amount_minor || ':' || currency || ':' || recurrence
     FROM billing_plan_prices WHERE plan_id = 'ira_pro'" \
  | grep -qx '1500:GBP:monthly' \
  || { echo "::error::086 did not re-apply the owner's price as 1500 GBP monthly"; exit 1; }

psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 087_billing_provider_lifecycle.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT count(*) FROM pg_tables
    WHERE tablename = 'billing_checkout_sessions' AND rowsecurity" \
  | grep -qx 1 \
  || { echo "::error::087 re-applied without row-level security on billing_checkout_sessions"; exit 1; }

# …then 088, mirroring the peel order in reverse.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 088_technical_library_fulltext.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_memory_embeddings_techlib_fts'" \
  | grep -qxF "CREATE INDEX idx_memory_embeddings_techlib_fts ON public.memory_embeddings USING gin (to_tsvector('english'::regconfig, content)) WHERE (user_id = 'technical_library'::text)" \
  || { echo "::error::088 did not re-apply its partial full-text index with the definition the library query needs"; exit 1; }

# …and 089 after it, the newest layer last.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 089_evidence_repair.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT count(*) FROM pg_class WHERE relname LIKE 'evidence\\_%' AND relkind = 'r'
      AND relrowsecurity AND relforcerowsecurity" \
  | grep -qx 9 \
  || { echo "::error::089 did not re-apply all nine evidence tables with forced row-level security"; exit 1; }

# …then 090, the layer below it. It re-seeds the five voice
# artefacts into the allowlist (073's manifest columns are back by now) and
# their evidence into the register.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 090_voice_model_pins.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT count(*) FROM model_pull_allowlist
    WHERE identifier IN ('Systran/faster-whisper-base', 'Systran/faster-whisper-small',
                         'Systran/faster-distil-whisper-large-v3',
                         'Systran/faster-whisper-large-v3', 'Supertone/supertonic-3')
      AND manifest_sha256 IS NOT NULL" \
  | grep -qx 5 \
  || { echo "::error::090 did not re-apply the five voice artefacts to the allowlist with their manifests"; exit 1; }
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT count(*) FROM model_licence_register
    WHERE identifier IN ('Systran/faster-whisper-base', 'Systran/faster-whisper-small',
                         'Systran/faster-distil-whisper-large-v3',
                         'Systran/faster-whisper-large-v3', 'Supertone/supertonic-3')
      AND classification = 'review_required'" \
  | grep -qx 5 \
  || { echo "::error::090 did not re-apply the five voice licence evidence rows as review_required"; exit 1; }
# …then 092. It puts the private-account columns
# and their four CHECKs back on `tenant_memberships`.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 092_private_account_memberships.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT count(*) FROM pg_constraint
    WHERE conrelid = 'tenant_memberships'::regclass
      AND conname IN ('tenant_memberships_private_role_check',
                      'tenant_memberships_private_fields_private_tenant',
                      'tenant_memberships_person_needs_private_role',
                      'tenant_memberships_private_account_is_not_admin')" \
  | grep -qx 4 \
  || { echo "::error::092 did not re-apply all four private-account constraints"; exit 1; }

# …and 093 last of all — the newest layer. Before it goes back on, one tenant
# is seeded with a legacy conversation for each of ADR-0009's backfill rules,
# written as they were before 093 (no owner column), so the re-apply is proved
# to attribute each one correctly rather than merely to run.
#   e1 evidence  (Alice's name on its message embedding)         → Alice
#   e2 conflict  (Alice and Bob on one session)                    → nobody
#   e3 legacy    (shared workspace, no evidence)                   → nobody
#   e4 sole member (Carol's own workspace, no evidence)            → Carol
#   e5 system    (session 'ira-self-reflection')                   → the job principal
#   e6 legacy    (an embedding naming a namespace, not an account) → nobody
#   e7 evidence  (a legacy delegated account)                      → its runtime id
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -q <<'SQL'
BEGIN;
INSERT INTO tenants (id,name,slug,edition) VALUES ('00000000-0000-4000-8000-0000000920a1','ADR-0009 backfill','adr0009-backfill','cloud');
INSERT INTO organisations (id,tenant_id,name,slug) VALUES ('00000000-0000-4000-8000-0000000920b1','00000000-0000-4000-8000-0000000920a1','O','adr0009-o');
INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,is_default) VALUES
 ('00000000-0000-4000-8000-0000000920c1','00000000-0000-4000-8000-0000000920a1','00000000-0000-4000-8000-0000000920b1','Shared','adr0009-w1',TRUE),
 ('00000000-0000-4000-8000-0000000920c2','00000000-0000-4000-8000-0000000920a1','00000000-0000-4000-8000-0000000920b1','Solo','adr0009-w2',FALSE);
INSERT INTO users (id,email,display_name,auth_provider,status) VALUES
 ('00000000-0000-4000-8000-0000000920d1','alice@adr0009.test','A','password','active'),
 ('00000000-0000-4000-8000-0000000920d2','bob@adr0009.test','B','password','active'),
 ('00000000-0000-4000-8000-0000000920d3','carol@adr0009.test','C','password','active');
INSERT INTO tenant_memberships (tenant_id,user_id,role,status)
 SELECT '00000000-0000-4000-8000-0000000920a1', id, 'member', 'active' FROM users WHERE email LIKE '%@adr0009.test';
INSERT INTO workspace_memberships (tenant_id,workspace_id,user_id,role) VALUES
 ('00000000-0000-4000-8000-0000000920a1','00000000-0000-4000-8000-0000000920c1','00000000-0000-4000-8000-0000000920d1','member'),
 ('00000000-0000-4000-8000-0000000920a1','00000000-0000-4000-8000-0000000920c1','00000000-0000-4000-8000-0000000920d2','member'),
 ('00000000-0000-4000-8000-0000000920a1','00000000-0000-4000-8000-0000000920c2','00000000-0000-4000-8000-0000000920d3','member');
-- delegated_users is closed to new rows (040); a legacy row is simulated here.
ALTER TABLE delegated_users DISABLE TRIGGER USER;
INSERT INTO delegated_users (id,username,password_hash,tenant_id) VALUES
 ('00000000-0000-4000-8000-0000000920f1','dave-chain','x','00000000-0000-4000-8000-0000000920a1');
ALTER TABLE delegated_users ENABLE TRIGGER USER;
INSERT INTO conversations (id,tenant_id,workspace_id,session_id) VALUES
 ('00000000-0000-4000-8000-0000000920e1','00000000-0000-4000-8000-0000000920a1','00000000-0000-4000-8000-0000000920c1','adr0009-s1'),
 ('00000000-0000-4000-8000-0000000920e2','00000000-0000-4000-8000-0000000920a1','00000000-0000-4000-8000-0000000920c1','adr0009-s2'),
 ('00000000-0000-4000-8000-0000000920e3','00000000-0000-4000-8000-0000000920a1','00000000-0000-4000-8000-0000000920c1','adr0009-s3'),
 ('00000000-0000-4000-8000-0000000920e4','00000000-0000-4000-8000-0000000920a1','00000000-0000-4000-8000-0000000920c2','adr0009-s4'),
 ('00000000-0000-4000-8000-0000000920e5','00000000-0000-4000-8000-0000000920a1','00000000-0000-4000-8000-0000000920c1','ira-self-reflection'),
 ('00000000-0000-4000-8000-0000000920e6','00000000-0000-4000-8000-0000000920a1','00000000-0000-4000-8000-0000000920c1','adr0009-s6'),
 ('00000000-0000-4000-8000-0000000920e7','00000000-0000-4000-8000-0000000920a1','00000000-0000-4000-8000-0000000920c1','adr0009-s7');
INSERT INTO messages (id,conversation_id,tenant_id,workspace_id,role,content)
 SELECT ('00000000-0000-4000-8000-00000009210'||n)::uuid,
        ('00000000-0000-4000-8000-0000000920e'||c)::uuid,
        '00000000-0000-4000-8000-0000000920a1',
        CASE WHEN c = '4' THEN '00000000-0000-4000-8000-0000000920c2'
             ELSE '00000000-0000-4000-8000-0000000920c1' END::uuid,
        'user', 'synthetic'
   FROM (VALUES (1,'1'),(2,'2'),(3,'2'),(4,'3'),(5,'4'),(6,'5'),(7,'6'),(8,'7')) v(n,c);
INSERT INTO memory_embeddings (source_id,source_type,content,user_id,tenant_id,workspace_id) VALUES
 ('00000000-0000-4000-8000-000000092101','message','x','alice@adr0009.test','00000000-0000-4000-8000-0000000920a1','00000000-0000-4000-8000-0000000920c1'),
 ('00000000-0000-4000-8000-000000092102','message','x','alice@adr0009.test','00000000-0000-4000-8000-0000000920a1','00000000-0000-4000-8000-0000000920c1'),
 ('00000000-0000-4000-8000-000000092103','message','x','bob@adr0009.test','00000000-0000-4000-8000-0000000920a1','00000000-0000-4000-8000-0000000920c1'),
 ('00000000-0000-4000-8000-000000092107','message','x','system','00000000-0000-4000-8000-0000000920a1','00000000-0000-4000-8000-0000000920c1'),
 ('00000000-0000-4000-8000-000000092108','message','x','dave-chain','00000000-0000-4000-8000-0000000920a1','00000000-0000-4000-8000-0000000920c1');
COMMIT;
SQL
# Applied AS ira_schema_owner, the role scripts/migrate.py uses, not as the
# superuser every other step here connects as: a superuser bypasses RLS, so it
# would see the evidence and memberships that forced RLS hides from the owner,
# and a backfill that attributes nothing in production would pass here.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -c "SET ROLE ira_schema_owner" -f 093_conversation_ownership.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT string_agg(right(id::text, 2) || '=' || coalesce(right(owner_user_id::text, 12), 'none'), ' ' ORDER BY id)
     FROM conversations WHERE tenant_id = '00000000-0000-4000-8000-0000000920a1'" \
  | grep -qx 'e1=0000000920d1 e2=none e3=none e4=0000000920d3 e5=34f09ce54ce7 e6=none e7=7fec24b552a8' \
  || { echo "::error::093 did not attribute the seeded legacy conversations by ADR-0009's rules"; exit 1; }
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT count(*) FROM messages m JOIN conversations c ON c.id = m.conversation_id
    WHERE m.tenant_id = '00000000-0000-4000-8000-0000000920a1'
      AND m.owner_user_id IS DISTINCT FROM c.owner_user_id" \
  | grep -qx 0 \
  || { echo "::error::093 left a message owned differently from its conversation"; exit 1; }
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT count(*) FROM pg_policies WHERE tablename IN ('conversations', 'messages')
      AND coalesce(qual, with_check) LIKE '%owner_user_id%app.user_id%'" \
  | grep -qx 8 \
  || { echo "::error::093 did not re-apply the owner predicate to all eight policies"; exit 1; }
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -q -c \
  "DELETE FROM tenants WHERE id = '00000000-0000-4000-8000-0000000920a1';
   DELETE FROM delegated_users WHERE id = '00000000-0000-4000-8000-0000000920f1';
   DELETE FROM users WHERE email LIKE '%@adr0009.test';"

# …then 094. Before it goes back on, three facts
# are seeded as the owner's chat saved them before #60, so the re-apply is
# proved to move the right ones rather than merely to run:
#   a1 the private tenant, filed under the chat username      → the vault
#   a2 the private tenant, a fact from another source         → stays
#   a3 a cloud tenant, whose vault every member can read      → stays
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -q <<'SQL'
BEGIN;
INSERT INTO tenants (id,name,slug,edition) VALUES ('00000000-0000-4000-8000-0000000940a1','094 cloud','m094-chain','cloud');
INSERT INTO organisations (id,tenant_id,name,slug) VALUES ('00000000-0000-4000-8000-0000000940b1','00000000-0000-4000-8000-0000000940a1','O','m094-chain');
INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,is_default) VALUES
 ('00000000-0000-4000-8000-0000000940c1','00000000-0000-4000-8000-0000000940a1','00000000-0000-4000-8000-0000000940b1','W','m094-chain',TRUE);
INSERT INTO memory_embeddings (id,tenant_id,workspace_id,user_id,source_type,source,content)
 SELECT '00000000-0000-4000-8000-0000000940e1', t.id, w.id, 'admin', 'fact', 'owner_chat', 'm094 a1'
   FROM tenants t JOIN workspaces w ON w.tenant_id = t.id AND w.is_default
  WHERE t.edition = 'private' ORDER BY t.slug LIMIT 1;
INSERT INTO memory_embeddings (id,tenant_id,workspace_id,user_id,source_type,source,content)
 SELECT '00000000-0000-4000-8000-0000000940e2', t.id, w.id, 'admin', 'fact', 'document', 'm094 a2'
   FROM tenants t JOIN workspaces w ON w.tenant_id = t.id AND w.is_default
  WHERE t.edition = 'private' ORDER BY t.slug LIMIT 1;
INSERT INTO memory_embeddings (id,tenant_id,workspace_id,user_id,source_type,source,content) VALUES
 ('00000000-0000-4000-8000-0000000940e3','00000000-0000-4000-8000-0000000940a1','00000000-0000-4000-8000-0000000940c1','alice@m094.test','fact','owner_chat','m094 a3');
COMMIT;
SQL
# As ira_schema_owner, for the reason 093 gives above: a superuser bypasses
# RLS and would pass a 094 whose RLS window was missing.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -c "SET ROLE ira_schema_owner" -f 094_remembered_facts_into_vault.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT string_agg(right(id::text, 2) || '=' || user_id, ' ' ORDER BY id)
     FROM memory_embeddings WHERE content LIKE 'm094 a%'" \
  | grep -qx 'e1=owner e2=admin e3=alice@m094.test' \
  || { echo "::error::094 did not move exactly the private owner's remembered fact"; exit 1; }
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -q -c \
  "DELETE FROM memory_embeddings WHERE content LIKE 'm094 a%';
   DELETE FROM tenants WHERE id = '00000000-0000-4000-8000-0000000940a1';"

# …and 095 last of all — the newest layer (ADR-0010). Before it goes back on,
# two tenants are seeded in the shape before it: a task (Group C, no owner
# column yet) and a project and a calibration row (Group B, no author) in each.
#   s  one membership row                         → every row gains that member
#   m  two, the second suspended                  → nothing is attributed
# so the re-apply is proved to attribute the right rows rather than merely run.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -q <<'SQL'
BEGIN;
INSERT INTO users (id,email,display_name,auth_provider,external_subject,status) VALUES
 ('00000000-0000-4000-8000-0000000950f1',NULL,'S','password','s@m095.test','active'),
 ('00000000-0000-4000-8000-0000000950f2',NULL,'M1','password','m1@m095.test','active'),
 ('00000000-0000-4000-8000-0000000950f3',NULL,'M2','password','m2@m095.test','active');
INSERT INTO tenants (id,name,slug,edition) VALUES
 ('00000000-0000-4000-8000-0000000950a1','095 single','m095-chain-s','cloud'),
 ('00000000-0000-4000-8000-0000000950a2','095 two','m095-chain-m','cloud');
INSERT INTO organisations (id,tenant_id,name,slug) VALUES
 ('00000000-0000-4000-8000-0000000950b1','00000000-0000-4000-8000-0000000950a1','O','m095-chain-s'),
 ('00000000-0000-4000-8000-0000000950b2','00000000-0000-4000-8000-0000000950a2','O','m095-chain-m');
INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,is_default) VALUES
 ('00000000-0000-4000-8000-0000000950c1','00000000-0000-4000-8000-0000000950a1','00000000-0000-4000-8000-0000000950b1','W','m095-chain-s',TRUE),
 ('00000000-0000-4000-8000-0000000950c2','00000000-0000-4000-8000-0000000950a2','00000000-0000-4000-8000-0000000950b2','W','m095-chain-m',TRUE);
INSERT INTO tenant_memberships (tenant_id,user_id,role,status) VALUES
 ('00000000-0000-4000-8000-0000000950a1','00000000-0000-4000-8000-0000000950f1','member','active'),
 ('00000000-0000-4000-8000-0000000950a2','00000000-0000-4000-8000-0000000950f2','member','active'),
 ('00000000-0000-4000-8000-0000000950a2','00000000-0000-4000-8000-0000000950f3','member','suspended');
INSERT INTO tasks (id,title,tenant_id,workspace_id) VALUES
 ('00000000-0000-4000-8000-0000000950d1','m095 s','00000000-0000-4000-8000-0000000950a1','00000000-0000-4000-8000-0000000950c1'),
 ('00000000-0000-4000-8000-0000000950d2','m095 m','00000000-0000-4000-8000-0000000950a2','00000000-0000-4000-8000-0000000950c2');
INSERT INTO projects (id,name,slug,tenant_id,workspace_id) VALUES
 ('00000000-0000-4000-8000-0000000950e1','m095 s','m095-s','00000000-0000-4000-8000-0000000950a1','00000000-0000-4000-8000-0000000950c1'),
 ('00000000-0000-4000-8000-0000000950e2','m095 m','m095-m','00000000-0000-4000-8000-0000000950a2','00000000-0000-4000-8000-0000000950c2');
INSERT INTO strategy_calibration (tenant_id,domain) VALUES
 ('00000000-0000-4000-8000-0000000950a1','m095'),
 ('00000000-0000-4000-8000-0000000950a2','m095');
COMMIT;
SQL
# As ira_schema_owner, for the reason 093 gives above.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -c "SET ROLE ira_schema_owner" -f 095_owners_and_single_user_backfill.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT (SELECT string_agg(right(id::text, 2) || '=' || coalesce(right(owner_user_id::text, 2), '-'), ' ' ORDER BY id)
             FROM tasks WHERE title LIKE 'm095 %')
       || ' ' ||
          (SELECT string_agg(right(id::text, 2) || '=' || coalesce(right(created_by::text, 2), '-'), ' ' ORDER BY id)
             FROM projects WHERE name LIKE 'm095 %')
       || ' ' ||
          (SELECT string_agg(right(tenant_id::text, 2) || '=' || coalesce(right(created_by::text, 2), '-'), ' ' ORDER BY tenant_id)
             FROM strategy_calibration WHERE domain = 'm095')" \
  | grep -qx 'd1=f1 d2=- e1=f1 e2=- a1=f1 a2=-' \
  || { echo "::error::095 did not attribute exactly the single-member tenant's rows"; exit 1; }
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT count(*) FROM attribution_backfill_095 WHERE attributed_to = '00000000-0000-4000-8000-0000000950f1'" \
  | grep -qx 2 \
  || { echo "::error::095 did not record its two Group B attributions"; exit 1; }
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 -q -c \
  "DELETE FROM attribution_backfill_095 WHERE attributed_to = '00000000-0000-4000-8000-0000000950f1';
   DELETE FROM projects WHERE name LIKE 'm095 %';
   DELETE FROM strategy_calibration WHERE domain = 'm095';
   DELETE FROM tenants WHERE id IN ('00000000-0000-4000-8000-0000000950a1','00000000-0000-4000-8000-0000000950a2');
   DELETE FROM users WHERE email IS NULL AND external_subject LIKE '%@m095.test';"
# …then 096 (durable logout). As
# ira_schema_owner, for the reason 093 gives above; twice, because it says it
# is idempotent and the second run is how that is checked. One line in a loop,
# not two: the workflow checker reads the LAST mention of a migration as its
# re-apply, and a second line would let a cycle that lost the first pass, or
# both, still look as if it re-applied 096.
for pass in 1 2; do
  psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
    -c "SET ROLE ira_schema_owner" -f 096_auth_token_versions.sql \
    || { echo "::error::096 failed on re-apply pass $pass"; exit 1; }
done
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT (SELECT relforcerowsecurity FROM pg_class WHERE oid = 'public.auth_token_versions'::regclass)::text
       || ' ' || (SELECT count(*) FROM pg_policies WHERE tablename = 'auth_token_versions')
       || ' ' || (SELECT count(*) FROM pg_trigger WHERE tgname = 'auth_token_versions_monotonic')
       || ' ' || (SELECT string_agg(privilege_type, ',' ORDER BY privilege_type)
                    FROM information_schema.role_table_grants
                   WHERE table_name = 'auth_token_versions' AND grantee = 'ira_app')
       || ' ' || (SELECT string_agg(privilege_type, ',' ORDER BY privilege_type)
                    FROM information_schema.role_table_grants
                   WHERE table_name = 'auth_token_versions' AND grantee = 'ira_worker')" \
  | grep -qx 'true 5 1 DELETE,INSERT,SELECT,UPDATE DELETE,SELECT' \
  || { echo "::error::096 did not restore auth_token_versions with FORCE RLS, its five policies, its trigger and its grants"; exit 1; }
# …then 097 (LangGraph's checkpoint tables), under 098. As
# ira_schema_owner and twice, in one line in a loop, for the reasons 096 gives
# above. It comes after 042's re-apply on purpose: 042 grants DML on every
# table to both runtime roles, and 097 narrows that for its own four.
for pass in 1 2; do
  psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
    -c "SET ROLE ira_schema_owner" -f 097_langgraph_checkpoint_tables.sql \
    || { echo "::error::097 failed on re-apply pass $pass"; exit 1; }
done
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT (SELECT count(*) FROM checkpoint_migrations)
       || ' ' || (SELECT count(*) FROM pg_tables
                   WHERE tablename IN ('checkpoints','checkpoint_blobs','checkpoint_writes','checkpoint_migrations')
                     AND tableowner = 'ira_schema_owner')
       || ' ' || (SELECT string_agg(privilege_type, ',' ORDER BY privilege_type)
                    FROM information_schema.role_table_grants
                   WHERE table_name = 'checkpoints' AND grantee = 'ira_app')
       || ' ' || (SELECT string_agg(privilege_type, ',' ORDER BY privilege_type)
                    FROM information_schema.role_table_grants
                   WHERE table_name = 'checkpoint_migrations' AND grantee = 'ira_app')
       || ' ' || coalesce((SELECT string_agg(privilege_type, ',')
                             FROM information_schema.role_table_grants
                            WHERE table_name IN ('checkpoints','checkpoint_blobs','checkpoint_writes','checkpoint_migrations')
                              AND grantee = 'ira_worker'), 'none')" \
  | grep -qx '10 4 DELETE,INSERT,SELECT,UPDATE SELECT none' \
  || { echo "::error::097 did not restore its four tables with their ten recorded versions, owner and grants"; exit 1; }

# …and 098 last of all — the newest layer. It re-seeds the memory embedder,
# reranker and voice-print encoder into the allowlist and their evidence into
# the register.
psql -h localhost -U postgres -d iratest -v ON_ERROR_STOP=1 \
  -f 098_memory_and_speaker_model_pins.sql
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT count(*) FROM model_pull_allowlist
    WHERE identifier IN ('BAAI/bge-large-en-v1.5', 'BAAI/bge-reranker-v2-m3',
                         'speechbrain/spkrec-ecapa-voxceleb')
      AND manifest_sha256 IS NOT NULL" \
  | grep -qx 3 \
  || { echo "::error::098 did not re-apply the three memory/voice-print artefacts to the allowlist with their manifests"; exit 1; }
psql -h localhost -U postgres -d iratest -tAc \
  "SELECT count(*) FROM model_licence_register
    WHERE identifier IN ('BAAI/bge-large-en-v1.5', 'BAAI/bge-reranker-v2-m3',
                         'speechbrain/spkrec-ecapa-voxceleb')
      AND classification = 'review_required'" \
  | grep -qx 3 \
  || { echo "::error::098 did not re-apply the three memory/voice-print licence evidence rows as review_required"; exit 1; }
# No blanket re-grant here any more. This step used to end with
# `GRANT ALL ON ALL TABLES … TO ira_app`, dating from when the cycle
# stopped at 032 and nothing restored the runtime grants. 042 is now
# re-applied above and grants exactly the four DML privileges, so the
# blanket line would have widened them straight back — GRANT ALL
# includes TRUNCATE, which 042 exists to withhold.
echo "✅ down + up migration cycle verified"
