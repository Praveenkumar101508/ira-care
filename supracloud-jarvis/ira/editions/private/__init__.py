"""IRA Private — the local-first, single-owner edition.

This is today's shipping product. Phase 1 changes none of its behaviour: the
existing modules (`router`, `agents/`, `actions/`, `memory/`, `utils/llm`) are
untouched and still serve every request exactly as before.

What this package adds is the DECLARATION of what Private is composed of, so the
architecture tests can assert the boundary, and so a later phase has an obvious
place to move implementations into.

Private-only capabilities — the ones ADR-0001 keeps out of the cloud edition's
import graph entirely:
"""
from __future__ import annotations

from core.editions import EDITION_PRIVATE, EditionName

EDITION: EditionName = EDITION_PRIVATE

#: Module prefixes that are Private-only. The architecture test uses this list
#: as the definition of "private-only executor" — so adding a capability here is
#: how you put it out of cloud's reach, and the test enforces it thereafter.
#:
#: Each entry is a real capability that must never be reachable from a
#: multi-user consumer service:
PRIVATE_ONLY_MODULES: tuple[str, ...] = (
    "armory",          # encrypted security-tool cabinet (Kali toolchain, nmap, …)
    "coding",          # Forge: engineering tasks that edit a real checkout
    "cognition",       # NeuroKernel — experimental, self-modifying
    "subagents",       # architect: applies changes to IRA's own source
    "commands",        # host shell/OS control
)

#: Capabilities excluded from the first cloud release by the transformation
#: brief. Recorded here so the exclusion is greppable from code, not only prose.
CONSUMER_CLOUD_EXCLUSIONS: tuple[str, ...] = (
    "armory_vault",
    "penetration_testing_tools",
    "unrestricted_computer_control",
    "neurokernel",
    "self_modification",
    "full_forge",
    "meeting_recording",
    "anticipation_loop",
    "financial_transactions",
    "social_media_publishing",
    "permanent_autonomous_deletion",
    "custom_face_cloning",
    "custom_voice_cloning",
    "always_on_hidden_microphone",
)


__all__ = ["EDITION", "PRIVATE_ONLY_MODULES", "CONSUMER_CLOUD_EXCLUSIONS"]
