"""May this avatar be offered — and to whom?

This is the fail-closed gate the transformation brief requires: *"Invalid avatar
licence metadata → do not publish the avatar."* It answers one question and
answers it conservatively.

The distinction that does the real work here is between two permissions people
routinely confuse:

  **Use it yourself.** The owner running IRA Private on their own machine, with
  their own portrait. Nobody is redistributing anything. The rights question is
  the owner's own business.

  **Redistribute it.** A hosted service handing the same asset to thousands of
  unrelated users. That is publication. It needs a licence that positively
  permits commercial use AND redistribution, verified by a human who read it.

Collapsing those two into one boolean is how a personal likeness ends up in a
public product. `edition_availability` keeps them apart, and this module refuses
to promote a private-only asset into a cloud catalogue no matter what else is
set on it.

**Unknown is treated as no.** A licence field of `None` means nobody has
checked, and an unchecked licence is not a permissive one. Every refusal carries
a reason, so an operator can tell "nobody filled this in" apart from "the
licence says no" — the first is fixable by reading the licence file, the second
is not fixable at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.companion.models import (
    AvatarEntry,
    VoiceEntry,
)

#: Editions an entry can be offered in, keyed by its `edition_availability`.
_AVAILABILITY: dict[str, frozenset[str]] = {
    "private": frozenset({"private"}),
    "cloud": frozenset({"cloud"}),
    "both": frozenset({"private", "cloud"}),
}


@dataclass(frozen=True, slots=True)
class LicenceDecision:
    """Whether an asset may be offered, and why not when it may not."""

    allowed: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return self.allowed

    @property
    def reason(self) -> str:
        """All refusal reasons as one line, for logs and API detail."""
        return "; ".join(self.reasons)


def _redistribution_required(edition: str) -> bool:
    """Does offering in this edition amount to redistributing the asset?

    Cloud, yes: the deployment serves the asset to users who are not its
    operator. Private, no: the person using it installed it.
    """
    return edition == "cloud"


def evaluate_avatar_licence(entry: AvatarEntry, *, edition: str) -> LicenceDecision:
    """Decide whether `entry` may be offered to a user of `edition`.

    Collects EVERY reason rather than returning on the first one. An operator
    fixing a catalogue entry wants the whole list, not one round-trip per
    problem.
    """
    reasons: list[str] = []

    if edition not in ("private", "cloud"):
        # An unrecognised edition is not a reason to guess the laxer rule.
        return LicenceDecision(False, (f"unknown edition {edition!r}",))

    # ── Availability ─────────────────────────────────────────────────────────
    permitted = _AVAILABILITY.get(entry.edition_availability)
    if permitted is None:
        reasons.append(
            f"edition_availability {entry.edition_availability!r} is not a known value"
        )
    elif edition not in permitted:
        reasons.append(
            f"not available in the {edition} edition "
            f"(edition_availability={entry.edition_availability})"
        )

    if not entry.active:
        reasons.append("entry is not active")

    # ── Validation ───────────────────────────────────────────────────────────
    # A model nobody has validated may not be offered. 'not-installed' and
    # 'failed' are both refusals; they are reported distinctly because only one
    # of them means somebody should go and install something.
    if entry.validation_status == "not-installed":
        reasons.append("no model installed (validation_status=not-installed)")
    elif entry.validation_status == "pending":
        reasons.append("validation has not completed (validation_status=pending)")
    elif entry.validation_status == "failed":
        reasons.append("model failed validation")
    elif entry.validation_status != "passed":
        reasons.append(f"unknown validation_status {entry.validation_status!r}")

    # ── Licensing ────────────────────────────────────────────────────────────
    # A procedural avatar is drawn by IRA's own code and has no asset file to
    # license. Requiring a licence record for it would be theatre — but it is
    # still checked that it really is procedural, so the exemption cannot be
    # claimed by setting one field on an entry that ships a GLB.
    is_procedural = (
        entry.renderer == "procedural"
        and not entry.desktop_model_url
        and not entry.mobile_model_url
    )

    if not is_procedural:
        if not entry.licence_reference:
            reasons.append("no licence_reference recorded")

        if _redistribution_required(edition):
            if not entry.licence_verified:
                reasons.append(
                    "licence not verified — a human must confirm the terms "
                    "before the asset is redistributed to cloud users"
                )
            if entry.commercial_use is not True:
                reasons.append(
                    "commercial_use is not confirmed"
                    + (" (unknown)" if entry.commercial_use is None else "")
                )
            if entry.redistribution is not True:
                reasons.append(
                    "redistribution is not confirmed"
                    + (" (unknown)" if entry.redistribution is None else "")
                )
    elif entry.renderer == "procedural" and (
        entry.desktop_model_url or entry.mobile_model_url
    ):  # pragma: no cover - defended by the branch above; kept explicit
        reasons.append("renderer=procedural but a model URL is set")

    return LicenceDecision(not reasons, tuple(reasons))


def evaluate_voice_availability(entry: VoiceEntry, *, edition: str) -> LicenceDecision:
    """Whether a voice may be offered.

    Voices are not a redistribution question in the way avatars are — a voice is
    produced by an engine, not shipped as an asset — so this checks availability
    and runtime readiness rather than licence terms. Offering a voice whose
    engine is not installed is a worse experience than not offering it, and
    (for an external provider) it would also make a privacy promise the
    deployment cannot keep.
    """
    reasons: list[str] = []

    if edition not in ("private", "cloud"):
        return LicenceDecision(False, (f"unknown edition {edition!r}",))

    permitted = _AVAILABILITY.get(entry.edition_availability)
    if permitted is None:
        reasons.append(
            f"edition_availability {entry.edition_availability!r} is not a known value"
        )
    elif edition not in permitted:
        reasons.append(
            f"not available in the {edition} edition "
            f"(edition_availability={entry.edition_availability})"
        )

    if not entry.active:
        reasons.append(
            "voice is not active"
            + (f" (requires {entry.requires_runtime})" if entry.requires_runtime else "")
        )

    return LicenceDecision(not reasons, tuple(reasons))


def is_offerable(entry: AvatarEntry | VoiceEntry, *, edition: str) -> bool:
    """Convenience predicate over the two evaluators above."""
    if isinstance(entry, AvatarEntry):
        return evaluate_avatar_licence(entry, edition=edition).allowed
    return evaluate_voice_availability(entry, edition=edition).allowed
