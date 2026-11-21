"""Routing policy — where a request may be sent, and where it may never be.

The Phase 1 seam states the contract this module exists to honour:

    routing must consider the principal's privacy policy and the content's
    sensitivity BEFORE availability or cost, and must **refuse rather than
    downgrade privacy**.

Both halves matter and the second is the harder one to keep. Every incentive in
a routing layer pushes the other way: the local model is down, the request is
waiting, an external provider is right there and would answer in 200ms. A
gateway that "falls back" under that pressure is a gateway that leaks a user's
health notes to a third party on a bad afternoon, and reports success.

So refusal is the fallback. `route()` returns either a provider that satisfies
the policy or a `RoutingRefusal` — there is no third outcome and no parameter
that relaxes it.

── The two inputs, and why order matters ────────────────────────────────────

**Privacy mode** is the user's (or edition's) standing answer:

    local_only    inference never leaves this machine
    local_first   prefer local; external permitted for non-sensitive content
    balanced      external permitted unless the content is sensitive
    external_ok   external permitted for anything the user has not restricted

**Sensitivity** is the content's classification, reusing Phase 5's memory
vocabulary rather than inventing a second one — `normal`, `personal`, `health`,
`financial`, `family`, `credential`. Two vocabularies for "how sensitive is
this" would disagree, and the disagreement would be resolved somewhere by
whichever was more permissive.

The policy is evaluated as a permission, not a preference: `admits()` answers
"may this provider carry this request", and availability and cost are only
consulted among providers that already passed.

── Unknown is not permission ────────────────────────────────────────────────

A provider whose `retains_prompts` is `None` has not stated whether it keeps
prompts. That is treated as "may retain", so it is refused for anything above
`normal`. The seam is explicit that `None` must be SHOWN as "not known" rather
than rendered as "no", and this module applies the same rule to the decision:
an unstated posture is not a safe posture.

`credential` never leaves the machine, under any mode. There is no privacy mode
that permits sending a password or an API key to a third party, and making that
configurable would mean shipping a configuration that is always wrong.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final, Optional, Sequence

from core.interfaces.model_provider import Locality, ProviderDisclosure


class PrivacyMode(str, Enum):
    """The standing answer about where inference may happen.

    `str` mixin so it serialises as its token and matches `IRA_PRIVACY_MODE`.
    """

    LOCAL_ONLY = "local_only"
    LOCAL_FIRST = "local_first"
    BALANCED = "balanced"
    EXTERNAL_OK = "external_ok"


#: Strictest first. Used to compare modes and to clamp — never to combine two
#: modes into something more permissive than either.
_MODE_ORDER: Final[tuple[PrivacyMode, ...]] = (
    PrivacyMode.LOCAL_ONLY,
    PrivacyMode.LOCAL_FIRST,
    PrivacyMode.BALANCED,
    PrivacyMode.EXTERNAL_OK,
)


def normalise_mode(value: Optional[str]) -> PrivacyMode:
    """Parse a privacy mode. Anything unrecognised becomes the STRICTEST.

    Not the default and not an error — the strictest. An unrecognised mode means
    the operator believes they configured something this build does not
    implement, and the only safe reading of "I asked for a privacy posture you
    do not have" is to give them the most private one.
    """
    if not value:
        return PrivacyMode.LOCAL_ONLY
    try:
        return PrivacyMode(str(value).strip().lower())
    except ValueError:
        return PrivacyMode.LOCAL_ONLY


def stricter_mode(a: PrivacyMode, b: PrivacyMode) -> PrivacyMode:
    """The stricter of two modes. One definition, so nothing open-codes it."""
    return a if _MODE_ORDER.index(a) <= _MODE_ORDER.index(b) else b


# ── Sensitivity ──────────────────────────────────────────────────────────────

#: Phase 5's memory vocabulary, reused deliberately. See the module docstring.
SENSITIVITY_LEVELS: Final[tuple[str, ...]] = (
    "normal", "personal", "health", "financial", "family", "credential",
)

#: Everything above `normal`. These are the categories a user would be upset to
#: find at a third party, and the ones the policy protects.
SENSITIVE: Final[frozenset[str]] = frozenset(SENSITIVITY_LEVELS[1:])

#: Never leaves the machine, under any privacy mode. There is no configuration
#: that should send a password to a third party, so there is none.
NEVER_EXTERNAL: Final[frozenset[str]] = frozenset({"credential"})


def normalise_sensitivity(value: Optional[str]) -> str:
    """Parse a sensitivity. Unrecognised is treated as the MOST sensitive.

    Same fail-closed reading as `normalise_mode`: content nobody has classified
    is content nobody has thought about, and guessing `normal` for it is how a
    health note reaches an external provider because a caller forgot a field.
    """
    if not value:
        return "personal"
    token = str(value).strip().lower()
    return token if token in SENSITIVITY_LEVELS else "credential"


def is_sensitive(sensitivity: Optional[str]) -> bool:
    return normalise_sensitivity(sensitivity) in SENSITIVE


# ── The decision ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RoutingRefusal:
    """No provider may carry this request, and this is why.

    A dataclass rather than an exception because a refusal is an ordinary
    outcome that the caller must show to a user, not an error to log. The
    `reason` is written to be read by a person.
    """

    reason: str
    privacy_mode: PrivacyMode
    sensitivity: str
    #: What was considered and why each was rejected, for the disclosure panel
    #: and for anyone debugging "why did IRA say it couldn't answer?".
    considered: tuple[tuple[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return False

    def public_dict(self) -> dict[str, object]:
        return {
            "refused": True,
            "reason": self.reason,
            "privacy_mode": self.privacy_mode.value,
            "sensitivity": self.sensitivity,
            "considered": [
                {"provider_id": pid, "rejected_because": why}
                for pid, why in self.considered
            ],
        }


def admits(
    disclosure: ProviderDisclosure,
    *,
    mode: PrivacyMode,
    sensitivity: str,
) -> tuple[bool, str]:
    """May this provider carry this request? Returns (allowed, reason-if-not).

    The single place the policy is decided. Every check is spelled out so a
    refusal can name which one fired — "privacy policy" as one message makes it
    impossible for a user to know whether to change their setting, reclassify
    their content, or pick a different provider.

    Ordering inside the function is deliberate: locality first, then retention,
    then training. Locality is the coarsest and the one users understand, so it
    is the reason they see when several apply.
    """
    level = normalise_sensitivity(sensitivity)

    # 1. Local always admits. There is no privacy mode strict enough to refuse
    #    on-device inference, and treating one as suspect would leave
    #    `local_only` with nowhere to route.
    if disclosure.locality is Locality.LOCAL:
        return True, ""

    # 2. Credentials never leave, whatever the mode says.
    if level in NEVER_EXTERNAL:
        return False, (
            f"content classified {level!r} never leaves this machine, under any "
            f"privacy setting"
        )

    # 3. local_only means what it says.
    if mode is PrivacyMode.LOCAL_ONLY:
        return False, (
            "the privacy setting is 'local_only', so inference never leaves "
            "this machine"
        )

    # 4. Self-hosted is the operator's own infrastructure — not a third party,
    #    but not the user's device either. Permitted from local_first upward.
    if disclosure.locality is Locality.SELF_HOSTED:
        return True, ""

    # From here the provider is EXTERNAL: a third party.

    # 5. Sensitive content does not go to a third party in local_first or
    #    balanced. `external_ok` is the user explicitly accepting it.
    if level in SENSITIVE and mode in (PrivacyMode.LOCAL_FIRST, PrivacyMode.BALANCED):
        return False, (
            f"this content is classified {level!r} and the privacy setting is "
            f"{mode.value!r}, which does not send sensitive content to a third "
            f"party"
        )

    # 6. Retention. `None` means the provider has not said, and an unstated
    #    posture is not a safe posture.
    if disclosure.retains_prompts is None and level != "normal":
        return False, (
            f"{disclosure.display_name} has not stated whether it retains "
            f"prompts, and this content is classified {level!r}. An unknown "
            f"retention posture is treated as 'may retain'."
        )
    if disclosure.retains_prompts and level in SENSITIVE:
        return False, (
            f"{disclosure.display_name} retains prompts, and this content is "
            f"classified {level!r}"
        )

    # 7. Training on submitted data. Unknown is again not permission.
    if disclosure.trains_on_data is None and level in SENSITIVE:
        return False, (
            f"{disclosure.display_name} has not stated whether it trains on "
            f"submitted data, and this content is classified {level!r}"
        )
    if disclosure.trains_on_data and level in SENSITIVE:
        return False, (
            f"{disclosure.display_name} trains models on submitted data, and "
            f"this content is classified {level!r}"
        )

    return True, ""


def admissible(
    disclosures: Sequence[ProviderDisclosure],
    *,
    mode: PrivacyMode,
    sensitivity: str,
) -> tuple[tuple[ProviderDisclosure, ...], tuple[tuple[str, str], ...]]:
    """Split providers into those the policy admits and those it does not.

    Returns (admitted, rejections). The rejections are kept rather than
    discarded so a refusal can explain what was considered — otherwise "no
    provider available" is indistinguishable from "nothing is configured".
    """
    admitted: list[ProviderDisclosure] = []
    rejected: list[tuple[str, str]] = []
    for disclosure in disclosures:
        allowed, why = admits(disclosure, mode=mode, sensitivity=sensitivity)
        if allowed:
            admitted.append(disclosure)
        else:
            rejected.append((disclosure.provider_id, why))
    return tuple(admitted), tuple(rejected)


def refusal_for(
    *,
    mode: PrivacyMode,
    sensitivity: str,
    rejections: Sequence[tuple[str, str]],
) -> RoutingRefusal:
    """Build the refusal a user sees when nothing admissible remains.

    Distinguishes "nothing is configured" from "everything was refused",
    because the first is an operator problem and the second is a policy
    working.
    """
    level = normalise_sensitivity(sensitivity)
    if not rejections:
        reason = (
            "No model provider is configured, so IRA cannot answer this. This "
            "is a deployment setting, not a privacy refusal."
        )
    elif len(rejections) == 1:
        reason = (
            f"IRA did not send this anywhere: {rejections[0][1]}. Nothing was "
            f"sent to a third party and no answer was produced."
        )
    else:
        reason = (
            f"IRA did not send this anywhere. {len(rejections)} provider(s) were "
            f"considered and each was refused by your privacy settings; the "
            f"first was because {rejections[0][1]}. Nothing was sent to a third "
            f"party and no answer was produced."
        )
    return RoutingRefusal(
        reason=reason, privacy_mode=mode, sensitivity=level,
        considered=tuple(rejections),
    )


def describe_mode(mode: PrivacyMode) -> str:
    """One sentence a user can read, for the settings screen."""
    return {
        PrivacyMode.LOCAL_ONLY:
            "Nothing leaves this machine. IRA only uses models running locally, "
            "and refuses rather than sending anything to a third party.",
        PrivacyMode.LOCAL_FIRST:
            "Local models are preferred. Non-sensitive requests may go to an "
            "external provider when no local model can handle them; anything "
            "sensitive stays here.",
        PrivacyMode.BALANCED:
            "IRA picks whichever model suits the request, but anything "
            "classified sensitive stays on this machine.",
        PrivacyMode.EXTERNAL_OK:
            "IRA may use external providers for anything except credentials. "
            "Providers that have not stated their retention policy are still "
            "refused for sensitive content.",
    }[mode]


__all__ = [
    "NEVER_EXTERNAL",
    "SENSITIVE",
    "SENSITIVITY_LEVELS",
    "PrivacyMode",
    "RoutingRefusal",
    "admissible",
    "admits",
    "describe_mode",
    "is_sensitive",
    "normalise_mode",
    "normalise_sensitivity",
    "refusal_for",
    "stricter_mode",
]
