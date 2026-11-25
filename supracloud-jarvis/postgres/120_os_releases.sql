-- =============================================================================
-- 120_os_releases.sql — IRA CARE OS: the over-the-air update catalogue (E4.d)
--
--   os_releases
--     One row per IRA CARE OS image the deployment may offer to its watches: the
--     version, the build fingerprint, the rollback index, and the A/B payload's
--     location, size and SHA-256. Not the image itself (that is served from object
--     storage at payload_url), and not anyone's data: a deployment-wide catalogue,
--     like model_registry (067), so it carries no tenant or workspace column and no
--     row-level security.
--
--     Publishing, staging (rollout_percent) and withdrawing a release are operator
--     actions through scripts/os_release.py with the migrator's credentials. The runtime
--     role may only READ it: the device-facing route (POST /care/devices/me/os-update)
--     chooses among published releases and signs the answer; nothing a device or a care
--     workspace can do writes here.
--
-- Rollback: rollback/120_os_releases_down.sql (refuses while any release exists)
-- =============================================================================

BEGIN;

CREATE TABLE os_releases (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    version           TEXT NOT NULL UNIQUE CHECK (version ~ '^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$'),
    build_fingerprint TEXT NOT NULL UNIQUE CHECK (length(build_fingerprint) BETWEEN 8 AND 256),
    -- The verified-boot rollback index the image carries. A device is never offered an
    -- image whose index is lower than its own (care/ota.py), and the bootloader refuses
    -- one anyway (E4.e); the server keeps the two in step.
    rollback_index    INTEGER NOT NULL CHECK (rollback_index >= 0),
    payload_url       TEXT NOT NULL CHECK (payload_url ~ '^https://'),
    payload_sha256    TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    payload_size      BIGINT NOT NULL CHECK (payload_size > 0),
    -- Staged rollout: a device is in the stage when its bucket (a hash of the release
    -- and the device, 0 to 99) is below this. 0 publishes to nobody; 100 to every device.
    rollout_percent   SMALLINT NOT NULL DEFAULT 0 CHECK (rollout_percent BETWEEN 0 AND 100),
    withdrawn         BOOLEAN NOT NULL DEFAULT FALSE,
    notes             TEXT NOT NULL DEFAULT '' CHECK (length(notes) <= 2000),
    published_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX os_releases_offerable ON os_releases (rollback_index DESC, published_at DESC)
    WHERE NOT withdrawn AND rollout_percent > 0;

-- 042's default privileges hand the runtime roles everything on new tables; a read-only
-- catalogue has to REVOKE to mean it (as 067 found).
REVOKE ALL ON os_releases FROM PUBLIC;
REVOKE ALL ON os_releases FROM ira_app, ira_worker;
GRANT SELECT ON os_releases TO ira_app;

COMMIT;
