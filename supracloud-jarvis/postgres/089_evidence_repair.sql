-- =============================================================================
-- 089_evidence_repair.sql — versioned evidence, dependencies and approvals for
--                           changed-evidence repair (F1, on a minimum F3)
--
-- When a source document is corrected, completed work that depended on it may
-- now be wrong. Answering "which work, why, and what is the smallest repair" needs
-- four things this schema did not have:
--
--   * every imported version kept, byte for byte, with its hash
--   * claims extracted from a version, each with the exact bytes it came from
--   * dependencies from claims to the outputs that used them
--   * approvals bound to the exact output, input versions and policy they saw
--
-- ── Invariants, and where each is enforced ─────────────────────────────────
--
-- 1. **Earlier evidence is never silently overwritten.** The runtime roles
--    have NO UPDATE and NO DELETE on `evidence_artifact_versions` or
--    `evidence_audit_events`. A new version is a new row. Elsewhere UPDATE is
--    granted per COLUMN, naming only the status columns that legitimately
--    move. Enforced by privilege, not trigger, so a tenant or workspace
--    deletion still cascades — erasure must not be blocked by an append-only
--    rule meant for the application.
--
-- 2. **Content matches its hash.** A CHECK recomputes sha256 of the stored
--    bytes. A row whose content and hash disagree cannot exist.
--
-- 3. **Nothing crosses a workspace.** Every table carries tenant_id and
--    workspace_id, forced RLS on both, and composite foreign keys that include
--    them — so a claim cannot point at another workspace's version even through
--    a guessed UUID.
--
-- 4. **No runtime DELETE.** No delete policy and no DELETE grant on any table
--    here. Rows leave only with their workspace or tenant.
--
-- ── Approval capability (F1 design decision D2) ─────────────────────────────
--
-- Applying a repair is an approval of changed work. `040` has no capability
-- for that, and the nearest, `workspace.write`, is carried by every member.
-- This adds `workspace.approve_changes`, granted to `workspace_admin` only,
-- which requires widening 040's CHECK on `workspace_role_capabilities`.
-- =============================================================================

BEGIN;

-- ── Capability ───────────────────────────────────────────────────────────────

ALTER TABLE workspace_role_capabilities
    DROP CONSTRAINT IF EXISTS workspace_role_capabilities_capability_check;
ALTER TABLE workspace_role_capabilities
    ADD CONSTRAINT workspace_role_capabilities_capability_check
    CHECK (capability IN (
        'workspace.read',
        'workspace.write',
        'workspace.manage_members',
        'workspace.manage_settings',
        'workspace.delete_content',
        'workspace.approve_changes'));

-- The table is reference data under FORCED row-level security with a read-only
-- policy, so even its owner cannot insert. Lift the force for this one row and
-- restore it in the same transaction; nothing else can observe the gap.
ALTER TABLE workspace_role_capabilities NO FORCE ROW LEVEL SECURITY;
INSERT INTO workspace_role_capabilities (role, capability) VALUES
    ('workspace_admin', 'workspace.approve_changes')
ON CONFLICT (role, capability) DO NOTHING;
ALTER TABLE workspace_role_capabilities FORCE ROW LEVEL SECURITY;

-- ── 1. Artifacts ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS evidence_artifacts (
    artifact_id     UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    kind            TEXT NOT NULL CHECK (kind IN ('source', 'output')),
    title           TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 500),
    collection_key  TEXT NOT NULL DEFAULT 'default'
                        CHECK (length(collection_key) BETWEEN 1 AND 200),
    access_mode     TEXT NOT NULL DEFAULT 'workspace'
                        CHECK (access_mode IN ('workspace', 'restricted')),
    access_epoch    INTEGER NOT NULL DEFAULT 0 CHECK (access_epoch >= 0),
    head_version    INTEGER NOT NULL DEFAULT 0 CHECK (head_version >= 0),
    created_by      UUID NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at      TIMESTAMPTZ,

    CONSTRAINT evidence_artifacts_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT evidence_artifacts_scoped_key
        UNIQUE (tenant_id, workspace_id, artifact_id)
);

CREATE INDEX IF NOT EXISTS evidence_artifacts_collection
    ON evidence_artifacts (tenant_id, workspace_id, collection_key);

-- ── 2. Artifact versions ─────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS evidence_artifact_versions (
    tenant_id         UUID NOT NULL
                          DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    workspace_id      UUID NOT NULL
                          DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    artifact_id       UUID NOT NULL,
    version           INTEGER NOT NULL CHECK (version >= 1),
    content_hash      TEXT NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
    content           BYTEA,
    purged_at         TIMESTAMPTZ,
    size_bytes        BIGINT NOT NULL CHECK (size_bytes >= 0),
    media_type        TEXT NOT NULL
                          CHECK (media_type IN ('text/plain', 'text/markdown', 'text/csv')),
    source_type       TEXT NOT NULL
                          CHECK (source_type IN ('import', 'generated', 'human_edit',
                                                 'patch', 'revert')),
    parent_version    INTEGER CHECK (parent_version IS NULL OR parent_version < version),
    imported_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    imported_by       UUID NOT NULL,
    retention_rule    TEXT NOT NULL DEFAULT 'retain'
                          CHECK (length(retention_rule) BETWEEN 1 AND 100),
    extractor_version TEXT NOT NULL CHECK (length(extractor_version) BETWEEN 1 AND 100),
    local_content_ref TEXT GENERATED ALWAYS AS
                          ('evidence:' || artifact_id::text || '@' || version::text) STORED,

    PRIMARY KEY (artifact_id, version),
    CONSTRAINT evidence_versions_scoped_key
        UNIQUE (tenant_id, workspace_id, artifact_id, version),
    CONSTRAINT evidence_versions_artifact_fkey
        FOREIGN KEY (tenant_id, workspace_id, artifact_id)
        REFERENCES evidence_artifacts(tenant_id, workspace_id, artifact_id) ON DELETE CASCADE,
    -- Purge (not implemented in v1) is the only way content may be absent.
    CONSTRAINT evidence_versions_content_or_purged
        CHECK ((content IS NULL) = (purged_at IS NOT NULL)),
    CONSTRAINT evidence_versions_hash_matches_content
        CHECK (content IS NULL
               OR content_hash = 'sha256:' || encode(sha256(content), 'hex')),
    CONSTRAINT evidence_versions_size_matches_content
        CHECK (content IS NULL OR size_bytes = octet_length(content))
);

-- ── 3. Claims ────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS evidence_claims (
    claim_id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id             UUID NOT NULL
                              DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    workspace_id          UUID NOT NULL
                              DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    artifact_id           UUID NOT NULL,
    version               INTEGER NOT NULL,
    claim_key             TEXT NOT NULL CHECK (length(claim_key) BETWEEN 1 AND 1000),
    kind                  TEXT NOT NULL CHECK (kind IN ('field', 'table_cell', 'statement')),
    value_text            TEXT NOT NULL,
    value_type            TEXT NOT NULL
                              CHECK (value_type IN ('date', 'quantity', 'text',
                                                    'statement', 'ambiguous')),
    value_norm            JSONB NOT NULL,
    units                 TEXT,
    valid_from            DATE,
    valid_until           DATE,
    span_start            INTEGER NOT NULL CHECK (span_start >= 0),
    span_end              INTEGER NOT NULL,
    line_no               INTEGER NOT NULL CHECK (line_no >= 1),
    status                TEXT NOT NULL DEFAULT 'observed'
                              CHECK (status IN ('observed', 'inferred', 'disputed')),
    reviewer              UUID,
    provenance_confidence TEXT NOT NULL CHECK (provenance_confidence IN ('high', 'medium', 'low')),
    extractor_version     TEXT NOT NULL CHECK (length(extractor_version) BETWEEN 1 AND 100),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT evidence_claims_span_ordered CHECK (span_end >= span_start),
    CONSTRAINT evidence_claims_valid_time_ordered
        CHECK (valid_from IS NULL OR valid_until IS NULL OR valid_from <= valid_until),
    CONSTRAINT evidence_claims_key_per_version UNIQUE (artifact_id, version, claim_key),
    CONSTRAINT evidence_claims_version_fkey
        FOREIGN KEY (tenant_id, workspace_id, artifact_id, version)
        REFERENCES evidence_artifact_versions(tenant_id, workspace_id, artifact_id, version)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS evidence_claims_lineage
    ON evidence_claims (tenant_id, workspace_id, artifact_id, claim_key);

-- ── 4. Output spans (a bound region of an output version) ────────────────────

CREATE TABLE IF NOT EXISTS evidence_output_spans (
    tenant_id         UUID NOT NULL
                          DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    workspace_id      UUID NOT NULL
                          DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    span_id           UUID NOT NULL,
    artifact_id       UUID NOT NULL,
    version           INTEGER NOT NULL,
    span_start        INTEGER NOT NULL CHECK (span_start >= 0),
    span_end          INTEGER NOT NULL,
    section_key       TEXT NOT NULL DEFAULT '' CHECK (length(section_key) <= 500),
    calc              JSONB NOT NULL,
    protected         BOOLEAN NOT NULL DEFAULT false,
    protection_reason TEXT,
    created_by        UUID NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (span_id, artifact_id, version),
    CONSTRAINT evidence_spans_ordered CHECK (span_end >= span_start),
    CONSTRAINT evidence_spans_protection_has_reason
        CHECK (protected = (protection_reason IS NOT NULL)),
    CONSTRAINT evidence_spans_version_fkey
        FOREIGN KEY (tenant_id, workspace_id, artifact_id, version)
        REFERENCES evidence_artifact_versions(tenant_id, workspace_id, artifact_id, version)
        ON DELETE CASCADE
);

-- ── 5. Dependencies ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS evidence_dependencies (
    dependency_id       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           UUID NOT NULL
                            DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    workspace_id        UUID NOT NULL
                            DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    from_type           TEXT NOT NULL CHECK (from_type IN ('claim', 'span', 'artifact')),
    from_artifact_id    UUID NOT NULL,
    from_key            TEXT NOT NULL CHECK (length(from_key) BETWEEN 1 AND 1000),
    to_type             TEXT NOT NULL CHECK (to_type IN ('claim', 'span', 'artifact', 'approval')),
    to_artifact_id      UUID NOT NULL,
    to_key              TEXT NOT NULL CHECK (length(to_key) BETWEEN 1 AND 1000),
    relationship_type   TEXT NOT NULL
                            CHECK (relationship_type IN ('supports', 'contradicts',
                                                         'calculated_from', 'supersedes',
                                                         'requires_approval')),
    supporting_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    extractor_version   TEXT NOT NULL CHECK (length(extractor_version) BETWEEN 1 AND 100),
    human_confirmed     BOOLEAN NOT NULL DEFAULT false,
    status              TEXT NOT NULL CHECK (status IN ('confirmed', 'inferred', 'rejected')),
    confidence_category TEXT NOT NULL CHECK (confidence_category IN ('high', 'medium', 'low')),
    reason              TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 2000),
    superseded_by       UUID,
    created_by          UUID NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- A model-proposed link is never silently a confirmed one.
    CONSTRAINT evidence_dependencies_inferred_not_confirmed
        CHECK (status <> 'inferred' OR NOT human_confirmed),
    CONSTRAINT evidence_dependencies_from_fkey
        FOREIGN KEY (tenant_id, workspace_id, from_artifact_id)
        REFERENCES evidence_artifacts(tenant_id, workspace_id, artifact_id) ON DELETE CASCADE,
    CONSTRAINT evidence_dependencies_to_fkey
        FOREIGN KEY (tenant_id, workspace_id, to_artifact_id)
        REFERENCES evidence_artifacts(tenant_id, workspace_id, artifact_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS evidence_dependencies_from
    ON evidence_dependencies (tenant_id, workspace_id, from_artifact_id, from_key);
CREATE INDEX IF NOT EXISTS evidence_dependencies_to
    ON evidence_dependencies (tenant_id, workspace_id, to_artifact_id, to_key);

-- ── 6. Patches (drafts — never applied by being written) ─────────────────────

CREATE TABLE IF NOT EXISTS evidence_patches (
    patch_id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id              UUID NOT NULL
                               DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    workspace_id           UUID NOT NULL
                               DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    output_artifact_id     UUID NOT NULL,
    base_version           INTEGER NOT NULL,
    base_content_hash      TEXT NOT NULL CHECK (base_content_hash ~ '^sha256:[0-9a-f]{64}$'),
    source_version_set     JSONB NOT NULL,
    dependency_fingerprint TEXT NOT NULL CHECK (dependency_fingerprint ~ '^[0-9a-f]{64}$'),
    change_set             JSONB NOT NULL,
    hunks                  JSONB NOT NULL,
    review_items           JSONB NOT NULL DEFAULT '[]'::jsonb,
    review_fingerprint     TEXT NOT NULL CHECK (review_fingerprint ~ '^[0-9a-f]{64}$'),
    status                 TEXT NOT NULL
                               CHECK (status IN ('draft', 'blocked', 'applied',
                                                 'rejected', 'stale')),
    applied_version        INTEGER,
    created_by             UUID NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_by             UUID,
    decided_at             TIMESTAMPTZ,

    CONSTRAINT evidence_patches_decision_complete
        CHECK ((decided_by IS NULL) = (decided_at IS NULL)),
    CONSTRAINT evidence_patches_applied_has_version
        CHECK ((status = 'applied') = (applied_version IS NOT NULL)),
    CONSTRAINT evidence_patches_base_fkey
        FOREIGN KEY (tenant_id, workspace_id, output_artifact_id, base_version)
        REFERENCES evidence_artifact_versions(tenant_id, workspace_id, artifact_id, version)
        ON DELETE CASCADE,
    CONSTRAINT evidence_patches_scoped_key UNIQUE (tenant_id, workspace_id, patch_id)
);

-- ── 7. Approvals ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS evidence_approvals (
    approval_id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id              UUID NOT NULL
                               DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    workspace_id           UUID NOT NULL
                               DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    output_artifact_id     UUID NOT NULL,
    output_version         INTEGER NOT NULL,
    output_content_hash    TEXT NOT NULL CHECK (output_content_hash ~ '^sha256:[0-9a-f]{64}$'),
    source_version_set     JSONB NOT NULL,
    dependency_fingerprint TEXT NOT NULL CHECK (dependency_fingerprint ~ '^[0-9a-f]{64}$'),
    policy_version         TEXT NOT NULL CHECK (length(policy_version) BETWEEN 1 AND 200),
    signer                 UUID NOT NULL,
    approval_scope         JSONB NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'valid'
                               CHECK (status IN ('valid', 'needs_rereview',
                                                 'superseded', 'revoked')),
    status_reason          TEXT,
    patch_id               UUID,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status_changed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT evidence_approvals_reason_when_not_valid
        CHECK (status = 'valid' OR status_reason IS NOT NULL),
    CONSTRAINT evidence_approvals_output_fkey
        FOREIGN KEY (tenant_id, workspace_id, output_artifact_id, output_version)
        REFERENCES evidence_artifact_versions(tenant_id, workspace_id, artifact_id, version)
        ON DELETE CASCADE,
    CONSTRAINT evidence_approvals_patch_fkey
        FOREIGN KEY (tenant_id, workspace_id, patch_id)
        REFERENCES evidence_patches(tenant_id, workspace_id, patch_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS evidence_approvals_output
    ON evidence_approvals (tenant_id, workspace_id, output_artifact_id, status);

-- ── 8. Per-artifact access ───────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS evidence_artifact_access (
    tenant_id     UUID NOT NULL
                      DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    workspace_id  UUID NOT NULL
                      DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    artifact_id   UUID NOT NULL,
    user_id       UUID NOT NULL,
    granted_by    UUID NOT NULL,
    granted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_by    UUID,
    revoked_at    TIMESTAMPTZ,

    PRIMARY KEY (artifact_id, user_id, granted_at),
    CONSTRAINT evidence_access_revocation_complete
        CHECK ((revoked_by IS NULL) = (revoked_at IS NULL)),
    CONSTRAINT evidence_access_revoked_after_grant
        CHECK (revoked_at IS NULL OR revoked_at >= granted_at),
    CONSTRAINT evidence_access_artifact_fkey
        FOREIGN KEY (tenant_id, workspace_id, artifact_id)
        REFERENCES evidence_artifacts(tenant_id, workspace_id, artifact_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS evidence_access_one_active_grant
    ON evidence_artifact_access (artifact_id, user_id) WHERE revoked_at IS NULL;

-- ── 9. Audit events (hash-chained per workspace; also the idempotency record) ─

CREATE TABLE IF NOT EXISTS evidence_audit_events (
    event_id        UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    workspace_id    UUID NOT NULL
                        DEFAULT NULLIF(current_setting('app.workspace_id', true), '')::uuid,
    seq             BIGINT NOT NULL CHECK (seq >= 1),
    actor           UUID NOT NULL,
    at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    action          TEXT NOT NULL CHECK (action ~ '^[a-z_.]{3,64}$'),
    decision        TEXT NOT NULL CHECK (decision IN ('allowed', 'refused', 'recorded')),
    reason_code     TEXT NOT NULL CHECK (length(reason_code) BETWEEN 1 AND 100),
    object_hashes   JSONB NOT NULL,
    correlation_id  TEXT CHECK (correlation_id IS NULL OR length(correlation_id) <= 200),
    idempotency_key TEXT CHECK (idempotency_key IS NULL
                                OR length(idempotency_key) BETWEEN 1 AND 200),
    request_hash    TEXT CHECK (request_hash IS NULL OR request_hash ~ '^[0-9a-f]{64}$'),
    result          JSONB,
    prev_hash       TEXT NOT NULL CHECK (prev_hash ~ '^[0-9a-f]{64}$'),
    hash            TEXT NOT NULL CHECK (hash ~ '^[0-9a-f]{64}$'),

    CONSTRAINT evidence_audit_idempotency_complete
        CHECK ((idempotency_key IS NULL) = (request_hash IS NULL)),
    CONSTRAINT evidence_audit_seq_per_workspace UNIQUE (tenant_id, workspace_id, seq),
    CONSTRAINT evidence_audit_workspace_in_tenant_fkey
        FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS evidence_audit_idempotency
    ON evidence_audit_events (tenant_id, workspace_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- ── Row-level security: tenant AND workspace, forced, on every table ─────────
--
-- SELECT and INSERT everywhere. UPDATE only where a column grant below names
-- something that may move. No DELETE policy anywhere.

DO $$
DECLARE
    t TEXT;
    pred CONSTANT TEXT :=
        'tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid '
        'AND workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid';
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'evidence_artifacts', 'evidence_artifact_versions', 'evidence_claims',
        'evidence_output_spans', 'evidence_dependencies', 'evidence_patches',
        'evidence_approvals', 'evidence_artifact_access', 'evidence_audit_events']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_select', t);
        EXECUTE format('CREATE POLICY %I ON %I FOR SELECT USING (%s)', t || '_select', t, pred);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_insert', t);
        EXECUTE format('CREATE POLICY %I ON %I FOR INSERT WITH CHECK (%s)', t || '_insert', t, pred);
        IF t NOT IN ('evidence_artifact_versions', 'evidence_audit_events') THEN
            EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_update', t);
            EXECUTE format('CREATE POLICY %I ON %I FOR UPDATE USING (%s) WITH CHECK (%s)',
                           t || '_update', t, pred, pred);
        END IF;
        EXECUTE format('REVOKE ALL ON %I FROM PUBLIC', t);
        EXECUTE format('REVOKE ALL ON %I FROM ira_app, ira_worker', t);
        EXECUTE format('GRANT SELECT, INSERT ON %I TO ira_app, ira_worker', t);
    END LOOP;
END $$;

-- ── Column-level UPDATE: exactly what may move, nothing else ─────────────────

GRANT UPDATE (head_version, access_epoch, access_mode, deleted_at)
    ON evidence_artifacts TO ira_app, ira_worker;
GRANT UPDATE (status, reviewer)
    ON evidence_claims TO ira_app, ira_worker;
GRANT UPDATE (protected, protection_reason)
    ON evidence_output_spans TO ira_app, ira_worker;
GRANT UPDATE (status, human_confirmed, superseded_by)
    ON evidence_dependencies TO ira_app, ira_worker;
GRANT UPDATE (status, applied_version, decided_by, decided_at)
    ON evidence_patches TO ira_app, ira_worker;
GRANT UPDATE (status, status_reason, status_changed_at)
    ON evidence_approvals TO ira_app, ira_worker;
GRANT UPDATE (revoked_by, revoked_at)
    ON evidence_artifact_access TO ira_app, ira_worker;

-- ── Self-check ───────────────────────────────────────────────────────────────

DO $$
DECLARE
    missing INTEGER;
BEGIN
    SELECT count(*) INTO missing
      FROM unnest(ARRAY[
          'evidence_artifacts', 'evidence_artifact_versions', 'evidence_claims',
          'evidence_output_spans', 'evidence_dependencies', 'evidence_patches',
          'evidence_approvals', 'evidence_artifact_access', 'evidence_audit_events']) AS t(name)
     WHERE NOT EXISTS (
          SELECT 1 FROM pg_class c
           WHERE c.relname = t.name AND c.relrowsecurity AND c.relforcerowsecurity);
    IF missing > 0 THEN
        RAISE EXCEPTION '089 FAILED: % evidence table(s) lack forced row-level security', missing;
    END IF;

    IF has_table_privilege('ira_app', 'evidence_artifact_versions', 'UPDATE')
       OR has_table_privilege('ira_app', 'evidence_artifact_versions', 'DELETE')
       OR has_table_privilege('ira_app', 'evidence_audit_events', 'UPDATE')
       OR has_table_privilege('ira_app', 'evidence_audit_events', 'DELETE') THEN
        RAISE EXCEPTION '089 FAILED: the runtime role can rewrite evidence versions or audit events';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM workspace_role_capabilities
                    WHERE role = 'workspace_admin'
                      AND capability = 'workspace.approve_changes') THEN
        RAISE EXCEPTION '089 FAILED: workspace.approve_changes is not granted to workspace_admin';
    END IF;
END $$;

COMMIT;
