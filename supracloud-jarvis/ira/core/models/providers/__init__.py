"""Concrete `ModelProvider` implementations.

Phase 5 built the registry, Phase 6 the licence gate and Phase 7 the training
pipeline, and all three are enforced by `IRAModelGateway`. Three closure reports
in a row then recorded the same limitation in the same words:

    nothing in the product constructs one

There was no `ModelProvider` implementation anywhere in the tree. The gateway
had a protocol and fixtures, and the live path went straight from
`utils/llm.py` to an OpenAI client. This package is the missing half.
"""
from __future__ import annotations

from core.models.providers.openai_compatible import (
    OpenAICompatibleProvider,
    ProviderMisconfigured,
    UnsupportedApprovedModel,
)

__all__ = [
    "OpenAICompatibleProvider",
    "ProviderMisconfigured",
    "UnsupportedApprovedModel",
]
