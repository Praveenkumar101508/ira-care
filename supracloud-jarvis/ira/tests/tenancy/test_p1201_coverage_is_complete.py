"""37 of 37, written down, and a ratchet so it cannot quietly become 37 of 39.

`tests/tenancy/test_workspace_binding_ratchet.py` already asserts that no
module reaches a workspace table unbound. That is a grep, and P12-01 exists
because a grep answers a question about vocabulary rather than about behaviour:
`worker/heartbeat.py` named `workspace_connection` and had never imported it,
so the ratchet was green while the signal had never run.

This file is the other half. It does not ask whether a module names the helper.
It asks whether every module that touches a `workspace_id`-carrying table has a
RECORDED CLASSIFICATION backed by an execution, and it fails when a new one
appears without one.

Absence from a findings list is not evidence. That sentence is the whole batch,
and `MANIFEST` is what makes it enforceable.
"""
from __future__ import annotations

import pathlib
import subprocess

IRA_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: The twenty tables the live schema gives a `workspace_id`. Duplicated from
#: the schema deliberately: the audit script reads it from
#: `information_schema`, and this test has to be runnable in a job that has no
#: database. `test_p1201_workspace_hazards.py` is where the two are reconciled
#: against `pg_policies`.
WORKSPACE_TABLES = (
    "agents", "biometric_audit", "calendar_events", "commitments",
    "companion_versions", "companions", "conversations", "decisions", "edges",
    "entities", "files", "lifeos_events", "memory_embeddings", "messages",
    "notifications", "people", "projects", "reminders", "tasks",
    "workspace_memberships",
)

#: Every runtime file that touches one of those tables, and what executing it
#: showed. One of the six buckets D1 defines, and nothing may be absent.
#:
#: `WORKSPACE_BOUND`        bound at query time, observed
#: `TENANT_WIDE_BY_DESIGN`  intentionally crosses workspaces
#: `ROOT_LOOKUP`            must precede workspace binding
#: `NOT_APPLICABLE`         touches no table whose predicate reads the setting
#: `EXCLUDED_NOT_RUNTIME`   not a runtime surface; no user reaches it
#:
#: `INCORRECTLY_UNBOUND` and `UNREACHABLE` have no members. Two files were
#: UNREACHABLE when this batch started — `worker/heartbeat.py` and
#: `worker/self_healing.py` — and both were repaired rather than recorded,
#: because the repair was a missing import and a stale table name.
MANIFEST: dict[str, str] = {
    # ── Product surfaces, observed bound ─────────────────────────────────────
    "agents/creator.py": "WORKSPACE_BOUND",
    "api/routes/memory.py": "WORKSPACE_BOUND",
    "api/routes/notifications.py": "WORKSPACE_BOUND",
    "core/companion/service.py": "WORKSPACE_BOUND",
    "core/identity/provisioning.py": "WORKSPACE_BOUND",
    "core/privacy/store.py": "WORKSPACE_BOUND",
    # F1 commit gate. Reads `workspace_memberships` like the roots below, but
    # AFTER binding, inside the connection it guards. Observed: 2,023 bound,
    # 0 unbound under scripts/audit/workspace_binding_audit.py, and pinned by
    # tests/callers/test_evidence_callers.py, which fails if that read is ever
    # unbound (checked by unbinding it once: the test failed).
    "memory/decision_journal.py": "WORKSPACE_BOUND",
    "memory/life_graph.py": "WORKSPACE_BOUND",
    "memory/relationships.py": "WORKSPACE_BOUND",
    "memory/self_knowledge.py": "WORKSPACE_BOUND",
    "memory/store.py": "WORKSPACE_BOUND",
    "memory/technical_library.py": "WORKSPACE_BOUND",
    "memory/temporal.py": "WORKSPACE_BOUND",
    "tasks/calendar.py": "WORKSPACE_BOUND",
    "tasks/manager.py": "WORKSPACE_BOUND",
    "worker/briefing.py": "WORKSPACE_BOUND",
    "worker/heartbeat.py": "WORKSPACE_BOUND",
    "worker/heartbeat_signals.py": "WORKSPACE_BOUND",
    "worker/reflection.py": "WORKSPACE_BOUND",
    "worker/reminders.py": "WORKSPACE_BOUND",
    "worker/self_healing.py": "WORKSPACE_BOUND",

    # ── Deliberately crossing workspaces ─────────────────────────────────────
    #
    # Erasure and retention both sweep a whole tenant. Neither binds one
    # workspace and neither should: they run N workspace-bound passes, one per
    # workspace the tenant has, and `erase_postgres` reports FAILURE rather
    # than success if it cannot enumerate them. That is what keeps "erase
    # everything" from becoming "erase whatever the current workspace can see".
    "core/privacy/erasure.py": "TENANT_WIDE_BY_DESIGN",
    "core/privacy/retention.py": "TENANT_WIDE_BY_DESIGN",
    # Writes `notifications` with a NULL workspace, which `051`'s policy reads
    # as tenant-wide. Permitted by the predicate, not a missing binding.
    "worker/notifier.py": "TENANT_WIDE_BY_DESIGN",

    # ── Roots: read BEFORE a workspace can be chosen ─────────────────────────
    #
    # All four touch `workspace_memberships` and nothing else in the set. That
    # table has the column and NO workspace predicate, and must not gain one:
    # it is how a principal finds out which workspace it may bind, so a policy
    # keyed on `app.workspace_id` would make "which workspaces am I in?"
    # answer "none" for everybody.
    "api/routes/workspaces.py": "ROOT_LOOKUP",
    "core/identity/background.py": "ROOT_LOOKUP",
    "core/identity/invitations.py": "ROOT_LOOKUP",
    "core/identity/tenant_provisioning.py": "ROOT_LOOKUP",
    "core/identity/workspace.py": "ROOT_LOOKUP",

    # ── Touches a carrying table, but not a predicated one ───────────────────
    #
    # Three bare `acquire()` calls and no `workspace_connection` anywhere — the
    # worst-looking profile in the batch, and correct. The only carrying table
    # it reaches is `biometric_audit`, which has no workspace predicate, so
    # nothing it reads is hidden. Establishing that took `pg_policies`; no
    # reading of the file could have done it.
    "voice/biometrics.py": "NOT_APPLICABLE",
}

#: Observed executing, and deliberately NOT one of the 37.
#:
#: `scripts/phase3b/workspace_null_audit.py` runs 1,640 unbound queries across
#: seventeen predicated tables, which is the largest unbound figure the audit
#: reports and is entirely correct: it is a one-off Phase 3B maintenance script
#: whose whole purpose is to count rows across every workspace. It does not
#: appear in the static sweep either — its table names are interpolated at
#: runtime, so the FROM/INTO pattern does not match it — which is why the audit
#: observes thirty-eight files and thirty-seven of them are the population.
#:
#: Recorded here so that the difference between "38 observed" and "37 of 37"
#: is a decision somebody made rather than an arithmetic discrepancy a reader
#: has to resolve on their own.
OBSERVED_NOT_RUNTIME: dict[str, str] = {
    "scripts/phase3b/workspace_null_audit.py": "EXCLUDED_NOT_RUNTIME",
}

VALID = {
    "WORKSPACE_BOUND", "TENANT_WIDE_BY_DESIGN", "ROOT_LOOKUP",
    "INCORRECTLY_UNBOUND", "UNREACHABLE", "NOT_APPLICABLE",
    "EXCLUDED_NOT_RUNTIME",
}


def _static_sweep() -> set[str]:
    """Every runtime file mentioning one of the tables in a FROM/INTO/UPDATE/JOIN.

    The same sweep `scripts/audit/workspace_binding_audit.py` runs, so the two
    cannot drift into disagreeing about what the population is.
    """
    found: set[str] = set()
    for table in WORKSPACE_TABLES:
        out = subprocess.run(
            ["grep", "-rlE", rf"(FROM|INTO|UPDATE|JOIN)[[:space:]]+{table}\b",
             "--include=*.py", "."],
            capture_output=True, text=True, cwd=IRA_ROOT).stdout.split()
        for path in out:
            if "/tests/" in path or "__pycache__" in path:
                continue
            found.add(path.lstrip("./"))
    return found


def test_every_classification_is_one_of_the_six():
    unknown = {f: c for f, c in {**MANIFEST, **OBSERVED_NOT_RUNTIME}.items()
               if c not in VALID}
    assert not unknown, f"not a D1 classification: {unknown}"


def test_no_file_touching_a_workspace_table_is_unclassified():
    """The ratchet. A new caller must be classified, not merely absent.

    This is the check that would have caught the twenty-five: they were not
    listed as unbound, because they had never run, and nothing anywhere
    required them to be accounted for.
    """
    static = _static_sweep()
    unclassified = sorted(static - set(MANIFEST))

    assert not unclassified, (
        f"{len(unclassified)} file(s) touch a workspace-carrying table and "
        f"have no P12-01 classification:\n"
        + "\n".join(f"    {f}" for f in unclassified)
        + "\n\nAdd each to MANIFEST with evidence — an exercise under "
          "`scripts/audit/workspace_binding_audit.py` that shows what "
          "`app.workspace_id` was when its query ran. Do not classify one from "
          "reading it: `worker/heartbeat.py` named `workspace_connection`, "
          "never imported it, and had never executed."
    )


def test_the_manifest_has_not_gone_stale():
    """A name in the manifest that no longer touches a workspace table.

    A manifest that lists files which have moved on stops describing reality
    and starts being scrolled past.
    """
    static = _static_sweep()
    stale = sorted(set(MANIFEST) - static)

    assert not stale, (
        f"{len(stale)} manifest entr(ies) no longer touch a workspace-carrying "
        f"table: {stale}. Remove them, or the manifest is describing a "
        f"codebase that used to exist."
    )


def test_nothing_is_recorded_as_a_live_defect():
    """`INCORRECTLY_UNBOUND` must stay empty, and emptiness must be checked.

    If a future batch finds a caller that genuinely reads unbound against a
    live predicate, this fails until it is fixed or its ownership is recorded
    — which is D1's bar, restated where CI can enforce it.
    """
    defects = sorted(f for f, c in MANIFEST.items() if c == "INCORRECTLY_UNBOUND")
    assert not defects, (
        f"{defects} are recorded as reading a workspace-predicated table with "
        f"nothing bound. Those reads return zero rows today and report it as "
        f"no data."
    )


def test_the_count_is_thirty():
    """The headline, asserted rather than quoted.

    P12-01 closed at 37/37; `memory/technical_library.py` made it 38 and
    `evidence/commit.py` made it 39, each recorded as an addendum in the
    evidence document. If the population changes, this fails and the number in
    the evidence document gets updated with it, rather than the document
    keeping a figure that was true once.
    """
    # IRA CARE: 39 in IRA, minus the 9 files that were not carried over.
    assert len(MANIFEST) == 30, (
        f"the manifest classifies {len(MANIFEST)} files; IRA CARE records 30. "
        f"Update the count here AND in "
        f"docs/cloud/evidence/P12_01_WORKSPACE_BINDING_AUDIT.md together."
    )
