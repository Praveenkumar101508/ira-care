"""Action Guard — one generic guard in front of any agent action.

Requirements pinned here:
  - a risky file delete shows its blast radius, requires approval, and has a
    snapshot taken BEFORE anything is destroyed
  - an approved action executes, writes ledger records, and yields a Proof Card
  - critical actions are BLOCKED outright — approval cannot release them
  - preview simulates and never executes
  - risk is derived; a caller can raise it, never lower it
  - the hash chain still verifies, and tampering with a record breaks it
"""
import os
from pathlib import Path

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import pytest

from trust import action_guard as guard
from trust.ledger import GENESIS, compute_hash, verify_chain


@pytest.fixture(autouse=True)
def lockdown_inactive(monkeypatch):
    """Pin the lockdown state, because P14-R1 made the guard depend on it.

    `guard_action` now consults `get_lockdown_state()` before staging or
    confirming anything (FINDING AB — until P14-R1 the state restricted
    nothing). These tests run with no Redis and no Postgres, so that call
    reports **UNKNOWN**, and UNKNOWN refuses `high` and `critical` work — which
    is every interesting case in this file. Five tests here went red the moment
    the enforcement was wired, and they were right to: the guard genuinely does
    refuse in that situation.

    That is a property of the LOCKDOWN policy, not of the approval machinery
    this file is about, and it is asserted where it belongs — in
    `test_p14r1_lockdown_enforcement.py` and in both acceptance scripts. Here it
    is pinned to INACTIVE so these tests measure what they claim to.

    Pinned explicitly rather than by stubbing the store, so the coupling is
    visible in the file that depends on it. And `test_lockdown_still_reaches_
    this_guard` below deliberately does NOT use the pin, so nobody can read
    this fixture as "the guard is unaffected by lockdown".

    Real deployments always have both stores — the portable Compose file ships
    postgres and redis, and `tests/portable/test_portable_compose.py` requires
    them — so UNKNOWN in production means an actual double outage, not a
    configuration this fixture is papering over.
    """
    from utils.security_tools import LockdownState

    async def _inactive():
        return LockdownState.INACTIVE

    monkeypatch.setattr("utils.security_tools.get_lockdown_state", _inactive)


@pytest.fixture
def no_ledger(monkeypatch):
    """The ledger needs Postgres; make writes fail so the fail-soft path runs."""
    async def _down(*a, **kw):
        raise RuntimeError("no database in tests")

    monkeypatch.setattr("trust.ledger.append_record", _down)
    monkeypatch.setattr("trust.ledger.records_for", _down)


def _file_desc(path: Path, operation: str, **params):
    return guard.ActionDescriptor(
        type="file_change", target=str(path), requested_by="owner",
        params={"operation": operation, **params},
    )


# ── Risk derivation ──────────────────────────────────────────────────────────

def test_delete_is_high_and_modify_is_medium(tmp_path):
    target = tmp_path / "notes.txt"
    assert guard.classify_file_risk(target, "delete") == "high"
    assert guard.classify_file_risk(target, "modify") == "medium"


def test_a_protected_path_is_critical(tmp_path):
    assert guard.classify_file_risk(tmp_path / ".env", "modify") == "critical"
    assert guard.classify_file_risk(Path("/home/x/.ssh/id_rsa"), "modify") == "critical"


def test_command_risk_uses_the_same_two_gates_as_the_executor():
    assert guard.classify_command_risk("git status")[0] == "low"
    assert guard.classify_command_risk("rm -rf /")[0] == "critical"
    assert guard.classify_command_risk("cat /etc/passwd; ls")[0] == "critical"
    assert guard.classify_command_risk("python train.py")[0] == "high"


def test_a_caller_can_raise_the_risk_floor_but_never_lower_it(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("x", encoding="utf-8")

    raised = guard.ActionDescriptor(
        type="file_change", target=str(target), requested_by="owner",
        params={"operation": "modify"}, min_risk="high",
    )
    spec, _sim, _ex = guard.build(raised)
    assert spec.risk == "high"

    lowered = guard.ActionDescriptor(
        type="file_change", target=str(target), requested_by="owner",
        params={"operation": "delete"}, min_risk="low",
    )
    spec, _sim, _ex = guard.build(lowered)
    assert spec.risk == "high", "a caller must not be able to talk risk down"


# ── Preview: blast radius without execution ──────────────────────────────────

@pytest.mark.asyncio
async def test_preview_shows_blast_radius_and_does_not_execute(tmp_path):
    target = tmp_path / "report.txt"
    target.write_text("keep me", encoding="utf-8")

    result = await guard.preview_action(_file_desc(target, "delete"))

    assert result["executed"] is False
    assert target.exists(), "preview must never touch the target"
    assert result["risk"] == "high"
    assert result["approval_required"] == "owner_password"
    assert result["blast_radius"]["paths"] == [str(target)]
    assert result["blast_radius"]["bytes"] == len("keep me")
    assert result["proof_card_summary"]["simulated"] is True


@pytest.mark.asyncio
async def test_preview_of_a_missing_file_fails_the_simulation(tmp_path):
    result = await guard.preview_action(_file_desc(tmp_path / "ghost.txt", "delete"))

    assert result["blocked"] is True
    assert any("does not exist" in r for r in result["blocked_reasons"])


@pytest.mark.asyncio
async def test_preview_of_a_critical_action_reports_blocked_before_any_token(tmp_path):
    secret = tmp_path / ".env"
    secret.write_text("SECRET=1", encoding="utf-8")

    result = await guard.preview_action(_file_desc(secret, "delete"))

    assert result["risk"] == "critical"
    assert result["blocked"] is True
    assert "critical actions are blocked outright" in result["blocked_reasons"]
    assert result["would_execute"] is False
    assert secret.exists()


@pytest.mark.asyncio
async def test_preview_of_a_command_reports_what_would_run():
    result = await guard.preview_action(guard.ActionDescriptor(
        type="shell_command", target="git status", requested_by="owner",
    ))

    assert result["risk"] == "low"
    assert result["blast_radius"]["command"] == "git status"
    assert result["blast_radius"]["network"] is False
    assert result["executed"] is False


# ── Staging: approval required, snapshot taken ───────────────────────────────

@pytest.mark.asyncio
async def test_risky_delete_requires_approval_and_snapshots_first(tmp_path, no_ledger):
    target = tmp_path / "important.txt"
    target.write_text("original contents", encoding="utf-8")

    staged = await guard.guard_action(_file_desc(target, "delete"), is_owner=True)

    assert staged["status"] == "confirmation_required"
    assert staged["approval_level"] == "owner_password"
    assert target.exists(), "staging must not delete anything"

    snapshots = list((tmp_path / guard.SNAPSHOT_DIRNAME).glob("important.txt.*.snap"))
    assert snapshots, "a high-risk file action needs a snapshot before approval"
    assert snapshots[0].read_text(encoding="utf-8") == "original contents"
    assert staged["proof_card"]["reversal"]["validated"] is True


@pytest.mark.asyncio
async def test_a_non_owner_cannot_stage_a_guarded_action(tmp_path, no_ledger):
    target = tmp_path / "f.txt"
    target.write_text("x", encoding="utf-8")

    outcome = await guard.guard_action(_file_desc(target, "delete"), is_owner=False)
    assert outcome["status"] == "forbidden"
    assert target.exists()


@pytest.mark.asyncio
async def test_a_critical_action_is_blocked_not_merely_warned(tmp_path, no_ledger):
    secret = tmp_path / ".env"
    secret.write_text("SECRET=1", encoding="utf-8")

    outcome = await guard.guard_action(_file_desc(secret, "delete"), is_owner=True)

    assert outcome["status"] == "blocked"
    assert "critical" in outcome["reason"]
    assert "token" not in outcome, "a blocked action must not be handed a token"
    assert secret.exists()


@pytest.mark.asyncio
async def test_a_blocked_command_never_reaches_execution(no_ledger):
    outcome = await guard.guard_action(
        guard.ActionDescriptor(type="shell_command", target="rm -rf /", requested_by="owner"),
        is_owner=True,
    )
    assert outcome["status"] == "blocked"


# ── Execution: proof card + ledger records ───────────────────────────────────

@pytest.mark.asyncio
async def test_approved_action_executes_and_returns_a_proof_card(tmp_path, monkeypatch):
    target = tmp_path / "draft.txt"
    target.write_text("v1", encoding="utf-8")

    appended: list[dict] = []

    async def _append(kind, *, user_id, action_id, task_id=None, record=None):
        appended.append({"kind": kind, "action_id": action_id, "record": record})
        return {"seq": len(appended)}

    async def _records_for(action_id):
        return [
            {"kind": a["kind"], "seq": i + 1, "at": "2026-07-26T00:00:00Z",
             "record": {**(a["record"] or {}), "approved_by": "owner_password"}}
            for i, a in enumerate(appended) if a["action_id"] == action_id
        ]

    monkeypatch.setattr("trust.ledger.append_record", _append)
    monkeypatch.setattr("trust.ledger.records_for", _records_for)

    desc = _file_desc(target, "modify", content="v2")
    staged = await guard.guard_action(desc, is_owner=True)
    outcome = await guard.guard_action(
        desc, is_owner=True, confirm_token=staged["token"],
        approval_artifact="plan_approval",
    )

    assert outcome["status"] == "executed"
    assert target.read_text(encoding="utf-8") == "v2"
    assert outcome["ledger_recorded"] is True
    kinds = [a["kind"] for a in appended]
    assert kinds[0] == "action", "intent is recorded BEFORE the side effect"
    assert "execution" in kinds
    summary = outcome["proof_card_summary"]
    assert summary["status"] == "executed"
    assert summary["local_only"] is True


@pytest.mark.asyncio
async def test_a_wrong_token_executes_nothing(tmp_path, no_ledger):
    target = tmp_path / "draft.txt"
    target.write_text("v1", encoding="utf-8")

    desc = _file_desc(target, "modify", content="v2")
    await guard.guard_action(desc, is_owner=True)
    outcome = await guard.guard_action(
        desc, is_owner=True, confirm_token="not-a-real-token",
    )

    assert outcome["status"] != "executed"
    assert target.read_text(encoding="utf-8") == "v1"


@pytest.mark.asyncio
# P1-09: not a ledger test. It took `no_ledger` so the old fail-soft path ran;
# a ledger that cannot record now refuses the delete, so it records instead.
async def test_the_snapshot_restores_the_file_after_a_guarded_delete(tmp_path, ledger_recording):
    target = tmp_path / "important.txt"
    target.write_text("original contents", encoding="utf-8")

    desc = _file_desc(target, "delete")
    staged = await guard.guard_action(desc, is_owner=True)
    # the descriptor is rebuilt on confirm, so grab the snapshot from disk
    snapshot_path = next((tmp_path / guard.SNAPSHOT_DIRNAME).glob("important.txt.*.snap"))
    outcome = await guard.guard_action(
        desc, is_owner=True, confirm_token=staged["token"],
        approval_artifact="owner_password",
    )

    assert outcome["status"] == "executed"
    assert not target.exists()
    target.write_text(snapshot_path.read_text(encoding="utf-8"), encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "original contents"


# ── Ledger integrity ─────────────────────────────────────────────────────────

def _chain(bodies: list[dict]) -> list[dict]:
    prev, out = GENESIS, []
    for i, record in enumerate(bodies, start=1):
        body = {"seq": i, "user_id": "owner", "action_id": "a1", "task_id": None,
                "kind": record["kind"], "record": record["record"], "prev_hash": prev}
        digest = compute_hash(prev, body)
        out.append({**body, "hash": digest})
        prev = digest
    return out


def test_guarded_action_records_still_verify_as_a_chain():
    records = _chain([
        {"kind": "action", "record": {"intended_action": "delete important.txt"}},
        {"kind": "approval", "record": {"approved_by": "owner_password"}},
        {"kind": "execution", "record": {"result": {"deleted": "important.txt"}}},
    ])
    assert verify_chain(records) == 3


def test_tampering_with_a_guarded_record_breaks_verification():
    records = _chain([
        {"kind": "action", "record": {"intended_action": "delete important.txt"}},
        {"kind": "execution", "record": {"result": {"deleted": "important.txt"}}},
    ])
    records[0]["record"]["intended_action"] = "delete nothing_important.txt"

    with pytest.raises(ValueError, match="hash mismatch"):
        verify_chain(records)


# ── Generic interface ────────────────────────────────────────────────────────

def test_three_action_types_are_wired():
    assert {"file_change", "calendar_change", "shell_command"} <= set(guard._ADAPTERS)


def test_an_unknown_action_type_is_rejected_at_construction():
    with pytest.raises(ValueError, match="unknown action type"):
        guard.ActionDescriptor(type="mind_control", target="x", requested_by="owner")


def test_a_new_action_type_can_be_registered_without_touching_the_guard():
    from actions.shadow_world import ActionSpec

    def _adapter(desc):
        spec = ActionSpec(kind="db_operation", risk="medium", requested_by=desc.requested_by,
                          intended_action=f"run migration {desc.target}")

        async def _sim(_s):
            return {"ok": True, "detail": "dry run"}

        return spec, _sim, lambda: {"migrated": desc.target}

    guard.register_adapter("db_operation", _adapter)
    try:
        spec, _sim, _ex = guard.build(guard.ActionDescriptor(
            type="db_operation", target="020_trust_ledger", requested_by="owner",
        ))
        assert spec.kind == "db_operation"
    finally:
        guard._ADAPTERS.pop("db_operation", None)


# ── P14-R1: the lockdown enforcement reaches THIS guard ─────────────────────

@pytest.mark.asyncio
async def test_lockdown_still_reaches_this_guard(tmp_path, monkeypatch, no_ledger):
    """The counterweight to the `lockdown_inactive` fixture.

    That fixture pins the state so the rest of this file measures the approval
    machinery rather than the lockdown policy. Read on its own it invites the
    wrong conclusion — that `guard_action` is independent of lockdown — and a
    fixture that quietly disables a security control for a whole file is how
    controls get lost.

    So this one overrides the pin and asserts the opposite: with both stores
    unreadable, a high-risk delete through THIS guard is refused. It is the
    situation the five tests above actually hit before the fixture existed.
    """
    from utils.security_tools import LockdownState

    async def _unknown():
        return LockdownState.UNKNOWN

    monkeypatch.setattr("utils.security_tools.get_lockdown_state", _unknown)

    target = tmp_path / "important.txt"
    target.write_text("original contents", encoding="utf-8")

    outcome = await guard.guard_action(_file_desc(target, "delete"), is_owner=True)

    assert outcome["refused_by"] == "lockdown", (
        f"a high-risk delete proceeded while the lockdown state was UNKNOWN: "
        f"{outcome}. The enforcement point does not reach this guard, so the "
        f"`lockdown_inactive` fixture above is hiding nothing — which means "
        f"FINDING AB is re-opened.")
    assert target.read_text(encoding="utf-8") == "original contents"
