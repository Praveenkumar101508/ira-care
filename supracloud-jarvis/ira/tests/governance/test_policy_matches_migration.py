"""Phase 4 — the shipped tool policies and their migration seed must not drift.

Same guard as Phase 3's catalogue: `core/governance/policies.py` is what the
process reads, `postgres/025_governance.sql` is the durable record, and if the
two disagree then which answer is true depends on the code path.

Here it is worse than a cosmetic mismatch. `max_effect` is a safety ceiling. A
migration that seeds `email.send` with `max_effect='allow'` while the code says
`require_confirmation` means an operator inspecting the database is told
something false about what the system will do.

Also asserted: the vocabulary the code emits is accepted by the CHECK
constraints. An effect the code can produce but the column refuses is a runtime
insert failure and nowhere earlier.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.governance.policies import SEED_POLICIES, policy_for
from core.interfaces.permission import Effect, RiskLevel

# tests/governance/ -> tests/ -> ira/ -> supracloud-jarvis/
MIGRATION = (
    Path(__file__).resolve().parents[3] / "postgres" / "025_governance.sql"
)


@pytest.fixture(scope="module")
def sql() -> str:
    assert MIGRATION.is_file(), f"missing {MIGRATION}"
    return MIGRATION.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def seed_block(sql: str) -> str:
    block = sql.split("INSERT INTO tool_policies", 1)
    assert len(block) == 2, "no tool_policies seed in the migration"
    return block[1].split("ON CONFLICT", 1)[0]


def _row(seed_block: str, domain: str, action: str) -> str:
    """The VALUES tuple for one policy, as raw text."""
    marker = f"('{domain}',"
    start = seed_block.find(marker)
    while start != -1:
        end = seed_block.find("\n", start)
        line = seed_block[start: end if end != -1 else len(seed_block)]
        if f"'{action}'" in line:
            return line
        start = seed_block.find(marker, start + 1)
    raise AssertionError(f"no seed row for {domain}.{action}")


def test_every_code_policy_is_seeded(seed_block):
    for policy in SEED_POLICIES:
        _row(seed_block, policy.domain, policy.action)  # raises if absent


def test_the_seed_introduces_no_policy_the_code_does_not_know(seed_block):
    """The other direction. A policy in the database that the code has never
    heard of would be enforced by nobody."""
    seeded = set(re.findall(r"\('([a-z]+)',\s*'([a-z_]+)',", seed_block))
    known = {(p.domain, p.action) for p in SEED_POLICIES}
    extra = seeded - known
    assert not extra, (
        f"the migration seeds policies the code does not implement: {sorted(extra)}"
    )


@pytest.mark.parametrize("policy", SEED_POLICIES, ids=lambda p: f"{p.domain}.{p.action}")
def test_max_effect_matches_the_seed(seed_block, policy):
    """The safety ceiling. The most important field in this file."""
    row = _row(seed_block, policy.domain, policy.action)
    assert f"'{policy.max_effect.value}'" in row, (
        f"{policy.domain}.{policy.action}: code says max_effect="
        f"{policy.max_effect.value!r}, which is not in the seed row"
    )


@pytest.mark.parametrize("policy", SEED_POLICIES, ids=lambda p: f"{p.domain}.{p.action}")
def test_default_effect_matches_the_seed(seed_block, policy):
    row = _row(seed_block, policy.domain, policy.action)
    assert f"'{policy.default_effect.value}'" in row


@pytest.mark.parametrize("policy", SEED_POLICIES, ids=lambda p: f"{p.domain}.{p.action}")
def test_risk_matches_the_seed(seed_block, policy):
    row = _row(seed_block, policy.domain, policy.action)
    assert f"'{policy.risk.value}'" in row


@pytest.mark.parametrize("policy", SEED_POLICIES, ids=lambda p: f"{p.domain}.{p.action}")
def test_side_effecting_matches_the_seed(seed_block, policy):
    row = _row(seed_block, policy.domain, policy.action)
    expected = "TRUE" if policy.side_effecting else "FALSE"
    assert expected in row, (
        f"{policy.domain}.{policy.action}: code says side_effecting="
        f"{policy.side_effecting}, seed row disagrees"
    )


# ── Vocabulary vs CHECK constraints ──────────────────────────────────────────


@pytest.mark.parametrize("effect", list(Effect))
def test_every_effect_the_code_can_emit_is_accepted_by_the_schema(sql, effect):
    assert f"'{effect.value}'" in sql, (
        f"the code can produce effect {effect.value!r} but no CHECK constraint "
        f"in 025 mentions it — an INSERT would be refused at runtime."
    )


@pytest.mark.parametrize("risk", list(RiskLevel))
def test_every_risk_level_is_accepted_by_the_schema(sql, risk):
    assert f"'{risk.value}'" in sql


def test_the_decision_log_is_append_only_in_the_schema(sql):
    """Evidence that can be edited is not evidence."""
    assert "permission_decisions_append_only" in sql
    assert "BEFORE UPDATE OR DELETE ON permission_decisions" in sql


def test_the_governance_tables_are_rls_scoped(sql):
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "WITH CHECK" in sql
    assert "NULLIF(current_setting('app.tenant_id', true), '')" in sql


def test_tool_policies_is_deliberately_not_tenant_scoped(sql):
    """It holds no customer data. Asserted so that "not scoped" stays a decision
    rather than becoming an oversight nobody notices."""
    loop = sql.split("scoped TEXT[] := ARRAY[", 1)[1].split("]", 1)[0]
    assert "'tool_policies'" not in loop
    for table in ("constitution_rules", "permission_grants",
                  "permission_decisions", "approval_events"):
        assert f"'{table}'" in loop, f"{table} is missing from the RLS policy loop"


# ── The guard must be able to fail ───────────────────────────────────────────


def test_the_row_extractor_finds_the_right_row(seed_block):
    """A helper that matched everything would make every check above vacuous."""
    row = _row(seed_block, "email", "send")
    assert "'send'" in row
    assert "'delete'" not in row, "the extractor is reading past its own row"


def test_the_row_extractor_raises_for_an_unseeded_policy(seed_block):
    with pytest.raises(AssertionError):
        _row(seed_block, "email", "forward")


def test_policy_for_returns_none_rather_than_a_permissive_default():
    """None is not "allowed" — the broker turns it into a denial."""
    assert policy_for("nonsense", "whatever") is None
