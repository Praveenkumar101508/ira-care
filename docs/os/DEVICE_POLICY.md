# Device policy: deployment-administrator instructions to a watch

**State: built and tested in software; nothing has run on a device.** The server side is tested
against a real database, the watch side on the JVM. Making the device-policy app the device owner
is provisioning (E4.f) and needs an image; until then no component on a watch calls this code.

The watch keeps developer options, factory reset and safe-mode boot locked
(`os/device/ira/ira_watch_cf/SETTINGS_POLICY.md`). For maintenance a **deployment administrator**
(D-082) can send one watch one signed instruction. This page is the whole workflow.

## Who is a deployment administrator

An ordinary signed-in account whose user id holds a live grant in `deployment_administrators`
(migration 121). Nothing else confers it: not a care workspace role (care manager, coordinator,
carer, viewer), not a tenant role, not the deployment-owner flag.

The operator grants and revokes it with the migrator's credentials, as OS releases are published:

    cd supracloud-jarvis/ira
    python -m scripts.deployment_admin grant  --dsn MIGRATOR_DSN --user-id UUID --label "fleet on-call"
    python -m scripts.deployment_admin revoke --dsn MIGRATOR_DSN --user-id UUID
    python -m scripts.deployment_admin list   --dsn MIGRATOR_DSN

The runtime role can write no grant (row-level security admits writes only from
`ira_schema_owner`), and can read a live grant only on a connection bound to that account.

## What an administrator can send

| Action | On the watch | Bound |
| --- | --- | --- |
| `MAINTENANCE_WINDOW` | lifts `DISALLOW_DEBUGGING_FEATURES` only; factory reset and safe boot stay locked | closes by itself at expiry (1 to 15 minutes after issue) and at every start of the app or the watch |
| `RESTORE_RESTRICTIONS` | puts every restriction back now | must be used within its expiry |
| `FACTORY_RESET` | `DevicePolicyManager.wipeData` | must be used within its expiry |

There is no other action and no field for a command, an argument or a restriction name. The API
(422), the table (CHECK constraint) and the watch ("unknown action") each refuse anything else, even
a payload the server's key signed.

## The flow

1. **Issue.** `POST /api/v1/care/deployment-admin/device-instructions` with the watch's
   `tenant_id`, `workspace_id` and `device_id` (from the care organisation's support request; the
   device id is in the workspace's device list, `GET /care/people/{id}/devices`), the `action`,
   `minutes` (1 to 15) and a `reason` (10 to 300 characters). The work runs on a connection bound to that workspace, so a
   device id paired with another tenant's or workspace's ids is not found (404). Only a paired
   watch can be named. The server builds the payload
   `ira-device-policy-v1|device|ACTION|issued_ms|expires_ms|nonce` (a fresh 128-bit nonce), signs it
   with ECDSA P-256 (`IRA_DEVICE_POLICY_KEY`; none configured means 503) and stores it.
2. **Collect.** The watch calls `POST /api/v1/care/devices/me/admin-instructions` with its device
   token and gets the unexpired instructions for itself it has not reported on, and
   `revoked_nonces`: unexpired instructions it must no longer honour.
3. **Verify and apply on the watch** (`DevicePolicy.handleSigned`). In order: the signature over the
   exact bytes received (so nothing unsigned is even parsed), the payload's shape and version, the
   action is one of the three, the device id is this watch, not issued more than a minute in the
   future, not expired, a window of at most 15 minutes, not revoked, not used before. Used and
   revoked nonces are kept in a file (`FileNonceStore`) so a reboot forgets neither. A refusal
   changes nothing.
4. **Report.** `POST /api/v1/care/devices/me/admin-instructions/{id}/outcome` with `applied` or
   `refused`, then `restored` when a window closes. Only the watch the instruction was for can
   report, and only in that order.
5. **Revoke.** `POST /api/v1/care/deployment-admin/device-instructions/{id}/revoke` with the tenant,
   workspace and a reason. A revoked instruction is never handed over again and its nonce goes on
   the watch's revocation list; if it opened the current window, `DevicePolicy.revoke` closes it at
   once. Revocation cannot be undone (trigger). Revoking the administrator's grant does the same for
   everything they issued that has not expired.
6. **Audit.** `GET /api/v1/care/deployment-admin/device-instructions?tenant_id=&workspace_id=&device_id=`
   lists every instruction for the watch with its trail: issued and revoked (by which administrator,
   with the reason), delivered, applied, refused and restored (by the watch), each with its time.

## Where it is proved

| What | Test |
| --- | --- |
| Ordinary roles, the tenant owner and the deployment-owner flag are refused; nothing is written | `supracloud-jarvis/ira/tests/tenancy/test_device_admin_db.py` |
| Another tenant's or workspace's watch cannot be named, reached or reported on; a second watch never sees another's instruction | same |
| Unknown actions and windows over 15 minutes refused by the API and by the table | same |
| Revocation, a revoked administrator, an expired instruction, no key, an unpaired watch | same |
| The runtime cannot grant, even after `GRANT ALL`; it cannot list live administrators | same |
| Forged, altered, foreign, expired, early, over-long, replayed, revoked and unknown-action instructions refused on the watch; the window closes at expiry, early and at a start | `watch/app/src/test/java/care/ira/watch/devicepolicy/DevicePolicyTest.kt` |
| The watch accepts exactly what the server signs | the same test, with `watch/app/src/test/resources/device_policy_instruction.json` from `scripts/os_device_policy_fixture.py` |

## Not built

- The device-owner registration (E4.f), and on a device the timer and boot receiver that call
  `relockIfDue` and `applyBaseline`, and the job that collects instructions.
- A dashboard screen for deployment administrators: the routes exist, the screen does not.
- Who in the operating organisation holds the grant is an owner decision.
