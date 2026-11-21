"""Phase 3A — build the authoritative classification manifest.

Every one of the 77 live tables gets exactly one classification, a written
justification, a target schema where migration is required, RLS/encryption/
deletion specifications, and cache/object-storage boundaries.

Owner decisions applied (CLOUD-ADR-0006, CLOUD-ADR-0008):
  users            IDENTITY_GLOBAL, immutable UUID, no tenant_id
  delegated_users  TENANT_SCOPED during compatibility; replaced by memberships
  projects         WORKSPACE_SCOPED
  agents           WORKSPACE_SCOPED (user-created); templates become REFERENCE_DATA
  owner_profile    decomposed into user_profiles / organisation_profiles /
                   tenant_settings / memberships
  totp_secrets     USER_PRIVATE, keyed by user_id, encrypted
  voice_profiles   USER_PRIVATE, keyed by user_id, encrypted
  biometric_audit  APPEND_ONLY_AUDIT with user_id + nullable tenant/workspace
"""
import json
from pathlib import Path

ROOT = Path("/home/user/supracloud-ira")
SNAP = json.load(open(ROOT / "artifacts/cloud/phase-3a/database-schema.json"))
ACCESS = json.load(open(ROOT / "artifacts/cloud/phase-3a/application-access-map.json"))
TABLES = sorted(SNAP["tables"])

def cols(t):
    return [c["name"] for c in SNAP["tables"][t]["columns"]]

def has(t, c):
    return c in cols(t)

def forced(t):
    return SNAP["tables"][t]["rls"].get("forced", False)

# ── Classification assignments ───────────────────────────────────────────────

REFERENCE_DATA = {
    "avatar_catalogue": "Avatar models and licence references. Identical for every tenant; app must read only.",
    "voice_catalogue": "TTS voices and privacy classification. Identical for every tenant; app must read only.",
    "personality_presets": "Warmth/formality/verbosity presets. Platform-authored catalogue.",
    "model_providers": "Provider locality, retention and cost facts. Platform-authored.",
    "tool_policies": "Domain/action risk defaults. Platform-authored policy floor; a tenant must not lower it.",
    "realtime_capability_profiles": "Capability rungs for realtime sessions. Platform-authored.",
    "connector_scopes": "Human-readable connector scope descriptions. Platform-authored.",
}

PLATFORM_GLOBAL = {
    "tenants": "The tenant registry itself. Scoping it to a tenant is circular.",
    "monitor_state": "Key/value operational state for the platform's own monitors. No customer data.",
}

IDENTITY_GLOBAL = {
    "users": "One person, many tenants. Immutable UUID identity; tenant access only via memberships.",
}

# Tables needing tenant_id added (currently have none)
NEEDS_TENANT = {
    # priority 1 — authentication and biometric
    "totp_secrets":    ("USER_PRIVATE", "MFA secrets. Currently keyed on a username STRING with no tenant — two tenants both holding 'alice' collide on the second factor. That is an authentication bypass, not a privacy nuisance."),
    "voice_profiles":  ("USER_PRIVATE", "Voiceprint templates — biometric special category. Currently keyed on owner_name string, stored unencrypted."),
    "biometric_audit": ("APPEND_ONLY_AUDIT", "Audit trail of voiceprint matching. Correlates to a person via session_id. Needs user_id plus nullable tenant/workspace: a platform-level auth event may have no active tenant."),
    # priority 2 — user content
    "calendar_events":      ("WORKSPACE_SCOPED", "Calendar entries with attendees (personal data). No owner column today."),
    "reminders":            ("WORKSPACE_SCOPED", "Reminders, FK to tasks (already tenant-scoped)."),
    "notifications":        ("USER_PRIVATE", "Delivered to a person, not to a tenant."),
    "briefings":            ("USER_PRIVATE", "Generated from that user's own data."),
    "decisions":            ("WORKSPACE_SCOPED", "Decision journal — collaborative content."),
    "entities":             ("WORKSPACE_SCOPED", "Life-graph nodes; carries an embedding."),
    "edges":                ("WORKSPACE_SCOPED", "Life-graph edges. A cross-tenant edge must be impossible by FK, not filtered on read."),
    "projects":             ("WORKSPACE_SCOPED", "Owner decision CLOUD-ADR-0006."),
    "agents":               ("WORKSPACE_SCOPED", "User-created agents hold code and docker_config. Owner decision CLOUD-ADR-0006; platform templates split out later as REFERENCE_DATA."),
    "business_events":      ("TENANT_SCOPED", "Business events belong to the tenant, not a workspace."),
    "strategy_predictions": ("TENANT_SCOPED", "Prediction log; tenant-level analytics."),
    "strategy_calibration": ("TENANT_SCOPED", "Per-domain calibration. Global today, so one tenant's outcomes shift another's."),
    "model_performance":    ("TENANT_SCOPED", "Latency/token stats. Global today, so per-tenant usage is inferable from aggregates."),
    "heartbeat_surfaced":   ("USER_PRIVATE", "Dedupe keys for what has been surfaced to a person."),
    "owner_profile":        ("USER_PRIVATE", "Single-owner artefact. Decomposes into user_profiles / organisation_profiles / tenant_settings / memberships (CLOUD-ADR-0006); classified by present contents, which are personal preferences."),
    "command_runs":         ("APPEND_ONLY_AUDIT", "Holds raw_text — user input. actor is a string today."),
    "access_audit_log":     ("APPEND_ONLY_AUDIT", "actor is a string today; needs tenant_id + user_id."),
    "security_events":      ("APPEND_ONLY_AUDIT", "17 code references. Some events are genuinely platform-level (failed login for an unknown user), so tenant_id must be NULLABLE with a platform-scope policy."),
    "delegated_users":      ("TENANT_SCOPED", "Legacy compatibility only. Owner decision: migrate into memberships, then deprecate. Must not be extended as the permanent authorisation model."),
}

# Among the 45 that already have tenant_id: which also need workspace_id
NEEDS_WORKSPACE = {
    "conversations", "messages", "memory_embeddings", "files", "tasks",
    "commitments", "lifeos_events", "people", "companions",
    "companion_versions", "companion_preferences", "agent_runs", "tool_calls",
}

# Among the 45: which are audit (append-only)
AUDIT = {
    "action_receipts", "admin_access_grants", "admin_access_log", "agent_runs",
    "approval_events", "model_usage", "permission_decisions", "privacy_events",
    "realtime_events", "tool_calls", "trust_ledger",
}

# Among the 45: which are user-private
USER_PRIVATE_EXISTING = {
    "companion_preferences", "connector_accounts", "user_devices", "user_sessions",
}

EPHEMERAL = {"connector_oauth_states", "realtime_sessions"}

# Billing / commercial — tenant-scoped only, never workspace
BILLING = {"usage_quotas", "model_usage", "investors", "client_agents"}

ENCRYPTION_REQUIRED = {
    "totp_secrets": "MFA secret — envelope-encrypted with the tenant DEK; never returned by an ordinary API; never logged.",
    "voice_profiles": "Biometric template — envelope-encrypted; hard-deleted on erasure, never redacted.",
    "connector_accounts": "OAuth access/refresh tokens — already handled by core/connectors/keyring.py (migration 030).",
    "tenant_data_keys": "Wrapped data-encryption keys. Shred-only trigger. NEVER exported in any form.",
}

def classify(t):
    if t in REFERENCE_DATA:  return "REFERENCE_DATA", REFERENCE_DATA[t]
    if t in PLATFORM_GLOBAL: return "PLATFORM_GLOBAL", PLATFORM_GLOBAL[t]
    if t in IDENTITY_GLOBAL: return "IDENTITY_GLOBAL", IDENTITY_GLOBAL[t]
    if t in NEEDS_TENANT:    return NEEDS_TENANT[t]
    if t in EPHEMERAL:       return "EPHEMERAL_OPERATIONAL", "Short-lived operational state; expires on its own."
    if t in AUDIT:           return "APPEND_ONLY_AUDIT", "Append-only evidence; redacted rather than deleted on erasure (ADR-0004, ADR-0008)."
    if t in USER_PRIVATE_EXISTING:
        return "USER_PRIVATE", "Belongs to one user; a tenant admin must not read it."
    if t in NEEDS_WORKSPACE: return "WORKSPACE_SCOPED", "Collaborative content; separable within a tenant."
    if t in BILLING:         return "TENANT_SCOPED", "Commercial record. Tenant-scoped only — a workspace-scoped invoice would make billing depend on which workspace produced it."
    return "TENANT_SCOPED", "Tenant-owned record with no workspace separation requirement."

SENSITIVE = {
    "authentication_data": {"totp_secrets", "delegated_users", "user_sessions",
                            "user_devices", "connector_accounts", "connector_oauth_states", "users"},
    "biometric_data": {"voice_profiles", "biometric_audit"},
    "financial_data": {"usage_quotas", "model_usage", "investors"},
}
NON_CUSTOMER = set(REFERENCE_DATA) | set(PLATFORM_GLOBAL)

manifest = {"schema_version": 1, "phase": "3A", "table_count": len(TABLES), "tables": {}}

for t in TABLES:
    cls, why = classify(t)
    existing = cols(t)
    needs_tenant = t in NEEDS_TENANT and cls not in ("REFERENCE_DATA", "PLATFORM_GLOBAL", "IDENTITY_GLOBAL")
    needs_ws = (t in NEEDS_WORKSPACE) or (cls == "WORKSPACE_SCOPED")
    needs_user_conv = t in {"totp_secrets", "voice_profiles", "delegated_users",
                            "access_audit_log", "command_runs", "owner_profile"}

    target = {}
    if cls in ("TENANT_SCOPED", "WORKSPACE_SCOPED", "USER_PRIVATE", "APPEND_ONLY_AUDIT",
               "EPHEMERAL_OPERATIONAL") and "tenant_id" not in existing:
        nullable = (t == "security_events" or t == "biometric_audit")
        target["tenant_id"] = {
            "type": "UUID", "nullable_during_transition": True,
            "final_nullable": nullable,
            "fk": "tenants(id)", "on_delete": "RESTRICT",
            "backfill_source": "PRIVATE_TENANT_ID (deterministic UUIDv5, core/identity/principal.py)",
            "index": "(tenant_id, created_at DESC)", "used_by_rls": True,
        }
    if needs_ws and "workspace_id" not in existing:
        target["workspace_id"] = {
            "type": "UUID", "nullable_during_transition": True,
            "final_nullable": cls != "WORKSPACE_SCOPED",
            "fk": "workspaces(id)", "on_delete": "RESTRICT",
            "backfill_source": "PRIVATE_WORKSPACE_ID (default workspace per tenant)",
            "index": "(tenant_id, workspace_id, created_at DESC)", "used_by_rls": cls == "WORKSPACE_SCOPED",
        }
    if needs_user_conv and "user_id" not in existing:
        target["user_id"] = {
            "type": "UUID", "nullable_during_transition": True,
            "final_nullable": cls == "APPEND_ONLY_AUDIT",
            "fk": "users(id)", "on_delete": "RESTRICT",
            "backfill_source": "username/owner_name/actor string resolved via the identity mapping table",
            "index": "(user_id)", "used_by_rls": cls == "USER_PRIVATE",
            "replaces": "the existing username/owner_name/actor string key",
        }
    if cls in ("WORKSPACE_SCOPED", "TENANT_SCOPED") and "created_by" not in existing and needs_tenant:
        target["created_by"] = {
            "type": "UUID", "nullable_during_transition": True, "final_nullable": True,
            "fk": "users(id)", "on_delete": "SET NULL",
            "backfill_source": "PRIVATE_USER_ID where the row predates multi-user",
            "index": None, "used_by_rls": False,
        }

    if cls in ("PLATFORM_GLOBAL", "REFERENCE_DATA", "IDENTITY_GLOBAL"):
        rls = {"required": False, "reason": "global by classification; protected by role grants, not RLS"}
    else:
        pred = "tenant_id = current_setting('app.tenant_id', true)::uuid"
        if cls == "WORKSPACE_SCOPED":
            pred += " AND workspace_id = current_setting('app.workspace_id', true)::uuid"
        if cls == "USER_PRIVATE":
            pred += " AND user_id = current_setting('app.user_id', true)::uuid"
        rls = {
            "required": True, "force": True,
            "select": pred, "insert": pred, "update": pred,
            "delete": pred if cls != "APPEND_ONLY_AUDIT" else "DENIED (append-only trigger)",
            "session_context": ["app.tenant_id"]
                + (["app.workspace_id"] if cls == "WORKSPACE_SCOPED" else [])
                + (["app.user_id"] if cls == "USER_PRIVATE" else []),
            "background_worker": "sets the same GUCs via core/identity/background.py",
            "support_access": "via admin_access_grants only; time-limited, audited, never implicit",
            "application_role_bypassrls": False,
            "already_satisfied": forced(t) and "tenant_id" in existing and cls != "WORKSPACE_SCOPED",
        }

    if cls == "APPEND_ONLY_AUDIT":
        deletion = {"user_deletion": "redact content, keep the row (ADR-0004/0008)",
                    "tenant_deletion": "redact; retain for the audit retention period",
                    "workspace_deletion": "unaffected", "membership_removal": "unaffected",
                    "legal_hold": "blocks redaction", "hard_delete": "only after retention expiry"}
        retention = "audit retention period (owner to set); never cascade-deleted with the tenant"
    elif cls == "USER_PRIVATE":
        deletion = {"user_deletion": "HARD DELETE, plus crypto-shred of the per-user material",
                    "tenant_deletion": "delete only rows whose user has no other membership",
                    "workspace_deletion": "unaffected", "membership_removal": "revoke access; do not delete",
                    "legal_hold": "n/a", "hard_delete": "immediate on user deletion"}
        retention = "life of the user account"
    elif cls in ("REFERENCE_DATA", "PLATFORM_GLOBAL"):
        deletion = {"user_deletion": "unaffected", "tenant_deletion": "unaffected",
                    "workspace_deletion": "unaffected", "membership_removal": "unaffected",
                    "legal_hold": "n/a", "hard_delete": "platform operation only"}
        retention = "indefinite (platform data)"
    elif cls == "IDENTITY_GLOBAL":
        deletion = {"user_deletion": "HARD DELETE after all memberships resolved",
                    "tenant_deletion": "MUST NOT delete a user who belongs to another tenant",
                    "workspace_deletion": "unaffected", "membership_removal": "unaffected",
                    "legal_hold": "may block deletion", "hard_delete": "on verified user deletion"}
        retention = "life of the account"
    elif cls == "EPHEMERAL_OPERATIONAL":
        deletion = {"user_deletion": "delete", "tenant_deletion": "delete",
                    "workspace_deletion": "delete", "membership_removal": "delete",
                    "legal_hold": "n/a", "hard_delete": "on expiry"}
        retention = "short TTL; expires without intervention"
    else:
        deletion = {"user_deletion": "reassign to the tenant or delete per retention policy",
                    "tenant_deletion": "HARD DELETE + crypto-shred the tenant DEK",
                    "workspace_deletion": "delete workspace-scoped rows" if cls == "WORKSPACE_SCOPED" else "unaffected",
                    "membership_removal": "revoke access; content stays with the tenant",
                    "legal_hold": "blocks deletion", "hard_delete": "on tenant deletion"}
        retention = "tenant retention_settings"

    if cls == "WORKSPACE_SCOPED":
        cache = "ira:{env}:tenant:{tenant_id}:workspace:{workspace_id}:" + t + ":{key}"
        obj = "tenant/{tenant_id}/workspace/{workspace_id}/" + t + "/"
    elif cls == "USER_PRIVATE":
        cache = "ira:{env}:user:{user_id}:" + t + ":{key}"
        obj = "user/{user_id}/private/" + t + "/"
    elif cls in ("REFERENCE_DATA", "PLATFORM_GLOBAL", "IDENTITY_GLOBAL"):
        cache = "ira:{env}:platform:" + t + ":{key}"
        obj = "platform/" + t + "/"
    else:
        cache = "ira:{env}:tenant:{tenant_id}:" + t + ":{key}"
        obj = "tenant/{tenant_id}/" + t + "/"

    a = ACCESS[t]
    manifest["tables"][t] = {
        "classification": cls,
        "justification": why,
        "current": {
            "has_tenant_id": "tenant_id" in existing,
            "has_workspace_id": "workspace_id" in existing,
            "has_user_id": "user_id" in existing,
            "rls_enabled": SNAP["tables"][t]["rls"].get("enabled", False),
            "rls_forced": forced(t),
            "policy_count": len(SNAP["tables"][t]["policies"]),
            "column_count": len(existing),
        },
        "data_kind": {
            "customer_data": t not in NON_CUSTOMER,
            "authentication_data": t in SENSITIVE["authentication_data"],
            "biometric_data": t in SENSITIVE["biometric_data"],
            "financial_data": t in SENSITIVE["financial_data"],
            "audit_data": cls == "APPEND_ONLY_AUDIT",
        },
        "access": {
            "read_call_sites": a["read_call_sites"],
            "write_call_sites": sorted(set(a["insert_call_sites"] + a["update_call_sites"])),
            "delete_call_sites": a["delete_call_sites"],
            "background_worker_call_sites": a["background_worker_call_sites"],
            "non_test_code_files": a["non_test_code_files"],
            "referenced_in_code": a["referenced_in_code"],
        },
        "migration": {
            "requires_migration": bool(target),
            "requires_tenant_id": "tenant_id" in target,
            "requires_workspace_id": "workspace_id" in target,
            "requires_user_ownership_conversion": "user_id" in target,
            "target_columns": target,
        },
        "encryption": {
            "required": t in ENCRYPTION_REQUIRED,
            "specification": ENCRYPTION_REQUIRED.get(t, "not required — no secret or biometric material"),
        },
        "rls": rls,
        "deletion": deletion,
        "retention": retention,
        "cache_key_pattern": cache,
        "object_storage_prefix": obj,
    }

out = ROOT / "docs/cloud/evidence/DATABASE_TENANCY_CLASSIFICATION.json"
json.dump(manifest, open(out, "w"), indent=1, sort_keys=True)

from collections import Counter
c = Counter(v["classification"] for v in manifest["tables"].values())
print("TOTAL:", len(manifest["tables"]))
for k, v in sorted(c.items()): print(f"  {k:24} {v}")
print("UNRESOLVED:", c.get("UNRESOLVED", 0))
print()
m = manifest["tables"]
print("requires_migration:  ", sum(1 for v in m.values() if v["migration"]["requires_migration"]))
print("requires_tenant_id:  ", sum(1 for v in m.values() if v["migration"]["requires_tenant_id"]))
print("requires_workspace_id:", sum(1 for v in m.values() if v["migration"]["requires_workspace_id"]))
print("requires_user_conv:  ", sum(1 for v in m.values() if v["migration"]["requires_user_ownership_conversion"]))
print("requires_encryption: ", sum(1 for v in m.values() if v["encryption"]["required"]))
