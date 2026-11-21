"""IRA NeuroKernel: bounded, owner-controlled cognitive orchestration.

The package is deliberately independent from the continuous conversational
brain.  It coordinates perception, attention, goals, predictions and action
planning, but never bypasses IRA's existing approval and execution gates.
"""

from cognition.runtime import NeuroKernel, get_kernel, reset_kernel

__all__ = ["NeuroKernel", "get_kernel", "reset_kernel"]
