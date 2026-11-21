"""Phase 8 acceptance — the gateway refuses rather than downgrading privacy.

The contract comes from the Phase 1 seam, `core/interfaces/model_provider.py`:

    routing must consider the principal's privacy policy and the content's
    sensitivity BEFORE availability or cost, and must **refuse rather than
    downgrade privacy**.

The second half is the one under pressure. Every incentive in a routing layer
pushes the other way: the local model is down, the request is waiting, an
external provider would answer in 200ms. A gateway that "falls back" under that
pressure leaks a user's health notes to a third party on a bad afternoon and
reports success.

So this is tested as a PROPERTY over the whole input space rather than by
example — every privacy mode × every sensitivity × every provider posture. An
example-based test would pass while leaving the one combination that matters
unchecked.

`test_no_pressure_produces_an_external_route_for_sensitive_content` is the
assertion the file exists for: it exhausts the combinations and asserts that
nothing sensitive reaches a third party under any mode below `external_ok`, and
that credentials never do under any mode at all.
"""
from __future__ import annotations

import itertools

import pytest

from core.interfaces.model_provider import Locality, ProviderDisclosure
from core.models.policy import (
    NEVER_EXTERNAL,
    SENSITIVE,
    SENSITIVITY_LEVELS,
    PrivacyMode,
    admissible,
    admits,
    describe_mode,
    is_sensitive,
    normalise_mode,
    normalise_sensitivity,
    refusal_for,
    stricter_mode,
)

ALL_MODES = list(PrivacyMode)
ALL_SENSITIVITIES = list(SENSITIVITY_LEVELS)


def disclosure(
    *,
    provider_id: str = "p",
    locality: Locality = Locality.EXTERNAL,
    retains=False,
    trains=False,
) -> ProviderDisclosure:
    return ProviderDisclosure(
        provider_id=provider_id,
        display_name=provider_id.title(),
        locality=locality,
        retains_prompts=retains,
        region="eu-west-1",
        trains_on_data=trains,
    )


LOCAL = disclosure(provider_id="ollama-local", locality=Locality.LOCAL,
                   retains=False, trains=False)
SELF_HOSTED = disclosure(provider_id="vllm-own", locality=Locality.SELF_HOSTED,
                         retains=False, trains=False)
#: The most co-operative third party imaginable: says it keeps nothing and
#: trains on nothing. Even this one must be refused for sensitive content below
#: `external_ok`, which is what makes the property about POLICY not posture.
GOOD_EXTERNAL = disclosure(provider_id="clean-cloud", retains=False, trains=False)
RETAINING_EXTERNAL = disclosure(provider_id="keeps-it", retains=True, trains=False)
TRAINING_EXTERNAL = disclosure(provider_id="trains-on-it", retains=False, trains=True)
#: Has not said. The seam requires this be shown as "not known" rather than
#: rendered as "no"; the policy applies the same rule to the decision.
SILENT_EXTERNAL = ProviderDisclosure(
    provider_id="says-nothing", display_name="Says Nothing",
    locality=Locality.EXTERNAL, retains_prompts=None, region="", trains_on_data=None,
)

#: States it does not train, but is silent about RETENTION specifically.
#:
#: This fixture exists because `SILENT_EXTERNAL` has BOTH fields None, so the
#: training check fires first and masks the retention check entirely. Planting
#: "treat unknown retention as no" passed all 44 tests until this was added —
#: a real hole, found by planting the violation rather than by reading the code.
SILENT_ABOUT_RETENTION = ProviderDisclosure(
    provider_id="quiet-on-retention", display_name="Quiet On Retention",
    locality=Locality.EXTERNAL, retains_prompts=None, region="eu-west-1",
    trains_on_data=False,
)
#: The mirror image: states retention, silent about training. Same reasoning.
SILENT_ABOUT_TRAINING = ProviderDisclosure(
    provider_id="quiet-on-training", display_name="Quiet On Training",
    locality=Locality.EXTERNAL, retains_prompts=False, region="eu-west-1",
    trains_on_data=None,
)

ALL_EXTERNAL = [
    GOOD_EXTERNAL, RETAINING_EXTERNAL, TRAINING_EXTERNAL, SILENT_EXTERNAL,
    SILENT_ABOUT_RETENTION, SILENT_ABOUT_TRAINING,
]


# ═══════════════════════════════════════════════════════════════════════════
# The property this file exists for
# ═══════════════════════════════════════════════════════════════════════════


def test_no_pressure_produces_an_external_route_for_sensitive_content():
    """Exhaustive over mode × sensitivity × provider posture.

    The only mode that may carry sensitive content to a third party is
    `external_ok`, which is the user explicitly accepting it — and even then a
    provider that retains, trains, or has not said is refused.
    """
    violations: list[str] = []
    for mode, level, provider in itertools.product(
        ALL_MODES, ALL_SENSITIVITIES, ALL_EXTERNAL
    ):
        allowed, _why = admits(provider, mode=mode, sensitivity=level)
        if not allowed:
            continue
        if level in NEVER_EXTERNAL:
            violations.append(f"{mode.value}/{level}/{provider.provider_id}")
        elif level in SENSITIVE and mode is not PrivacyMode.EXTERNAL_OK:
            violations.append(f"{mode.value}/{level}/{provider.provider_id}")
        elif level in SENSITIVE and (
            provider.retains_prompts is not False
            or provider.trains_on_data is not False
        ):
            violations.append(f"{mode.value}/{level}/{provider.provider_id}")
    assert not violations, (
        "sensitive content was admitted to a third party in these "
        f"combinations: {violations}"
    )


def test_credentials_never_leave_under_any_mode():
    """There is no privacy setting that should send a password or an API key to
    a third party, so there is none. Making it configurable would mean shipping
    a configuration that is always wrong."""
    for mode, provider in itertools.product(ALL_MODES, ALL_EXTERNAL + [SELF_HOSTED]):
        allowed, why = admits(provider, mode=mode, sensitivity="credential")
        assert not allowed, (
            f"credential content was admitted to {provider.provider_id} under "
            f"{mode.value}"
        )
        assert "never leaves this machine" in why


def test_local_inference_is_admitted_under_every_mode_and_sensitivity():
    """The other half of "refuse rather than downgrade": there must always be
    somewhere strict to go, or `local_only` would have nowhere to route and the
    policy would be forced to choose between refusing everything and leaking."""
    for mode, level in itertools.product(ALL_MODES, ALL_SENSITIVITIES):
        allowed, why = admits(LOCAL, mode=mode, sensitivity=level)
        assert allowed, f"local was refused under {mode.value}/{level}: {why}"


def test_local_only_admits_nothing_but_local():
    for provider in ALL_EXTERNAL + [SELF_HOSTED]:
        allowed, why = admits(provider, mode=PrivacyMode.LOCAL_ONLY,
                              sensitivity="normal")
        assert not allowed, f"{provider.provider_id} was admitted under local_only"
        assert "local_only" in why or "never leaves" in why


# ── Unknown is not permission ────────────────────────────────────────────────


def test_a_provider_that_has_not_stated_its_retention_is_refused_for_anything_sensitive():
    """The seam says `None` must be SHOWN as "not known" rather than rendered as
    "no". The same rule applies to the decision: an unstated posture is not a
    safe posture."""
    for mode in (PrivacyMode.LOCAL_FIRST, PrivacyMode.BALANCED,
                 PrivacyMode.EXTERNAL_OK):
        for level in sorted(SENSITIVE):
            allowed, why = admits(SILENT_EXTERNAL, mode=mode, sensitivity=level)
            assert not allowed, (
                f"a provider with unknown retention was admitted for {level} "
                f"under {mode.value}"
            )
            assert "not stated" in why or "never leaves" in why or level in why


def test_an_unknown_privacy_mode_is_the_strictest_not_the_default():
    """An unrecognised mode means the operator asked for a posture this build
    does not have. The only safe reading is the most private one."""
    for value in ("", None, "  ", "permissive", "yolo", "LOCAL_ONLYY"):
        assert normalise_mode(value) is PrivacyMode.LOCAL_ONLY


def test_an_unknown_sensitivity_is_the_most_sensitive():
    """Content nobody classified is content nobody thought about. Guessing
    `normal` is how a health note reaches a third party because a caller forgot
    a field."""
    for value in ("mystery", "SECRET", "1"):
        assert normalise_sensitivity(value) == "credential"
    # An absent value is treated as personal — sensitive, but not so extreme
    # that a plain unclassified chat can never be answered at all.
    assert normalise_sensitivity(None) == "personal"
    assert is_sensitive(None)


def test_every_recognised_sensitivity_round_trips():
    for level in SENSITIVITY_LEVELS:
        assert normalise_sensitivity(level) == level
    assert normalise_sensitivity("  HEALTH ") == "health"


# ── Retention and training ───────────────────────────────────────────────────


def test_a_retaining_provider_is_refused_for_sensitive_content_even_under_external_ok():
    """`external_ok` is the user accepting external processing — not accepting
    that a third party keeps their health records."""
    for level in sorted(SENSITIVE):
        allowed, why = admits(RETAINING_EXTERNAL, mode=PrivacyMode.EXTERNAL_OK,
                              sensitivity=level)
        assert not allowed
        assert "retains prompts" in why or "never leaves" in why


def test_a_training_provider_is_refused_for_sensitive_content():
    for level in sorted(SENSITIVE):
        allowed, why = admits(TRAINING_EXTERNAL, mode=PrivacyMode.EXTERNAL_OK,
                              sensitivity=level)
        assert not allowed
        assert "trains models" in why or "never leaves" in why


def test_a_clean_external_provider_carries_normal_content_from_local_first_upward():
    """The policy must not be so strict that it refuses everything — that would
    be indistinguishable from having no external providers, and would make the
    whole gateway pointless."""
    for mode in (PrivacyMode.LOCAL_FIRST, PrivacyMode.BALANCED,
                 PrivacyMode.EXTERNAL_OK):
        allowed, why = admits(GOOD_EXTERNAL, mode=mode, sensitivity="normal")
        assert allowed, f"clean external refused for normal content under {mode.value}: {why}"


def test_external_ok_carries_sensitive_content_only_to_a_clean_provider():
    """The one permitted sensitive-external combination, asserted explicitly so
    the property test above is known to be non-vacuous."""
    allowed, _ = admits(GOOD_EXTERNAL, mode=PrivacyMode.EXTERNAL_OK,
                        sensitivity="health")
    assert allowed


def test_self_hosted_is_not_treated_as_a_third_party():
    """The operator's own infrastructure is not the user's device, but it is not
    a third party either. Conflating the two would force every self-hosted
    deployment into local_only."""
    # `credential` is excluded deliberately: it never leaves the machine, and
    # self-hosted infrastructure is still not the machine. That rule is asserted
    # by test_credentials_never_leave_under_any_mode.
    for level in sorted(SENSITIVE - NEVER_EXTERNAL):
        allowed, _ = admits(SELF_HOSTED, mode=PrivacyMode.LOCAL_FIRST,
                            sensitivity=level)
        assert allowed, f"self-hosted refused for {level}"
    # But it still respects local_only and the credential rule.
    assert not admits(SELF_HOSTED, mode=PrivacyMode.LOCAL_ONLY,
                      sensitivity="normal")[0]
    assert not admits(SELF_HOSTED, mode=PrivacyMode.EXTERNAL_OK,
                      sensitivity="credential")[0]


# ── Refusals are explicable ──────────────────────────────────────────────────


def test_every_refusal_says_which_check_fired():
    """"Blocked by your privacy policy" leaves a user unable to tell whether to
    change a setting, reclassify their content, or pick another provider."""
    seen: set[str] = set()
    for mode, level, provider in itertools.product(
        ALL_MODES, ALL_SENSITIVITIES, ALL_EXTERNAL
    ):
        allowed, why = admits(provider, mode=mode, sensitivity=level)
        if not allowed:
            assert why, f"a refusal with no reason: {mode.value}/{level}"
            seen.add(why.split(",")[0][:40])
    assert len(seen) >= 4, (
        f"refusal reasons collapsed into {len(seen)} distinct messages; a user "
        f"cannot tell which setting to change"
    )


def test_no_refusal_reason_echoes_the_content():
    """The reason is rendered in a UI. It must describe the policy, never quote
    what the user was asking about."""
    for mode, level, provider in itertools.product(
        ALL_MODES, ALL_SENSITIVITIES, ALL_EXTERNAL
    ):
        _allowed, why = admits(provider, mode=mode, sensitivity=level)
        assert "message" not in why.lower() or "prompts" in why.lower()


# ── Splitting a provider list ────────────────────────────────────────────────


def test_admissible_keeps_the_rejections_so_a_refusal_can_explain_itself():
    """Discarding them makes "no provider available" indistinguishable from
    "nothing is configured" — an operator problem and a policy working."""
    admitted, rejected = admissible(
        [LOCAL, GOOD_EXTERNAL, RETAINING_EXTERNAL],
        mode=PrivacyMode.BALANCED, sensitivity="health",
    )
    assert [d.provider_id for d in admitted] == ["ollama-local"]
    assert len(rejected) == 2
    assert all(why for _pid, why in rejected)


def test_a_refusal_distinguishes_nothing_configured_from_everything_refused():
    nothing = refusal_for(mode=PrivacyMode.BALANCED, sensitivity="normal",
                          rejections=())
    assert "deployment setting, not a privacy refusal" in nothing.reason

    refused = refusal_for(
        mode=PrivacyMode.BALANCED, sensitivity="health",
        rejections=(("keeps-it", "it retains prompts"),),
    )
    assert "did not send this anywhere" in refused.reason
    assert "Nothing was sent to a third party" in refused.reason


def test_a_refusal_is_not_an_exception_and_reports_ok_false():
    """A refusal is an ordinary outcome the caller must show a user, not an
    error to log and swallow."""
    refusal = refusal_for(mode=PrivacyMode.LOCAL_ONLY, sensitivity="health",
                          rejections=(("p", "because"),))
    assert refusal.ok is False
    assert not isinstance(refusal, Exception)
    payload = refusal.public_dict()
    assert payload["refused"] is True
    assert payload["privacy_mode"] == "local_only"
    assert payload["considered"][0]["provider_id"] == "p"


def test_a_multi_provider_refusal_says_how_many_were_considered():
    refusal = refusal_for(
        mode=PrivacyMode.BALANCED, sensitivity="financial",
        rejections=(("a", "reason a"), ("b", "reason b"), ("c", "reason c")),
    )
    assert "3 provider(s) were considered" in refusal.reason


# ── Mode comparison ──────────────────────────────────────────────────────────


def test_stricter_mode_is_a_single_definition():
    assert stricter_mode(PrivacyMode.EXTERNAL_OK, PrivacyMode.LOCAL_ONLY) \
        is PrivacyMode.LOCAL_ONLY
    assert stricter_mode(PrivacyMode.LOCAL_FIRST, PrivacyMode.BALANCED) \
        is PrivacyMode.LOCAL_FIRST
    assert stricter_mode(PrivacyMode.BALANCED, PrivacyMode.BALANCED) \
        is PrivacyMode.BALANCED


@pytest.mark.parametrize("a", ALL_MODES)
@pytest.mark.parametrize("b", ALL_MODES)
def test_stricter_mode_is_commutative_and_never_loosens(a, b):
    """A combiner that can return something more permissive than either input is
    how a privacy setting gets quietly relaxed."""
    assert stricter_mode(a, b) is stricter_mode(b, a)
    assert stricter_mode(a, b) in (a, b)


@pytest.mark.parametrize("mode", ALL_MODES)
def test_every_mode_is_described_for_a_settings_screen(mode):
    """A setting a user cannot understand is one they will set wrongly."""
    text = describe_mode(mode)
    assert len(text) > 60
    assert text.endswith(".")


def test_the_strictest_description_promises_refusal_not_best_effort():
    text = describe_mode(PrivacyMode.LOCAL_ONLY)
    assert "refuses" in text
    assert "Nothing leaves this machine" in text


def test_the_most_permissive_description_still_states_its_limits():
    """`external_ok` must not read as "anything goes", because it is not."""
    text = describe_mode(PrivacyMode.EXTERNAL_OK)
    assert "except credentials" in text
    assert "not stated" in text


# ── The vocabulary is shared, not duplicated ─────────────────────────────────


def test_the_sensitivity_vocabulary_matches_phase_5s_memory_categories():
    """Two vocabularies for "how sensitive is this" would disagree, and the
    disagreement would be resolved by whichever was more permissive."""
    from core.privacy.memory import SENSITIVITIES

    assert set(SENSITIVITY_LEVELS) == set(SENSITIVITIES), (
        "the routing policy and the memory vault disagree about the "
        "sensitivity vocabulary"
    )


def test_normal_is_the_only_non_sensitive_level():
    assert SENSITIVE == set(SENSITIVITY_LEVELS) - {"normal"}
    assert not is_sensitive("normal")

def test_unknown_retention_alone_is_enough_to_refuse():
    """Isolates the retention check from the training check.

    `SILENT_EXTERNAL` has both fields None, so the training check fires first
    and hides whether the retention check works at all. Planting "treat unknown
    retention as no" passed the whole suite until this test existed — the seam's
    explicit prohibition was unguarded.
    """
    for mode in (PrivacyMode.LOCAL_FIRST, PrivacyMode.BALANCED,
                 PrivacyMode.EXTERNAL_OK):
        for level in sorted(SENSITIVE - NEVER_EXTERNAL):
            allowed, why = admits(SILENT_ABOUT_RETENTION, mode=mode,
                                  sensitivity=level)
            assert not allowed, (
                f"a provider silent about RETENTION was admitted for {level} "
                f"under {mode.value}"
            )
            if mode is PrivacyMode.EXTERNAL_OK:
                # Under external_ok the mode check does not fire, so the
                # retention check is the only thing that can refuse.
                assert "has not stated whether it retains" in why


def test_unknown_training_alone_is_enough_to_refuse():
    """The mirror image, isolated for the same reason."""
    allowed, why = admits(SILENT_ABOUT_TRAINING, mode=PrivacyMode.EXTERNAL_OK,
                          sensitivity="health")
    assert not allowed
    assert "has not stated whether it trains" in why


def test_unknown_retention_still_carries_normal_content():
    """The policy must not be so strict that an undocumented provider is useless
    for ordinary chat — that would make "state your retention policy" a
    requirement nobody meets and the gateway pointless."""
    allowed, _ = admits(SILENT_ABOUT_RETENTION, mode=PrivacyMode.BALANCED,
                        sensitivity="normal")
    assert allowed
