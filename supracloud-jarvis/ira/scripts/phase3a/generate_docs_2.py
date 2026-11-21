import json, re
from collections import defaultdict
from pathlib import Path

ROOT = Path("/home/user/supracloud-ira")
EV = ROOT / "docs/cloud/evidence"
M = json.load(open(EV / "DATABASE_TENANCY_CLASSIFICATION.json"))["tables"]
S = json.load(open(ROOT / "artifacts/cloud/phase-3a/database-schema.json"))["tables"]
A = json.load(open(ROOT / "artifacts/cloud/phase-3a/application-access-map.json"))

SCOPED = {"TENANT_SCOPED", "WORKSPACE_SCOPED", "USER_PRIVATE",
          "APPEND_ONLY_AUDIT", "EPHEMERAL_OPERATIONAL"}

# ── UNIQUE CONSTRAINT REVIEW ─────────────────────────────────────────────────
L=[]; w=L.append
w("# Phase 3A — Unique Constraint Review")
w("")
w("Every unique constraint and unique index on a table that will become")
w("tenant-, workspace- or user-scoped. **A global unique constraint on a")
w("tenant-created resource means the first tenant to use a value locks out every")
w("other tenant** — `alice`, `default`, `personal` and `main` are taken once,")
w("globally, forever.")
w("")
w("Generated from the schema snapshot; PK and UNIQUE constraints plus unique indexes.")
w("")
w("---")
w("")

rows=[]
for t in sorted(S):
    cls = M[t]["classification"]
    uniques = [c for c in S[t]["constraints"] if c["kind"] in ("PRIMARY KEY","UNIQUE")]
    uidx = [i for i in S[t]["indexes"] if "UNIQUE INDEX" in i["definition"].upper()
            and not any(u["name"] == i["name"] for u in uniques)]
    for c in uniques + [{"name": i["name"], "kind": "UNIQUE INDEX",
                         "definition": i["definition"]} for i in uidx]:
        d = c["definition"]
        m = re.search(r"\(([^)]*)\)", d)
        cols = [x.strip().strip('"') for x in m.group(1).split(",")] if m else []
        surrogate = cols == ["id"] and c["kind"] == "PRIMARY KEY"
        already = "tenant_id" in cols or "workspace_id" in cols or "user_id" in cols
        rows.append((t, cls, c["name"], c["kind"], cols, surrogate, already))

need_change = [r for r in rows if not r[5] and not r[6] and r[1] in SCOPED]
w("## 1. Summary")
w("")
w("| | Count |")
w("|---|---|")
w(f"| Unique constraints / indexes examined | {len(rows)} |")
w(f"| Surrogate primary keys (`id` only) — no change | {sum(1 for r in rows if r[5])} |")
w(f"| Already scoped (include tenant/workspace/user) | {sum(1 for r in rows if r[6])} |")
w(f"| **Global uniqueness on a scoped table — must change** | **{len(need_change)}** |")
w("")
w("A surrogate `PRIMARY KEY (id)` on a UUID needs no change: it is unique by")
w("construction and carries no business meaning. The constraints that matter are")
w("the ones over a *name*, *slug*, *username* or *external identifier*.")
w("")
w("## 2. Constraints that must become scoped")
w("")
w("| Table | Classification | Constraint | Current columns | Becomes |")
w("|---|---|---|---|---|")
TARGET = {
    "WORKSPACE_SCOPED": "UNIQUE (tenant_id, workspace_id, …)",
    "TENANT_SCOPED": "UNIQUE (tenant_id, …)",
    "USER_PRIVATE": "UNIQUE (user_id, …)",
    "APPEND_ONLY_AUDIT": "UNIQUE (tenant_id, …)",
    "EPHEMERAL_OPERATIONAL": "UNIQUE (tenant_id, …)",
}
for t, cls, name, kind, cols, _s, _a in sorted(need_change):
    tgt = TARGET[cls].replace("…", ", ".join(cols))
    w(f"| `{t}` | `{cls}` | `{name}` ({kind}) | `{', '.join(cols)}` | `{tgt}` |")
w("")
w("## 3. The named cases")
w("")
w("| Table | Today | Target | Why it matters |")
w("|---|---|---|---|")
w("| `totp_secrets` | `PRIMARY KEY (username)` | `UNIQUE (user_id)` | **The finding that made this a rule.** Two tenants both holding `alice` collide on the second factor — an authentication bypass, not a name clash. The key becomes an immutable `user_id`, never the string. |")
w("| `delegated_users` | `UNIQUE (username)` | `UNIQUE (tenant_id, username)` during compatibility; retired with the table | Legacy. Owner decision: migrate into memberships and deprecate. |")
w("| `voice_profiles` | keyed on `owner_name` | `UNIQUE (user_id, version)` | Versioned enrolment: a re-enrolment is a new version, not an overwrite, so revocation has something to revoke. |")
w("| `projects` | `UNIQUE (slug)` | `UNIQUE (tenant_id, workspace_id, slug)` | Otherwise one tenant naming a project `main` blocks every other tenant. |")
w("| `strategy_calibration` | `PRIMARY KEY (domain)` | `PRIMARY KEY (tenant_id, domain)` | One tenant's outcomes currently move another tenant's calibration. |")
w("| `heartbeat_surfaced` | `UNIQUE (item_key)` | `UNIQUE (tenant_id, user_id, item_key)` | Dedupe keys are per-person; global uniqueness suppresses another user's notification. |")
w("| `monitor_state` | `PRIMARY KEY (key)` | **unchanged** | `PLATFORM_GLOBAL` — genuinely one row per platform key. |")
w("| Future `external_identities` | — | `UNIQUE (issuer, subject)` | Owner decision: external identity is unique by issuer+subject, never by email. |")
w("| Future local-auth email | — | `UNIQUE (normalised_email)` where local password auth is enabled | A verified local-auth email is globally unique; email remains a mutable attribute, never the identity. |")
w("")
w("## 4. Rule")
w("")
w("> A unique constraint over a **user-chosen value** on a scoped table must")
w("> include that table's scope column. A unique constraint over a **surrogate")
w("> UUID** must not.")
w("")
w("The failure mode is quiet: the second tenant to try the name gets a")
w("constraint-violation error that looks like a bug in their own input, and")
w("nothing in the message says another tenant took it.")
(EV / "PHASE_3A_UNIQUE_CONSTRAINT_REVIEW.md").write_text("\n".join(L) + "\n")

# ── APPLICATION ACCESS MAP ───────────────────────────────────────────────────
L=[]; w=L.append
w("# Phase 3A — Application Access Map")
w("")
w("Where every table is actually read, written and deleted. **854 files scanned**")
w("across backend, workers, scripts, tests, frontend and SQL.")
w("")
w("Generated: `artifacts/cloud/phase-3a/application-access-map.json`")
w("")
w("> Per the phase brief: no table is called unused until every call site has")
w("> been searched. Six tables have no non-test code reference; §3 says what they")
w("> are and why that is not the same as dead.")
w("")
w("---")
w("")
w("## 1. Method")
w("")
w("Two passes per file. A SQL pass matches `FROM`/`JOIN`/`INSERT INTO`/")
w("`UPDATE … SET`/`DELETE FROM` against the 77 known table names. A mention pass")
w("matches the bare table name, which catches ORM and helper usage that no SQL")
w("regex would see. A table counts as code-referenced only on a **non-test,")
w("non-SQL** file.")
w("")
w("## 2. Access by table")
w("")
w("`R`/`W`/`D` = files with read / write / delete statements. `Worker` = files")
w("under `worker/`, `tasks/`, or matching scheduler/heartbeat/reflection.")
w("")
w("| Table | Classification | R | W | D | Worker | Non-test files |")
w("|---|---|---|---|---|---|---|")
for t in sorted(A):
    a, cls = A[t], M[t]["classification"]
    w(f"| `{t}` | `{cls}` | {len(a['read_call_sites'])} | "
      f"{len(set(a['insert_call_sites'] + a['update_call_sites']))} | "
      f"{len(a['delete_call_sites'])} | {len(a['background_worker_call_sites'])} | "
      f"{a['non_test_code_files']} |")
w("")
w("## 3. Tables with no non-test code reference")
w("")
w("| Table | Classification | Reading |")
w("|---|---|---|")
notes = {
 "approval_events": "Approval flow schema exists; no route writes it yet.",
 "approval_requests": "Same — the governance approval surface is not wired.",
 "avatar_catalogue": "Seeded by SQL; the avatar picker reads it through a view/route not yet built.",
 "export_requests": "Data export is `PLANNED`; the table shipped ahead of the feature.",
 "personality_presets": "Seeded by SQL; the preset selector is not wired.",
 "voice_catalogue": "Seeded by SQL; the voice picker is not wired.",
}
for t in sorted(A):
    if not A[t]["referenced_in_code"]:
        w(f"| `{t}` | `{M[t]['classification']}` | {notes.get(t,'—')} |")
w("")
w("**These are not dead tables.** Each is defined and seeded by a migration and")
w("covered by tests. They are schema that shipped ahead of its feature, which is")
w("the opposite problem from dead code: the migration must still scope them")
w("correctly, because the feature will arrive *after* Phase 3B, not before.")
w("")
w("## 4. Background-worker access")
w("")
w("Tables touched from `worker/`, `tasks/` or a scheduler path. Each needs")
w("explicit tenant context — `core/identity/background.py` sets the GUCs, and")
w("Phase 3C must prove it for every one of these.")
w("")
w("| Table | Classification | Worker files |")
w("|---|---|---|")
any_worker = False
for t in sorted(A):
    if A[t]["background_worker_call_sites"]:
        any_worker = True
        w(f"| `{t}` | `{M[t]['classification']}` | {len(A[t]['background_worker_call_sites'])} |")
if not any_worker:
    w("| — | — | none detected by path heuristic |")
w("")
w("## 5. Busiest tables")
w("")
w("| Table | Non-test files | Note |")
w("|---|---|---|")
busy = sorted(A.items(), key=lambda kv: -kv[1]["non_test_code_files"])[:10]
for t, a in busy:
    note = ""
    if t == "memory_embeddings": note = "**the busiest — backfilling it twice is the cost D-12 avoids**"
    elif t == "security_events": note = "needs a nullable tenant (platform-scope events)"
    w(f"| `{t}` | {a['non_test_code_files']} | {note} |")
(EV / "PHASE_3A_APPLICATION_ACCESS_MAP.md").write_text("\n".join(L) + "\n")

print("unique constraints needing change:", len(need_change))
print("wrote access map + unique constraint review")
