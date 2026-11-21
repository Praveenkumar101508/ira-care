"""Switch off the usage reporting that third-party libraries send by default.

IRA's promise is that nothing the owner says has to leave the machine. Some
libraries IRA loads report usage on their own, from native code that IRA's
egress controls never see:

* **ONNX Runtime.** faster-whisper loads it for its voice-activity filter on
  the first transcription, and the wake-word availability check imports it:
  at boot when the boot listener is on, and when the Voice Setup page polls
  the wake status. The official Linux wheels (seen in 1.29.0, 1.31.0, which
  requirements.lock pins, and 1.31.1, which an unpinned install resolved)
  connect to Microsoft's 1DS collector, ``mobile.events.data.microsoft.com``,
  as soon as the library is imported; ONNX Runtime's privacy notes say its
  macOS builds use the same collector. ``ORT_DISABLE_TELEMETRY=1`` stops it,
  but only when it is in the process environment before the library
  initialises. Calling ``onnxruntime.disable_telemetry_events()`` after the
  import is too late: the import has already connected. Observed on
  1 November 2026:
  supracloud-jarvis/docs/testing/evidence/onnxruntime-telemetry-off-2026-11-01/.

Native code reads the C environment, which ``os.environ`` writes through to,
so the switch has to be set before the first ``import onnxruntime`` anywhere in
the process. The API (``main.py``) and the worker (``worker/main.py``) call
:func:`switch_off_vendor_telemetry` before they import anything else, and
``voice/stt.py`` and ``voice/wakeword.py``, the two modules that lead to an
onnxruntime import, call it as well for any other process that loads them.
``start-ira.sh`` and ``start-ira.ps1`` export the same value, so every child
process inherits it.

Importing this module loads only the standard library (``core/__init__.py``
pulls in ``core/editions.py``, which is stdlib-only too), so it can run first.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping

#: Each switch and the value that turns the reporting off. A switch is set only
#: when it is absent or blank, so an owner's explicit value is kept.
SWITCHES: dict[str, str] = {
    "ORT_DISABLE_TELEMETRY": "1",
}


def switch_off_vendor_telemetry(environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Set every switch that is absent or blank; return the names it set.

    Blank counts as absent because ``start-ira.sh`` exports a ``.env`` line
    such as ``ORT_DISABLE_TELEMETRY=`` as an empty value, and ONNX Runtime
    reads an empty value as "telemetry on".
    """
    env = os.environ if environ is None else environ
    changed: list[str] = []
    for name, value in SWITCHES.items():
        if not env.get(name, "").strip():
            env[name] = value
            changed.append(name)
    return changed
