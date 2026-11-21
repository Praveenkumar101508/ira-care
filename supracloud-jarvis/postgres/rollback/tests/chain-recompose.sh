#!/usr/bin/env bash
#
# The recomposed schema matches a fresh one
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
# The point of the cycle above is that it lands where it started. That
# is only worth asserting, because it did NOT: the 042 rollback restored
# a blanket GRANT, re-applying 042 never revoked the surplus, and the
# runtime role came out of the cycle holding TRUNCATE on every table
# while a database built straight from the chain denies it. A rollback
# that quietly widens privileges is worse than one that fails loudly.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: the repository root`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../../.."

# Read the boundary chain-rollback.sh captured. In CI it is already in the
# environment via $GITHUB_ENV; outside CI it comes from the state file. The
# assertion below fails loudly if neither supplied it, which is the behaviour
# the original step already had.
CHAIN_STATE="${CHAIN_STATE:-${TMPDIR:-/tmp}/ira-chain-state.env}"
if [ -z "${CHAIN_ERA_BOUNDARY_BEFORE:-}" ] && [ -r "${CHAIN_STATE}" ]; then
    # shellcheck disable=SC1090
    . "${CHAIN_STATE}"
fi

        fail() { echo "::error::$1"; exit 1; }
        q() { psql -h localhost -U postgres -d iratest -tAc "$1"; }
        [ "$(q "SELECT has_table_privilege('ira_app','conversations','TRUNCATE')")" = "f" ] \
          || fail "ira_app holds TRUNCATE after the rollback cycle; a fresh chain denies it"
        [ "$(q "SELECT has_table_privilege('ira_app','conversations','SELECT')")" = "t" ] \
          || fail "ira_app lost SELECT after the rollback cycle"
        [ "$(q "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                 WHERE n.nspname='public' AND pg_get_userbyid(c.relowner)
                       IN ('ira_app','ira_worker')")" = "0" ] \
          || fail "a runtime role owns objects after the rollback cycle"
        [ "$(q "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname='conversations'")" \
          = "ira_schema_owner" ] || fail "conversations is not owned by ira_schema_owner"
        [ "$(q "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c
                 JOIN pg_class t ON t.oid=c.conrelid
                 WHERE t.relname='strategy_calibration' AND c.contype='p'")" \
          = "PRIMARY KEY (tenant_id, domain)" ] \
          || fail "strategy_calibration's composite primary key did not survive the cycle"
        # 081 creates no table — it gives an EXISTING one a boundary — so a
        # step that counts tables cannot see it. Peel it, never re-apply it,
        # and `briefings` comes out of the cycle exactly as finding G found
        # it: customer content with no tenancy and no RLS, read by
        # `GET /api/v1/briefing/latest` with no predicate. The boundary is
        # asserted directly, on both halves of the predicate and on FORCE.
        [ "$(q "SELECT relforcerowsecurity FROM pg_class WHERE relname='briefings'")" = "t" ] \
          || fail "briefings lost FORCE row-level security in the rollback cycle (finding G, re-opened)"
        [ "$(q "SELECT count(*) FROM pg_policies WHERE tablename='briefings'")" = "4" ] \
          || fail "briefings does not have its four per-command policies after the cycle"
        [ "$(q "SELECT count(*) FROM information_schema.columns
                 WHERE table_name='briefings' AND column_name IN ('tenant_id','workspace_id')
                   AND is_nullable='NO'")" = "2" ] \
          || fail "briefings' tenant_id/workspace_id are missing or nullable after the cycle"
        # 065 is index-only, and an index is invisible to a step that counts
        # tables and columns. Peel it, never re-apply it, and the recomposed
        # schema silently permits one second factor per user ACROSS ALL
        # TENANTS again — every other assertion in this step still passes.
        # `migration_workflow.py` now requires an index-only migration to be
        # named here; that rule was added because a planted defect could not
        # be planted against a check that examined nothing.
        [ "$(q "SELECT indexdef FROM pg_indexes WHERE indexname='totp_secrets_tenant_user_key'")" \
          = "CREATE UNIQUE INDEX totp_secrets_tenant_user_key ON public.totp_secrets USING btree (tenant_id, user_id) WHERE ((user_id IS NOT NULL) AND (revoked_at IS NULL))" ] \
          || fail "065's per-tenant enrolment index did not survive the cycle with its definition intact"
        [ "$(q "SELECT count(*) FROM pg_indexes WHERE indexname='totp_secrets_user_key'")" = "0" ] \
          || fail "036's global enrolment index came back through the cycle"
        # 088 creates no table and no column — one PARTIAL expression index —
        # so it is invisible to every check that counts tables or columns.
        # Asserted by its full definition: an index that came back without its
        # predicate would still exist and would make every chat message's
        # embedding write pay for to_tsvector, which is what 088 is built to avoid.
        [ "$(q "SELECT indexdef FROM pg_indexes WHERE indexname='idx_memory_embeddings_techlib_fts'")" \
          = "CREATE INDEX idx_memory_embeddings_techlib_fts ON public.memory_embeddings USING gin (to_tsvector('english'::regconfig, content)) WHERE (user_id = 'technical_library'::text)" ] \
          || fail "088's partial full-text index did not survive the cycle with its definition intact"
        # 089 creates the nine evidence tables. Each must come back with FORCED
        # row-level security and its policies — an evidence table without them
        # is one workspace's source documents readable from another — and the
        # two append-only tables must still deny UPDATE to the runtime role.
        [ "$(q "SELECT to_regclass('public.evidence_artifacts') IS NOT NULL")" = "t" ] \
          || fail "evidence_artifacts does not exist after the rollback cycle"
        [ "$(q "SELECT to_regclass('public.evidence_artifact_versions') IS NOT NULL")" = "t" ] \
          || fail "evidence_artifact_versions does not exist after the rollback cycle"
        [ "$(q "SELECT to_regclass('public.evidence_claims') IS NOT NULL")" = "t" ] \
          || fail "evidence_claims does not exist after the rollback cycle"
        [ "$(q "SELECT to_regclass('public.evidence_output_spans') IS NOT NULL")" = "t" ] \
          || fail "evidence_output_spans does not exist after the rollback cycle"
        [ "$(q "SELECT to_regclass('public.evidence_dependencies') IS NOT NULL")" = "t" ] \
          || fail "evidence_dependencies does not exist after the rollback cycle"
        [ "$(q "SELECT to_regclass('public.evidence_patches') IS NOT NULL")" = "t" ] \
          || fail "evidence_patches does not exist after the rollback cycle"
        [ "$(q "SELECT to_regclass('public.evidence_approvals') IS NOT NULL")" = "t" ] \
          || fail "evidence_approvals does not exist after the rollback cycle"
        [ "$(q "SELECT to_regclass('public.evidence_artifact_access') IS NOT NULL")" = "t" ] \
          || fail "evidence_artifact_access does not exist after the rollback cycle"
        [ "$(q "SELECT to_regclass('public.evidence_audit_events') IS NOT NULL")" = "t" ] \
          || fail "evidence_audit_events does not exist after the rollback cycle"
        [ "$(q "SELECT count(*) FROM pg_class WHERE relname LIKE 'evidence\\_%' AND relkind='r'
                 AND relforcerowsecurity")" = "9" ] \
          || fail "an evidence table lost FORCE row-level security in the rollback cycle"
        [ "$(q "SELECT count(*) FROM pg_policies WHERE tablename LIKE 'evidence\\_%'")" = "25" ] \
          || fail "the evidence tables do not have their 25 policies after the cycle"
        [ "$(q "SELECT has_table_privilege('ira_app','evidence_artifact_versions','UPDATE')
                 OR has_table_privilege('ira_app','evidence_audit_events','UPDATE')")" = "f" ] \
          || fail "the runtime role can rewrite evidence versions or audit events after the cycle"
        [ "$(q "SELECT count(*) FROM workspace_role_capabilities
                 WHERE role='workspace_admin' AND capability='workspace.approve_changes'")" = "1" ] \
          || fail "workspace.approve_changes did not survive the cycle"
        # 093 (ADR-0009): conversations and messages each carry owner_user_id, and
        # all eight of their policies check it. A cycle that brought back 049's
        # workspace-only policies would pass every row-count check here and
        # reopen the cross-user read, export and erasure 093 closed.
        [ "$(q "SELECT count(*) FROM information_schema.columns
                 WHERE table_name IN ('conversations','messages')
                   AND column_name = 'owner_user_id'")" = "2" ] \
          || fail "093's owner_user_id did not survive the cycle"
        [ "$(q "SELECT count(*) FROM pg_policies
                 WHERE tablename IN ('conversations','messages')
                   AND coalesce(qual, with_check) LIKE '%owner_user_id%app.user_id%'")" = "8" ] \
          || fail "093's owner predicate is missing from a conversations/messages policy after the cycle"
        [ "$(q "SELECT count(*) FROM pg_constraint
                 WHERE conname = 'messages_conversation_owner_fkey'")" = "1" ] \
          || fail "093's message-owner foreign key did not survive the cycle"
        # 095 (ADR-0010 Group C): five tables carry owner_user_id, a users id set
        # NULL when the user is deleted. Peel it and never re-apply it, and
        # erasure and export reach none of those tables' rows while every table
        # count here still matches. Its record of the single-user backfill must
        # come back too, readable by no runtime role.
        [ "$(q "SELECT count(*) FROM information_schema.columns
                 WHERE table_name IN ('tasks','people','notifications','briefings','heartbeat_surfaced')
                   AND column_name = 'owner_user_id'")" = "5" ] \
          || fail "095's owner_user_id columns did not survive the cycle"
        [ "$(q "SELECT count(*) FROM pg_constraint c JOIN pg_attribute a
                   ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
                 WHERE c.contype = 'f' AND a.attname = 'owner_user_id'
                   AND c.confrelid = 'users'::regclass AND c.confdeltype = 'n'
                   AND c.conrelid::regclass::text IN ('tasks','people','notifications','briefings','heartbeat_surfaced')")" = "5" ] \
          || fail "095's owner_user_id foreign keys (ON DELETE SET NULL) did not survive the cycle"
        [ "$(q "SELECT to_regclass('public.attribution_backfill_095') IS NOT NULL
                 AND NOT has_table_privilege('ira_app','attribution_backfill_095','SELECT')
                 AND NOT has_table_privilege('ira_worker','attribution_backfill_095','SELECT')")" = "t" ] \
          || fail "095's backfill record is missing, or a runtime role can read it, after the cycle"
        # 096 (durable logout): auth_token_versions keyed by users.id with ON
        # DELETE RESTRICT, its monotonic trigger, its grants, and the DELETE
        # policy that lets the runtime roles purge a counter only when it has
        # been unchanged for seven days and its account was erased seven days
        # ago. Peel it and never re-apply it, and every request is refused (the
        # version check reads a missing table) while every table count here
        # still matches minus one.
        [ "$(q "SELECT to_regclass('public.auth_token_versions') IS NOT NULL
                 AND (SELECT confdeltype FROM pg_constraint
                       WHERE conname = 'auth_token_versions_user_id_fkey') = 'r'
                 AND (SELECT count(*) FROM pg_trigger WHERE tgname = 'auth_token_versions_monotonic') = 1
                 AND has_table_privilege('ira_app','auth_token_versions','UPDATE')
                 AND (SELECT count(*) FROM pg_policies WHERE tablename = 'auth_token_versions'
                        AND cmd = 'DELETE' AND qual LIKE '%updated_at%') = 1
                 AND has_table_privilege('ira_worker','auth_token_versions','DELETE')
                 AND NOT has_table_privilege('ira_worker','auth_token_versions','UPDATE')")" = "t" ] \
          || fail "096's auth_token_versions, its RESTRICT key, trigger or grants did not survive the cycle"
        # 097 (LangGraph's checkpoint tables): all four, ten recorded LangGraph
        # migrations, DML for ira_app on the state and SELECT only on the version
        # record, nothing for ira_worker. Peel it and never re-apply it, and the
        # application falls back to an in-memory checkpointer on every start.
        [ "$(q "SELECT to_regclass('public.checkpoint_migrations') IS NOT NULL
                 AND to_regclass('public.checkpoints') IS NOT NULL
                 AND to_regclass('public.checkpoint_blobs') IS NOT NULL
                 AND to_regclass('public.checkpoint_writes') IS NOT NULL
                 AND (SELECT count(*) FROM checkpoint_migrations) = 10
                 AND has_table_privilege('ira_app','checkpoint_writes','DELETE')
                 AND NOT has_table_privilege('ira_app','checkpoint_migrations','INSERT')
                 AND NOT has_table_privilege('ira_worker','checkpoints','SELECT')")" = "t" ] \
          || fail "097's checkpoint tables, their recorded versions or their grants did not survive the cycle"
        # 092 creates no table: it adds two columns and four CHECKs to
        # `tenant_memberships`. Peel it and never re-apply it, and the grant
        # path fails on every database that went through the cycle while every
        # table count here still matches. The CHECK that keeps a private
        # account below tenant admin is asserted by definition, not by name.
        [ "$(q "SELECT count(*) FROM information_schema.columns
                 WHERE table_name='tenant_memberships'
                   AND column_name IN ('private_role','person_id')")" = "2" ] \
          || fail "092's private-account columns are missing after the cycle"
        [ "$(q "SELECT regexp_replace(pg_get_constraintdef(oid), '\\s+', ' ', 'g') FROM pg_constraint
                 WHERE conname='tenant_memberships_private_account_is_not_admin'")" \
          = "CHECK (((private_role IS NULL) OR (role = CASE WHEN (private_role = 'viewer'::text) THEN 'viewer'::text ELSE 'member'::text END)))" ] \
          || fail "092's private-account tenant-role CHECK did not survive the cycle intact"
        # 090 is pure DATA: five voice artefacts allowlisted with their manifests,
        # and their licence evidence. Peel it, never re-apply it, and every table
        # is correct while readiness refuses the pinned STT checkpoints and the
        # register forgets that Supertonic-3's OpenRAIL-M reading is outstanding.
        # Counted by name, not by total: 069's fifteen rows would hide five missing.
        [ "$(q "SELECT count(*) FROM model_pull_allowlist
                 WHERE identifier IN ('Systran/faster-whisper-base', 'Systran/faster-whisper-small',
                                      'Systran/faster-distil-whisper-large-v3',
                                      'Systran/faster-whisper-large-v3', 'Supertone/supertonic-3')
                   AND manifest_sha256 IS NOT NULL AND artifact_hash IS NULL")" = "5" ] \
          || fail "090's five voice artefacts are not on the allowlist with their manifests after the cycle"
        [ "$(q "SELECT count(*) FROM model_licence_register
                 WHERE identifier IN ('Systran/faster-whisper-base', 'Systran/faster-whisper-small',
                                      'Systran/faster-distil-whisper-large-v3',
                                      'Systran/faster-whisper-large-v3', 'Supertone/supertonic-3')
                   AND classification IN ('unknown', 'review_required')")" = "5" ] \
          || fail "090's five voice licence evidence rows did not survive the cycle undetermined"
        # 098, the same shape for the memory embedder, the memory reranker and
        # the voice-print encoder. Counted by name for the same reason.
        [ "$(q "SELECT count(*) FROM model_pull_allowlist
                 WHERE identifier IN ('BAAI/bge-large-en-v1.5', 'BAAI/bge-reranker-v2-m3',
                                      'speechbrain/spkrec-ecapa-voxceleb')
                   AND manifest_sha256 IS NOT NULL AND artifact_hash IS NULL")" = "3" ] \
          || fail "098's three memory/voice-print artefacts are not on the allowlist with their manifests after the cycle"
        [ "$(q "SELECT count(*) FROM model_licence_register
                 WHERE identifier IN ('BAAI/bge-large-en-v1.5', 'BAAI/bge-reranker-v2-m3',
                                      'speechbrain/spkrec-ecapa-voxceleb')
                   AND classification IN ('unknown', 'review_required')")" = "3" ] \
          || fail "098's three memory/voice-print licence evidence rows did not survive the cycle undetermined"
        # 087 creates `billing_checkout_sessions`, and 086 writes the owner's
        # price. Both are asserted by CONTENT rather than by presence: a table
        # that came back without its row-level security, or a price row that
        # came back with a different figure, would satisfy "the migration
        # re-applied" and be the worse outcome in each case — an unscoped
        # checkout table is every tenant's payment attempts visible to every
        # other, and a wrong price charges somebody the wrong amount.
        [ "$(q "SELECT to_regclass('public.billing_checkout_sessions') IS NOT NULL")" = "t" ] \
          || fail "billing_checkout_sessions does not exist after the rollback cycle"
        [ "$(q "SELECT relforcerowsecurity FROM pg_class WHERE relname='billing_checkout_sessions'")" = "t" ] \
          || fail "billing_checkout_sessions lost FORCE row-level security in the rollback cycle"
        [ "$(q "SELECT count(*) FROM pg_policies WHERE tablename='billing_checkout_sessions'")" = "3" ] \
          || fail "billing_checkout_sessions does not have its three policies after the cycle"
        [ "$(q "SELECT unit_amount_minor || ':' || currency || ':' || recurrence
                 FROM billing_plan_prices WHERE plan_id='ira_pro'")" = "1500:GBP:monthly" ] \
          || fail "the ira_pro price did not survive the cycle as 1500 GBP monthly"
        [ "$(q "SELECT count(*) FROM information_schema.columns
                 WHERE table_name='tenant_subscriptions'
                   AND column_name IN ('provider_subscription_ref','cancel_at_period_end')")" = "2" ] \
          || fail "087's subscription columns are missing after the cycle"
        # 085 creates no table either — it adds a column and a PARTIAL unique
        # index to `business_events`, so it is invisible to a step that counts
        # tables. Peel it, never re-apply it, and the recomposed schema loses
        # FINDING AQ's protection silently: a redelivered booking webhook is
        # two rows and two owner alerts again, and every other assertion here
        # still passes.
        #
        # The full `indexdef` is asserted, not merely the index's existence,
        # because the WHERE clause is the half that a careless recreate drops
        # — and an index without it constrains rows that have NO dedupe key,
        # which breaks lead intake instead of protecting it.
        [ "$(q "SELECT indexdef FROM pg_indexes WHERE indexname='uq_business_events_dedupe'")" \
          = "CREATE UNIQUE INDEX uq_business_events_dedupe ON public.business_events USING btree (tenant_id, event_type, dedupe_key) WHERE (dedupe_key IS NOT NULL)" ] \
          || fail "085's partial unique index did not survive the cycle with its definition intact — FINDING AQ re-opens silently"
        [ "$(q "SELECT count(*) FROM information_schema.columns
                 WHERE table_name='business_events' AND column_name='dedupe_key'")" = "1" ] \
          || fail "business_events.dedupe_key is missing after the rollback cycle"
        # 077's table, and its ONE open era. A cycle that dropped it and never
        # put it back would leave every anchor decision falling through to
        # "the boundary is unknown" — which `chain_eras` reports as an empty
        # list rather than as a known answer, so nothing would error. The
        # deployment would simply stop being able to tell a legacy global
        # anchor from a tenant one, silently.
        #
        # The open-era count is asserted as well as the table: two open eras
        # make "which era is this seq in" ambiguous, and an anchor decision
        # cannot be made on an ambiguous answer.
        [ "$(q "SELECT to_regclass('public.trust_ledger_chain_eras') IS NOT NULL")" = "t" ] \
          || fail "trust_ledger_chain_eras did not survive the rollback cycle"
        [ "$(q "SELECT count(*) FROM trust_ledger_chain_eras WHERE ends_at_seq IS NULL")" = "1" ] \
          || fail "the chain-era record has none or several open eras after the cycle"
        [ "$(q "SELECT has_table_privilege('ira_app','trust_ledger_chain_eras','UPDATE')")" = "f" ] \
          || fail "ira_app can move the chain-era boundary after the cycle"
        # …and the boundary itself is unchanged. 077 goes back on inside the
        # cycle to restore the REVOKE above, and re-stamping a LATER boundary
        # is the one thing that must never happen: every ledger row written
        # since the first apply would be reclassified as legacy-global, and
        # tenant anchors would come back LEGACY_GLOBAL_ANCHOR. The two inserts
        # are ON CONFLICT DO NOTHING so that they restate the boundary rather
        # than move it; this asserts that rather than trusting it.
        # ── 078's five tables, and the privileges that make them controls ──
        #
        # Existence first, because a cycle that dropped a table and never put
        # it back would pass every privilege check below vacuously.
        # Written out longhand rather than looped: `migration_workflow.py`
        # requires the literal `to_regclass('public.<table>')` for each table
        # the newest migration creates, and a loop variable satisfies the
        # database while defeating the checker. The rule is right — an
        # assertion you cannot find by reading the step is one nobody will
        # notice going missing.
        [ "$(q "SELECT to_regclass('public.billing_plans') IS NOT NULL")" = "t" ] \
          || fail "078's billing_plans did not survive the cycle"
        [ "$(q "SELECT to_regclass('public.billing_plan_prices') IS NOT NULL")" = "t" ] \
          || fail "078's billing_plan_prices did not survive the cycle"
        [ "$(q "SELECT to_regclass('public.plan_entitlements') IS NOT NULL")" = "t" ] \
          || fail "078's plan_entitlements did not survive the cycle"
        [ "$(q "SELECT to_regclass('public.tenant_subscriptions') IS NOT NULL")" = "t" ] \
          || fail "078's tenant_subscriptions did not survive the cycle"
        [ "$(q "SELECT to_regclass('public.tenant_entitlement_overrides') IS NOT NULL")" = "t" ] \
          || fail "078's tenant_entitlement_overrides did not survive the cycle"
        # Then the privileges. 042 is re-applied inside the cycle and grants
        # DML on ALL TABLES, so each of these is only false if 078 went back
        # on AFTER it. This is the same 042-shaped hole that P10-04 found the
        # hard way.
        [ "$(q "SELECT has_table_privilege('ira_app','billing_plan_prices','INSERT')")" = "f" ] \
          || fail "ira_app can write billing_plan_prices after the cycle — the runtime could set its own customer's price"
        [ "$(q "SELECT has_table_privilege('ira_app','tenant_entitlement_overrides','INSERT')")" = "f" ] \
          || fail "ira_app can insert entitlement overrides after the cycle — the runtime could raise its own caller's limits"
        [ "$(q "SELECT has_table_privilege('ira_app','tenant_subscriptions','DELETE')")" = "f" ] \
          || fail "ira_app can DELETE subscriptions after the cycle"
        [ "$(q "SELECT has_table_privilege('ira_app','tenant_subscriptions','INSERT')")" = "t" ] \
          || fail "ira_app lost INSERT on tenant_subscriptions — subscribing is something the runtime legitimately does"
        [ "$(q "SELECT relforcerowsecurity FROM pg_class WHERE relname='tenant_subscriptions'")" = "t" ] \
          || fail "FORCE RLS is not set on tenant_subscriptions after the cycle"
        # ── 079's two tables, and the controls that make them evidence ──
        [ "$(q "SELECT to_regclass('public.invoices') IS NOT NULL")" = "t" ] \
          || fail "079's invoices did not survive the cycle"
        [ "$(q "SELECT to_regclass('public.invoice_lines') IS NOT NULL")" = "t" ] \
          || fail "079's invoice_lines did not survive the cycle"
        [ "$(q "SELECT has_table_privilege('ira_app','invoices','DELETE')")" = "f" ] \
          || fail "ira_app can DELETE an invoice after the cycle — a bill that can disappear is not evidence of anything"
        [ "$(q "SELECT has_table_privilege('ira_app','invoice_lines','DELETE')")" = "f" ] \
          || fail "ira_app can DELETE invoice lines after the cycle — the total would outlive what justifies it"
        [ "$(q "SELECT has_table_privilege('ira_app','invoices','INSERT')")" = "t" ] \
          || fail "ira_app lost INSERT on invoices — generating a draft is something the runtime legitimately does"
        [ "$(q "SELECT relforcerowsecurity FROM pg_class WHERE relname='invoices'")" = "t" ] \
          || fail "FORCE RLS is not set on invoices after the cycle"
        [ "$(q "SELECT relforcerowsecurity FROM pg_class WHERE relname='invoice_lines'")" = "t" ] \
          || fail "FORCE RLS is not set on invoice_lines after the cycle"
        # The trigger, not just the table. A cycle that restored the columns
        # and lost the immutability rule would pass every check above while
        # leaving issued invoices editable.
        [ "$(q "SELECT count(*) FROM pg_trigger WHERE tgname='invoices_no_edit_once_issued' AND NOT tgisinternal")" = "1" ] \
          || fail "the issued-invoice immutability trigger did not survive the cycle"
        [ "$(q "SELECT count(*) FROM pg_trigger WHERE tgname='invoice_lines_no_edit_once_issued' AND NOT tgisinternal")" = "1" ] \
          || fail "the invoice-line immutability trigger did not survive the cycle"
        # ── 080's table, and the guard that makes the mapping trustworthy ──
        [ "$(q "SELECT to_regclass('public.payment_events') IS NOT NULL")" = "t" ] \
          || fail "080's payment_events did not survive the cycle"
        [ "$(q "SELECT has_table_privilege('ira_app','payment_events','DELETE')")" = "f" ] \
          || fail "ira_app can DELETE a payment event after the cycle — the record that a provider spoke could disappear"
        # No tenant_id: the column is what made an earlier draft violate the
        # cross-tenant sweep, and it was redundant besides.
        [ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='payment_events' AND column_name='tenant_id'")" = "0" ] \
          || fail "payment_events regained a tenant_id column; it must be unscoped or scoped, never scoped-without-protection"
        # THE guard. This is the assertion that matters most in this step: 042
        # is re-applied inside this cycle and grants DML on ALL TABLES, which
        # is exactly what would have silently unprotected a column-privilege
        # design. A trigger cannot be removed by a GRANT — proved here rather
        # than assumed.
        [ "$(q "SELECT count(*) FROM pg_trigger WHERE tgname='tenants_provider_customer_guard' AND NOT tgisinternal")" = "1" ] \
          || fail "the tenants provider-customer guard did not survive the cycle; the payment mapping is writable by the runtime for ANY tenant"
        [ "$(q "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prosecdef")" = "0" ] \
          || fail "a SECURITY DEFINER function exists after the cycle"
        [ -n "${CHAIN_ERA_BOUNDARY_BEFORE:-}" ] \
          || fail "the chain-era boundary was never captured before 077 was re-applied"
        [ "$(q "SELECT ends_at_seq FROM trust_ledger_chain_eras WHERE era = 1")" \
          = "$CHAIN_ERA_BOUNDARY_BEFORE" ] \
          || fail "re-applying 077 MOVED the chain-era boundary (was $CHAIN_ERA_BOUNDARY_BEFORE); every row written since the first apply is now classified legacy-global"
        # 066's table. A cycle that dropped it and never put it back would
        # leave the scheduler unable to record a single run, and every other
        # assertion in this step would still pass.
        [ "$(q "SELECT to_regclass('public.job_runs') IS NOT NULL")" = "t" ] \
          || fail "job_runs did not survive the rollback cycle"
        # 067's two tables. Their absence fails CLOSED at runtime — the
        # gateway refuses when no model is registered and the loader refuses
        # when the allowlist has no entry — so a cycle that lost them would
        # not corrupt anything. It would take the model fabric offline while
        # every other assertion here passed, which is exactly the failure this
        # step exists to catch.
        [ "$(q "SELECT to_regclass('public.model_registry') IS NOT NULL")" = "t" ] \
          || fail "model_registry did not survive the rollback cycle"
        [ "$(q "SELECT to_regclass('public.model_pull_allowlist') IS NOT NULL")" = "t" ] \
          || fail "model_pull_allowlist did not survive the rollback cycle"
        # The runtime role must not be able to write either one. A cycle that
        # restored a blanket GRANT would let the process that CHECKS the
        # allowlist also ADD to it, which is the whole control undone — and
        # 042 already produced exactly that shape once.
        for t in model_registry model_pull_allowlist model_licence_register; do
          for p in INSERT UPDATE DELETE; do
            [ "$(q "SELECT has_table_privilege('ira_app','$t','$p')")" = "f" ] \
              || fail "ira_app holds $p on $t after the rollback cycle"
          done
        done
        # 068's register, the column that binds a serving model to it, and the
        # trigger that enforces the binding. All three have to come back, and
        # the trigger is the one a cycle would lose silently: without it,
        # 067's `serving_is_licensed` still passes on any non-empty string, so
        # the schema LOOKS licensed while the gate that reads the register is
        # gone. That is the exact shape 068 was written to close.
        [ "$(q "SELECT to_regclass('public.model_licence_register') IS NOT NULL")" = "t" ] \
          || fail "model_licence_register did not survive the rollback cycle"
        [ "$(q "SELECT count(*) FROM pg_attribute WHERE attrelid='model_registry'::regclass
                 AND attname='licence_id' AND NOT attisdropped")" = "1" ] \
          || fail "model_registry.licence_id did not survive the rollback cycle"
        [ "$(q "SELECT count(*) FROM pg_trigger WHERE tgname='model_registry_licence_gate'
                 AND NOT tgisinternal")" = "1" ] \
          || fail "the licence gate trigger did not survive the rollback cycle; serving would fall back to 067's free-text check"
        # 075 widens `model_usage.outcome` and CREATES NO TABLE, so a cycle
        # that peeled it and never put it back would leave every assertion
        # above green while the gateway's two most important metering rows —
        # "we refused an unapproved model" and "a stream was abandoned" —
        # went back to being silently dropped by the fail-soft writer. The
        # check is on the constraint's text rather than on a table's
        # existence, because the table exists either way.
        [ "$(q "SELECT count(*) FROM pg_constraint WHERE conname='model_usage_outcome_check'
                 AND pg_get_constraintdef(oid) LIKE '%refused_policy%'")" = "1" ] \
          || fail "model_usage.outcome no longer accepts refused_policy; 075 did not survive the rollback cycle"
        [ "$(q "SELECT count(*) FROM pg_constraint WHERE conname='model_usage_outcome_check'
                 AND pg_get_constraintdef(oid) LIKE '%abandoned%'")" = "1" ] \
          || fail "model_usage.outcome no longer accepts abandoned; 075 did not survive the rollback cycle"
        # And the narrowing that must NOT have happened: a refusal still
        # cannot claim to have consumed tokens.
        [ "$(q "SELECT count(*) FROM pg_constraint WHERE conname='refusals_consume_nothing'
                 AND pg_get_constraintdef(oid) LIKE '%refused_policy%'")" = "1" ] \
          || fail "refusals_consume_nothing no longer covers refused_policy"
        # 076 creates nothing and alters nothing — it revokes. A cycle that
        # peeled it and never put it back would leave every assertion above
        # green while the process that answers requests regained the ability
        # to edit the record of where it sent them.
        for p in INSERT UPDATE DELETE; do
          [ "$(q "SELECT has_table_privilege('ira_app','model_providers','$p')")" = "f" ] \
            || fail "ira_app holds $p on model_providers after the rollback cycle; 076 did not survive it"
        done
        [ "$(q "SELECT has_table_privilege('ira_app','model_providers','SELECT')")" = "t" ] \
          || fail "ira_app lost SELECT on model_providers; the Privacy Centre cannot render"
        [ "$(q "SELECT count(*) FROM pg_trigger WHERE tgname='model_licence_register_withdrawal_gate'
                 AND NOT tgisinternal")" = "1" ] \
          || fail "the licence withdrawal trigger did not survive the rollback cycle"
        # 070's governance tables, and the two triggers that make them a
        # control rather than a schema. The promotion gate is the one a cycle
        # would lose silently: without it a dataset reaches `train` with no
        # passing checks at all, and every other assertion here still holds.
        [ "$(q "SELECT to_regclass('public.training_datasets') IS NOT NULL")" = "t" ] \
          || fail "training_datasets did not survive the rollback cycle"
        [ "$(q "SELECT to_regclass('public.dataset_checks') IS NOT NULL")" = "t" ] \
          || fail "dataset_checks did not survive the rollback cycle"
        for trg in training_dataset_promotion training_dataset_content_change; do
          [ "$(q "SELECT count(*) FROM pg_trigger WHERE tgname='$trg' AND NOT tgisinternal")" = "1" ] \
            || fail "$trg did not survive the rollback cycle; dataset promotion is ungated"
        done
        for t in training_datasets dataset_checks; do
          for p in INSERT UPDATE DELETE; do
            [ "$(q "SELECT has_table_privilege('ira_app','$t','$p')")" = "f" ] \
              || fail "ira_app holds $p on $t after the rollback cycle"
          done
        done
        # 071's run tables and the gate that decides whether training may
        # start. The gate trigger is what a cycle would lose silently: without
        # it a run reaches `approved` with no licence permitting derivatives,
        # no governed data and nobody's approval, and every other assertion
        # here still holds.
        [ "$(q "SELECT to_regclass('public.model_training_runs') IS NOT NULL")" = "t" ] \
          || fail "model_training_runs did not survive the rollback cycle"
        [ "$(q "SELECT to_regclass('public.training_run_datasets') IS NOT NULL")" = "t" ] \
          || fail "training_run_datasets did not survive the rollback cycle"
        for trg in model_training_run_gate_trigger training_run_binding_gate_trigger; do
          [ "$(q "SELECT count(*) FROM pg_trigger WHERE tgname='$trg' AND NOT tgisinternal")" = "1" ] \
            || fail "$trg did not survive the rollback cycle; training is ungated"
        done
        for t in model_training_runs training_run_datasets; do
          for p in INSERT UPDATE DELETE; do
            [ "$(q "SELECT has_table_privilege('ira_app','$t','$p')")" = "f" ] \
              || fail "ira_app holds $p on $t after the rollback cycle"
          done
        done
        # And nothing may have acquired an approval in transit.
        [ "$(q "SELECT count(*) FROM model_training_runs WHERE approved_by IS NOT NULL OR state='completed'")" = "0" ] \
          || fail "a training run is approved or completed after the rollback cycle; PP-04 is an owner decision"
        # 069's seeded evidence. The table existing is 068's property and
        # passes with every row gone — a cycle that lost the rows leaves a
        # register that still fails closed and can no longer say WHY, which is
        # the quietest possible way to lose MOD-01's answer.
        [ "$(q "SELECT count(*) FROM model_licence_register")" -ge 15 ] \
          || fail "069's seeded licence evidence did not survive the rollback cycle"
        # And none of it may have acquired a determination in transit. A
        # rollback that restored rows with classification='known_approved'
        # would hand every model a licence nobody recorded.
        [ "$(q "SELECT count(*) FROM model_licence_register
                 WHERE classification NOT IN ('unknown','review_required')")" = "0" ] \
          || fail "a seeded licence row carries a determination after the rollback cycle; only a named human may record one"
        # 051's two foreign keys are dropped and re-added BY HAND in the cycle
        # above, because rollback/051 refuses on a multi-tenant database and
        # so cannot be used there. Hand-written DDL goes stale silently, so it
        # is asserted against the definition rather than merely counted: if
        # 051 ever changes either constraint, this fails and names it.
        # Takes the TABLE as well as the constraint name. It was hardcoded
        # to `notifications`, which is invisible until a second table uses
        # it: P12-03 asked it for `briefings_tenant_id_fkey`, it looked for
        # that constraint ON NOTIFICATIONS, found nothing, and returned the
        # empty string — so the assertion failed with "does not match 081"
        # when the constraint was in fact present and correct.
        cdef() { q "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c
                     JOIN pg_class t ON t.oid=c.conrelid
                    WHERE t.relname='$1' AND c.conname='$2'"; }
        [ "$(cdef notifications notifications_workspace_id_fkey)" \
          = "FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT" ] \
          || fail "notifications_workspace_id_fkey does not match 051 after the cycle — the teardown/restore pair in the rollback step is stale"
        [ "$(cdef notifications notifications_workspace_in_tenant_fkey)" \
          = "FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE RESTRICT" ] \
          || fail "notifications_workspace_in_tenant_fkey does not match 051 after the cycle — the teardown/restore pair in the rollback step is stale"
        # 081's pair, restored by hand for the same reason and asserted the
        # same way. CASCADE, not RESTRICT: a briefing is derived data and must
        # not outlive the workspace it summarises — P12-01 finding E showed a
        # RESTRICT here becomes a teardown that cannot complete.
        [ "$(cdef briefings briefings_tenant_id_fkey)" \
          = "FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE" ] \
          || fail "briefings_tenant_id_fkey does not match 081 after the cycle — the teardown/restore pair in the rollback step is stale"
        [ "$(cdef briefings briefings_workspace_in_tenant_fkey)" \
          = "FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE" ] \
          || fail "briefings_workspace_in_tenant_fkey does not match 081 after the cycle — the teardown/restore pair in the rollback step is stale"
        # 082 creates no table either — it re-scopes an existing one — so the
        # recompose step has to say something about `heartbeat_surfaced` or a
        # peel-without-re-apply is invisible. The UNIQUE is the finding: a
        # global one suppresses another workspace's item.
        [ "$(cdef heartbeat_surfaced heartbeat_surfaced_scope_key)" \
          = "UNIQUE (tenant_id, workspace_id, item_key)" ] \
          || fail "heartbeat_surfaced lost its scoped uniqueness after the cycle — finding H, re-opened"
        [ "$(q "SELECT relforcerowsecurity FROM pg_class WHERE relname='heartbeat_surfaced'")" = "t" ] \
          || fail "heartbeat_surfaced lost FORCE row-level security in the rollback cycle"
        # 083 creates no table either — it widens a CHECK — so it is invisible
        # to a step that counts tables, and it is the one migration in this
        # group the cycle genuinely peels AND re-applies. Assert the value it
        # adds: without `consent_missing` in the vocabulary,
        # `_record_biometric_attempt` cannot record why an enrolled-but-never-
        # consented voiceprint was refused, and the write lands in a non-fatal
        # `except` that leaves the refused attempt with no audit row at all.
        [ "$(cdef biometric_audit biometric_audit_failure_reason_check)" \
          = "CHECK (((failure_reason IS NULL) OR (failure_reason = ANY (ARRAY['below_threshold'::text, 'no_enrolment'::text, 'revoked_enrolment'::text, 'unknown_claim'::text, 'rate_limited'::text, 'malformed_sample'::text, 'consent_missing'::text]))))" ] \
          || fail "biometric_audit's failure_reason vocabulary does not match 083 after the cycle"
        # 084, same shape as 083 and the same reason for asserting it here: it
        # widens a CHECK, creates no table, and is the newest layer the cycle
        # peels and re-applies. Without `unknown` in the vocabulary the end
        # route cannot record a termination it could not classify, and the
        # only value the schema would then accept for it is one that asserts
        # something nobody knows — `user_ended` (a clean hang-up that may not
        # have happened) or `error` (a failure nobody established).
        [ "$(q "SELECT count(*) FROM pg_constraint WHERE conname='realtime_sessions_end_reason_check' AND pg_get_constraintdef(oid) LIKE '%''unknown''%'")" = "1" ] \
          || fail "realtime_sessions.end_reason does not admit 'unknown' after the cycle — 084 was peeled and never re-applied"
        # …and the value must still be REFUSED when it is not in the
        # vocabulary, or the assertion above is satisfied by a dropped
        # constraint rather than a widened one.
        [ "$(q "SELECT count(*) FROM pg_constraint WHERE conname='realtime_sessions_end_reason_check'")" = "1" ] \
          || fail "realtime_sessions lost its end_reason CHECK in the cycle — the column is now free text fed from a request body"
        # 052. The cycle genuinely reverses and re-applies this one, so the
        # question is whether the script put back exactly what it declares —
        # four policies, not three and not five. Five is the dangerous count:
        # permissive policies combine with OR, so an extra tenant-only policy
        # would hand every tenant admin a read on every member's second factor
        # while looking like additional protection.
        [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.totp_secrets'::regclass")" = "4" ] \
          || fail "totp_secrets does not have exactly 4 policies after the cycle"
        [ "$(q "SELECT relforcerowsecurity FROM pg_class WHERE oid='public.totp_secrets'::regclass")" = "t" ] \
          || fail "totp_secrets lost FORCE ROW LEVEL SECURITY in the cycle"
        [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.totp_secrets'::regclass AND coalesce(pg_get_expr(polqual,polrelid),'')||coalesce(pg_get_expr(polwithcheck,polrelid),'') NOT LIKE '%app.tenant_id%'")" = "0" ] \
          || fail "a totp_secrets policy no longer constrains app.tenant_id after the cycle"
        [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.totp_secrets'::regclass AND coalesce(pg_get_expr(polqual,polrelid),'')||coalesce(pg_get_expr(polwithcheck,polrelid),'') NOT LIKE '%app.user_id%'")" = "0" ] \
          || fail "a totp_secrets policy no longer constrains app.user_id after the cycle — that is 036's tenant-admin read, reopened"
        [ "$(q "SELECT is_nullable FROM information_schema.columns WHERE table_name='totp_secrets' AND column_name='tenant_id'")" = "NO" ] \
          || fail "totp_secrets.tenant_id is not NOT NULL after the cycle"
        # 053. Same question as 052 and the same dangerous answer: five
        # policies rather than four. A tenant-only policy on user_mfa_keys
        # would let any tenant-bound connection unwrap every member's MFA DEK
        # — the decryption capability the whole design exists to withhold —
        # and it would look like additional protection while doing it.
        [ "$(q "SELECT to_regclass('public.user_mfa_keys') IS NOT NULL")" = "t" ] \
          || fail "user_mfa_keys did not survive the rollback cycle"
        [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.user_mfa_keys'::regclass")" = "4" ] \
          || fail "user_mfa_keys does not have exactly 4 policies after the cycle"
        [ "$(q "SELECT relforcerowsecurity FROM pg_class WHERE oid='public.user_mfa_keys'::regclass")" = "t" ] \
          || fail "user_mfa_keys lost FORCE ROW LEVEL SECURITY in the cycle"
        [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.user_mfa_keys'::regclass AND coalesce(pg_get_expr(polqual,polrelid),'')||coalesce(pg_get_expr(polwithcheck,polrelid),'') NOT LIKE '%app.user_id%'")" = "0" ] \
          || fail "a user_mfa_keys policy no longer constrains app.user_id after the cycle — that hands a tenant admin the key to every member's second factor"
        [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.user_mfa_keys'::regclass AND coalesce(pg_get_expr(polqual,polrelid),'')||coalesce(pg_get_expr(polwithcheck,polrelid),'') NOT LIKE '%app.tenant_id%'")" = "0" ] \
          || fail "a user_mfa_keys policy no longer constrains app.tenant_id after the cycle"
        # The seed's nonce column, without which the ciphertext columns 036
        # added cannot be used at all — AES-GCM needs a nonce and it is not
        # derivable. Its absence is why those columns sat unused for a phase.
        [ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='totp_secrets' AND column_name='secret_nonce'")" = "1" ] \
          || fail "totp_secrets.secret_nonce is missing after the cycle"
        [ "$(q "SELECT is_nullable FROM information_schema.columns WHERE table_name='totp_secrets' AND column_name='secret'")" = "YES" ] \
          || fail "totp_secrets.secret is NOT NULL after the cycle — an encrypted row cannot then clear its plaintext"
        for c in totp_secrets_has_a_secret totp_secrets_encrypted_is_complete; do
          [ "$(q "SELECT count(*) FROM pg_constraint WHERE conrelid='public.totp_secrets'::regclass AND conname='$c'")" = "1" ] \
            || fail "$c is missing after the cycle"
        done
        for c in user_mfa_keys_destroyed_is_empty user_mfa_keys_live_is_complete; do
          [ "$(q "SELECT count(*) FROM pg_constraint WHERE conrelid='public.user_mfa_keys'::regclass AND conname='$c'")" = "1" ] \
            || fail "$c is missing after the cycle"
        done
        # 054. Same policy-count question as 052 and 053, and here the thing
        # an extra permissive policy would widen access to is password hashes.
        [ "$(q "SELECT to_regclass('public.user_credentials') IS NOT NULL")" = "t" ] \
          || fail "user_credentials did not survive the rollback cycle"
        [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.user_credentials'::regclass")" = "4" ] \
          || fail "user_credentials does not have exactly 4 policies after the cycle"
        [ "$(q "SELECT relforcerowsecurity FROM pg_class WHERE oid='public.user_credentials'::regclass")" = "t" ] \
          || fail "user_credentials lost FORCE ROW LEVEL SECURITY in the cycle"
        # The revoke, not the grant. Default privileges hand every new table to
        # ira_worker, so this is opt-OUT — and a rollback cycle that restored
        # the table without re-revoking would hand the worker password hashes
        # while every other assertion passed.
        [ "$(q "SELECT has_table_privilege('ira_worker','user_credentials','SELECT')")" = "f" ] \
          || fail "ira_worker can read user_credentials after the cycle — the REVOKE did not survive"
        [ "$(q "SELECT has_table_privilege('ira_worker','email_verification_tokens','SELECT')")" = "f" ] \
          || fail "ira_worker can read email_verification_tokens after the cycle"
        [ "$(q "SELECT has_table_privilege('ira_app','user_credentials','SELECT')")" = "t" ] \
          || fail "ira_app lost SELECT on user_credentials after the cycle"
        # 055. Same policy-count question as 052/053/054, and one more that
        # is specific to this table: exactly ONE policy may be keyed on
        # possession, and it must be SELECT. A bearer-keyed UPDATE would let
        # the holder of an invitation link mutate the row that authorises them.
        [ "$(q "SELECT to_regclass('public.tenant_invitations') IS NOT NULL")" = "t" ] \
          || fail "tenant_invitations did not survive the rollback cycle"
        [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.tenant_invitations'::regclass")" = "5" ] \
          || fail "tenant_invitations does not have exactly 5 policies after the cycle"
        [ "$(q "SELECT relforcerowsecurity FROM pg_class WHERE oid='public.tenant_invitations'::regclass")" = "t" ] \
          || fail "tenant_invitations lost FORCE ROW LEVEL SECURITY in the cycle"
        [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.tenant_invitations'::regclass AND coalesce(pg_get_expr(polqual,polrelid),'')||coalesce(pg_get_expr(polwithcheck,polrelid),'') LIKE '%app.invitation_token_hash%' AND polcmd <> 'r'")" = "0" ] \
          || fail "a bearer-keyed tenant_invitations policy is not SELECT after the cycle — possession of a link must not authorise a write"
        [ "$(q "SELECT has_table_privilege('ira_worker','tenant_invitations','SELECT')")" = "f" ] \
          || fail "ira_worker can read tenant_invitations after the cycle — the REVOKE did not survive"
        # `primary_owner` must still be uninvitable. Without this the founder
        # protection could be lost to a recompose and every other assertion
        # here would still pass.
        [ "$(q "SELECT pg_get_constraintdef(oid) LIKE '%primary_owner%' FROM pg_constraint WHERE conrelid='public.tenant_invitations'::regclass AND conname='tenant_invitations_role_check'")" = "f" ] \
          || fail "primary_owner became an invitable role after the cycle"
        # And the display name 055 adds must NOT acquire a unique index — it
        # holds customer text, so uniqueness would make two customers sharing
        # a name a failure that discloses the other one.
        [ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='tenants' AND column_name='display_name'")" = "1" ] \
          || fail "tenants.display_name is missing after the cycle"
        # 056. Both predicates on every policy is the property that matters:
        # a tenant-only policy would hand every tenant administrator their
        # members' recovery codes, which is a second factor.
        [ "$(q "SELECT to_regclass('public.mfa_recovery_codes') IS NOT NULL")" = "t" ] \
          || fail "mfa_recovery_codes did not survive the rollback cycle"
        [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.mfa_recovery_codes'::regclass")" = "4" ] \
          || fail "mfa_recovery_codes does not have exactly 4 policies after the cycle"
        [ "$(q "SELECT relforcerowsecurity FROM pg_class WHERE oid='public.mfa_recovery_codes'::regclass")" = "t" ] \
          || fail "mfa_recovery_codes lost FORCE ROW LEVEL SECURITY in the cycle"
        [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.mfa_recovery_codes'::regclass AND (coalesce(pg_get_expr(polqual,polrelid),'')||coalesce(pg_get_expr(polwithcheck,polrelid),'') NOT LIKE '%app.user_id%' OR coalesce(pg_get_expr(polqual,polrelid),'')||coalesce(pg_get_expr(polwithcheck,polrelid),'') NOT LIKE '%app.tenant_id%')")" = "0" ] \
          || fail "an mfa_recovery_codes policy no longer constrains BOTH app.user_id and app.tenant_id after the cycle"
        [ "$(q "SELECT has_table_privilege('ira_worker','mfa_recovery_codes','SELECT')")" = "f" ] \
          || fail "ira_worker can read mfa_recovery_codes after the cycle — the REVOKE did not survive"
        # 057. The property that matters is a NEGATIVE one: 023's tenant-only
        # policy must be absent. A cycle that peeled 057 and did not re-apply
        # it would leave `tenant_isolation` in place and every count below
        # would still look plausible — one policy on a table that had one
        # before.
        for T in user_sessions user_devices; do
          [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.${T}'::regclass AND polname='tenant_isolation'")" = "0" ] \
            || fail "${T} carries 023's tenant-only policy after the cycle — every member of a tenant can read and revoke every other member's sessions, and erasing one account deletes them all"
          [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.${T}'::regclass")" = "4" ] \
            || fail "${T} does not have exactly 4 policies after the cycle"
          [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.${T}'::regclass AND (coalesce(pg_get_expr(polqual,polrelid),'')||coalesce(pg_get_expr(polwithcheck,polrelid),'') NOT LIKE '%app.user_id%' OR coalesce(pg_get_expr(polqual,polrelid),'')||coalesce(pg_get_expr(polwithcheck,polrelid),'') NOT LIKE '%app.tenant_id%')")" = "0" ] \
            || fail "a ${T} policy no longer constrains BOTH app.user_id and app.tenant_id after the cycle"
          [ "$(q "SELECT has_table_privilege('ira_worker','${T}','SELECT')")" = "f" ] \
            || fail "ira_worker can read ${T} after the cycle — the REVOKE did not survive"
        done
        # 058. Possession is the predicate, and `app.reset_user_id` reaches
        # DELETE and nothing else.
        [ "$(q "SELECT to_regclass('public.password_reset_tokens') IS NOT NULL")" = "t" ] \
          || fail "password_reset_tokens did not survive the rollback cycle"
        [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.password_reset_tokens'::regclass")" = "4" ] \
          || fail "password_reset_tokens does not have exactly 4 policies after the cycle"
        [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.password_reset_tokens'::regclass AND coalesce(pg_get_expr(polqual,polrelid),'')||coalesce(pg_get_expr(polwithcheck,polrelid),'') NOT LIKE '%app.reset_token_hash%'")" = "0" ] \
          || fail "a password_reset_tokens policy no longer keys on possession of the token after the cycle"
        [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.password_reset_tokens'::regclass AND polcmd IN ('a','w') AND coalesce(pg_get_expr(polqual,polrelid),'')||coalesce(pg_get_expr(polwithcheck,polrelid),'') LIKE '%app.reset_user_id%'")" = "0" ] \
          || fail "a password_reset_tokens INSERT/UPDATE policy accepts app.reset_user_id after the cycle"
        [ "$(q "SELECT has_table_privilege('ira_worker','password_reset_tokens','SELECT')")" = "f" ] \
          || fail "ira_worker can read password_reset_tokens after the cycle — the REVOKE did not survive"
        # 059. The identity key survives, and email is still not one.
        [ "$(q "SELECT to_regclass('public.external_identities') IS NOT NULL")" = "t" ] \
          || fail "external_identities did not survive the rollback cycle"
        [ "$(q "SELECT to_regclass('public.external_identities_issuer_subject_key') IS NOT NULL")" = "t" ] \
          || fail "the (issuer, subject) unique index is missing after the cycle"
        [ "$(q "SELECT count(*) FROM pg_index i WHERE i.indrelid='public.external_identities'::regclass AND i.indisunique AND pg_get_indexdef(i.indexrelid) LIKE '%claimed_email%'")" = "0" ] \
          || fail "a UNIQUE index covers claimed_email after the cycle — email is not an identity"
        [ "$(q "SELECT count(*) FROM pg_trigger WHERE tgrelid='public.external_identities'::regclass AND tgname='external_identity_pin_key_trg'")" = "1" ] \
          || fail "the external_identities immutability trigger did not survive the cycle"
        [ "$(q "SELECT has_table_privilege('ira_worker','external_identities','SELECT')")" = "f" ] \
          || fail "ira_worker can read external_identities after the cycle"
        # 060. The tenant-only policy must be gone from both tables.
        for T in connector_accounts companion_preferences; do
          [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.${T}'::regclass AND polname='tenant_isolation'")" = "0" ] \
            || fail "${T} carries 023's tenant-only policy after the cycle"
          [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.${T}'::regclass")" = "4" ] \
            || fail "${T} does not have exactly 4 policies after the cycle"
          [ "$(q "SELECT has_table_privilege('ira_worker','${T}','SELECT')")" = "f" ] \
            || fail "ira_worker can read ${T} after the cycle"
        done
        # 061. Three policies and no fourth: the ABSENCE of a DELETE policy is
        # the protection, and an absence is exactly what a recompose loses
        # without noticing — a policy that fails to be recreated leaves a
        # table that looks fine and denies nothing extra.
        [ "$(q "SELECT to_regclass('public.auth_events') IS NOT NULL")" = "t" ] \
          || fail "auth_events did not survive the rollback cycle"
        [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.auth_events'::regclass")" = "4" ] \
          || fail "auth_events does not have exactly 4 policies after the cycle"
        [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.auth_events'::regclass AND polcmd='d'")" = "0" ] \
          || fail "auth_events gained a DELETE policy across the cycle"
        [ "$(q "SELECT count(*) FROM pg_trigger WHERE tgrelid='public.auth_events'::regclass AND tgname='auth_events_append_only_trg'")" = "1" ] \
          || fail "the auth_events append-only trigger did not survive the cycle"
        [ "$(q "SELECT has_table_privilege('ira_app','auth_events','DELETE')")" = "f" ] \
          || fail "ira_app holds DELETE on auth_events after the cycle — the REVOKE did not survive"
        [ "$(q "SELECT has_table_privilege('ira_worker','auth_events','SELECT')")" = "f" ] \
          || fail "ira_worker can read auth_events after the cycle"
        # 062. The key columns are what make an encrypted template openable,
        # and a recompose that silently lost them would leave a schema that
        # looks complete and cannot decrypt anything.
        for C in template_nonce wrapped_dek wrap_nonce kek_id; do
          [ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='voice_profiles' AND column_name='${C}'")" = "1" ] \
            || fail "voice_profiles.${C} did not survive the rollback cycle"
        done
        [ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='voice_profiles' AND column_name='embedding'")" = "1" ] \
          || fail "the plaintext voice template column was lost in the cycle — 063 drops it, not 062"
        [ "$(q "SELECT count(*) FROM pg_constraint WHERE conname='voice_profiles_shredded_holds_no_key'")" = "1" ] \
          || fail "the crypto-shred guard did not survive the cycle"
        [ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='voice_migration_status'")" = "9" ] \
          || fail "voice_migration_status is not 062's shape after the cycle"
        # The plaintext columns must still be PRESENT after the cycle. 063 is
        # not a chain migration, so a recomposed chain is a PRE-contract
        # schema — and a cycle that dropped them would have destroyed the
        # upgrade path the encryption workers need.
        [ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='totp_secrets' AND column_name='secret'")" = "1" ] \
          || fail "totp_secrets.secret is gone after the cycle — the chain must not contract"
        [ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='voice_profiles' AND column_name='embedding'")" = "1" ] \
          || fail "voice_profiles.embedding is gone after the cycle — the chain must not contract"
        echo "✅ recomposed schema matches a fresh chain"

# ───────────────────────────────────────────────────────────────────────────
# Secret scanning (Phase 0 — Security Baseline).
#
# A detected credential FAILS this job, which fails the build. This replaces a
# grep over four literal strings in one file that could only ever print a
# warning. Config + allowlist policy: .gitleaks.toml at the repo root.
# ───────────────────────────────────────────────────────────────────────────
