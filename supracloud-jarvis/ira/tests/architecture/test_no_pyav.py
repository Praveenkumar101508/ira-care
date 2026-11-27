"""PyAV (`av`) does not ship, and nothing in the product imports it.

PyAV's wheels bundle FFmpeg built with the GPL encoders libx264 and libx265. faster-whisper
declares PyAV as a hard dependency and imports it at the top of ``faster_whisper.audio``.
The owner decided PyAV does not ship (docs/security/LICENCES.md), so:

* no manifest and no lock may name ``av``, and faster-whisper may appear only in
  requirements-nodeps.txt (installed with ``--no-deps``) and the lock;
* every install of that file uses ``--no-deps``, and the images fail their build if ``av``
  is present anyway;
* no Python file in the product or its tests imports ``av``, and only
  ``voice/whisper_engine.py`` imports ``faster_whisper``, so the substitution of IRA's own
  decoder (voice/audio_decode.py) cannot be bypassed by an import elsewhere;
* with ``av`` made unimportable, ``voice.stt`` still loads faster-whisper and never asks for
  ``av``. The control beside it shows the blocker is real: a plain ``import faster_whisper``
  fails under it.

AST-based, so a mention in a comment or a string is not an import.
"""
from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

IRA_ROOT = Path(__file__).resolve().parents[2]
REPO = IRA_ROOT.parents[1]

MANIFESTS = ("requirements.txt", "requirements-test.txt", "requirements-nodeps.txt",
             "requirements-api.txt", "requirements-api.lock",
             "requirements.lock", "requirements-torch.txt", "requirements-torch.lock")
#: The only module allowed to import faster_whisper.
ENGINE = IRA_ROOT / "voice" / "whisper_engine.py"
#: Python trees that ship or test what ships.
PY_TREES = (IRA_ROOT, REPO / "scripts")
_SKIP_DIRS = {".venv", "venv", "node_modules", "__pycache__", ".git", "future-scale"}


def _names(path: Path) -> dict[str, str]:
    """``{normalised name: line}`` for every requirement line in a manifest or lock."""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if m and not line.lstrip().startswith(("#", "-")):
            out[re.sub(r"[-_.]+", "-", m.group(1)).lower()] = line
    return out


@pytest.mark.parametrize("manifest", MANIFESTS)
def test_no_manifest_or_lock_names_av(manifest):
    path = IRA_ROOT / manifest
    if not path.exists():
        pytest.skip(f"{manifest} is not in this tree")
    assert "av" not in _names(path), f"{manifest} names PyAV: {_names(path)['av']!r}"


def test_control_the_manifest_reader_finds_av_when_it_is_there(tmp_path):
    lock = tmp_path / "requirements.lock"
    lock.write_text("attrs==26.1.0 \\\n    --hash=sha256:aa\nav==18.1.0 \\\n    --hash=sha256:bb\n")
    assert "av" in _names(lock)


def test_faster_whisper_is_installed_only_without_its_dependencies():
    """Listed in a resolved manifest, pip would pull PyAV back in."""
    for manifest in ("requirements.txt", "requirements-test.txt"):
        assert "faster-whisper" not in _names(IRA_ROOT / manifest), manifest
    assert "faster-whisper" in _names(IRA_ROOT / "requirements-nodeps.txt")
    assert "faster-whisper" in _names(IRA_ROOT / "requirements.lock")


#: Every place that installs the Python requirements, and the file it must also install with
#: --no-deps. A new installer that forgets it installs no recogniser, which the STT tests
#: (and /voice/transcribe's 503) show; one that installs faster-whisper with its
#: dependencies brings PyAV back, which the image check below catches.
INSTALLERS = (
    REPO / "deploy" / "Dockerfile.api",
    IRA_ROOT / "Dockerfile.portable",
    REPO / ".github" / "workflows" / "test.yml",
    REPO / "supracloud-jarvis" / "scripts" / "setup-native.sh",
    REPO / "supracloud-jarvis" / "scripts" / "setup-windows.ps1",
    REPO / "scripts" / "staging" / "clean_clone_proof.sh",
)


@pytest.mark.parametrize("path", INSTALLERS, ids=lambda p: p.name)
def test_every_installer_adds_faster_whisper_with_no_deps(path):
    text = path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if "requirements-nodeps.txt" in ln and "install" in ln]
    assert lines, f"{path.name} does not install requirements-nodeps.txt"
    assert all("--no-deps" in ln for ln in lines), lines


@pytest.mark.parametrize("path", [REPO / "deploy" / "Dockerfile.api", IRA_ROOT / "Dockerfile.portable"],
                         ids=lambda p: p.name)
def test_the_images_refuse_to_build_with_av_present(path):
    assert "find_spec('av')" in path.read_text(encoding="utf-8")


def _python_files():
    for tree in PY_TREES:
        for path in tree.rglob("*.py"):
            if _SKIP_DIRS.intersection(path.parts):
                continue
            yield path


def _imports(path: Path) -> list[tuple[str, int]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(a.name, node.lineno) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.module, node.lineno))
    return found


def test_nothing_imports_av():
    offenders = [f"{p.relative_to(REPO)}:{line}" for p in _python_files()
                 for name, line in _imports(p) if name == "av" or name.startswith("av.")]
    assert not offenders, offenders


def test_only_the_engine_module_imports_faster_whisper():
    """Anything else importing it first would load faster-whisper's own audio module, and
    with it PyAV, before the engine module could put IRA's decoder in its place."""
    offenders = [f"{p.relative_to(REPO)}:{line}" for p in _python_files() if p != ENGINE
                 for name, line in _imports(p)
                 if name == "faster_whisper" or name.startswith("faster_whisper.")]
    assert not offenders, offenders


def test_control_the_import_scan_sees_imports_not_mentions(tmp_path):
    f = tmp_path / "m.py"
    f.write_text('"""import av"""\n# import av\nx = "from av import open"\nfrom av.audio import x\n')
    assert _imports(f) == [("av.audio", 4)]


_BLOCK_AV = textwrap.dedent("""
    import sys
    class _NoAV:
        def find_spec(self, name, path=None, target=None):
            if name == "av" or name.startswith("av."):
                raise ImportError("av is not installed (blocked by the test)")
    sys.meta_path.insert(0, _NoAV())
""")


def _faster_whisper_or_skip() -> None:
    """Skip when faster-whisper is not installed, found without importing it.

    `pytest.importorskip("faster_whisper")` would run `import faster_whisper`, which asks for
    PyAV: the very import these tests show the product never makes. Where `av` is absent, as
    it is in CI and in every honest install, that gate skipped the three tests that prove the
    exclusion works. Whether the distribution is present is what decides here."""
    if importlib.util.find_spec("faster_whisper") is None:
        pytest.skip("faster-whisper is not installed")


def _run(code: str) -> subprocess.CompletedProcess:
    env = {"IRA_SECRET_KEY": "test-placeholder", "PATH": "/usr/bin:/bin", "HF_HUB_OFFLINE": "1"}
    return subprocess.run([sys.executable, "-c", _BLOCK_AV + textwrap.dedent(code)],
                          cwd=IRA_ROOT, env=env, capture_output=True, text=True, timeout=120)


def test_the_recogniser_loads_without_av():
    _faster_whisper_or_skip()
    r = _run("""
        import voice.stt as stt
        import faster_whisper.transcribe as t
        assert stt.WhisperModel is not None, "faster-whisper did not load"
        assert "av" not in sys.modules
        assert t.decode_audio.__module__ == "voice.audio_decode"
        print("ok")
    """)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr[-2000:]


def test_control_without_the_engine_module_faster_whisper_needs_av():
    """Shows the blocker above is effective: the plain import asks for PyAV and fails."""
    _faster_whisper_or_skip()
    r = _run("""
        try:
            import faster_whisper
        except ImportError as e:
            print("refused:", e)
        else:
            print("imported")
    """)
    assert r.stdout.startswith("refused:") and "av" in r.stdout, r.stdout + r.stderr[-2000:]


def test_the_substitute_covers_what_faster_whisper_imports_from_its_audio_module():
    """Read from the installed faster-whisper's source, so an upgrade that imports something
    new from its audio module fails here, not at the first transcription."""
    _faster_whisper_or_skip()
    import importlib.metadata as md

    from voice import audio_decode, whisper_engine

    version = md.version("faster-whisper")
    assert version in whisper_engine.SUPPORTED_VERSIONS, (
        f"faster-whisper {version} has not been checked against voice/whisper_engine.py")
    pkg = Path(importlib.util.find_spec("faster_whisper").origin).parent
    wanted: set[str] = set()
    for path in pkg.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (
                    node.module == "faster_whisper.audio" or (node.level and node.module == "audio")):
                wanted |= {a.name for a in node.names}
            if isinstance(node, ast.Import):
                assert not any(a.name == "av" for a in node.names) or path.name == "audio.py", (
                    f"faster-whisper {version} imports av in {path.name}, not only in audio.py")
    assert wanted == set(whisper_engine.AUDIO_NAMES), wanted
    assert all(callable(getattr(audio_decode, n)) for n in wanted)
