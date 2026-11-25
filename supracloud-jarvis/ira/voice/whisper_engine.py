"""Import faster-whisper without PyAV.

faster-whisper's package ``__init__`` and its transcriber import ``faster_whisper.audio``,
whose first lines import PyAV (``av``) for its file decoder. PyAV's wheels bundle FFmpeg
built with the GPL encoders libx264 and libx265, and the owner decided that it does not ship
(docs/security/LICENCES.md). IRA installs faster-whisper with ``--no-deps``
(requirements-nodeps.txt), so ``av`` is absent and a plain ``import faster_whisper`` fails.

``load_faster_whisper`` registers ``voice.audio_decode`` as ``faster_whisper.audio`` before
the package is imported. The transcriber then finds the two names it uses from that module,
``decode_audio`` and ``pad_or_trim``, in IRA's own decoder, and PyAV is never looked for.
IRA always passes the recogniser decoded float32 samples, so ``decode_audio`` is reached only
if a caller hands it a file, and then it reads WAV or refuses with ``UnsupportedAudio``.

Every product import of faster-whisper goes through here
(tests/architecture/test_no_pyav.py). The names are checked against the installed
faster-whisper's own source, so a version that imports something else from its audio module
fails that test rather than the first transcription.
"""

from __future__ import annotations

import sys
import threading

#: The faster-whisper releases this substitution was checked against. Another release must be
#: read before it is allowed: it may import more from its audio module, or move PyAV elsewhere.
SUPPORTED_VERSIONS = frozenset({"1.2.1"})

#: What faster-whisper 1.2.1 imports from ``faster_whisper.audio``.
AUDIO_NAMES = ("decode_audio", "pad_or_trim")

_lock = threading.Lock()


def load_faster_whisper():
    """The ``faster_whisper`` package, imported with IRA's decoder in place of PyAV's.

    Raises ImportError when faster-whisper is not installed. If faster-whisper was already
    imported in this process by something else, that import stands, whatever it loaded.
    """
    with _lock:
        placed = False
        if "faster_whisper" not in sys.modules:
            from voice import audio_decode
            placed = sys.modules.setdefault("faster_whisper.audio", audio_decode) is audio_decode
        try:
            import faster_whisper
        except BaseException:
            if placed:
                sys.modules.pop("faster_whisper.audio", None)
            raise
        if getattr(faster_whisper, "audio", None) is None:
            faster_whisper.audio = sys.modules["faster_whisper.audio"]
    return faster_whisper
