"""Phase 0 — the shipped .env.example must be safe, honest and well-formed.

`.env.example` is a TRACKED file in a public repository. Every value in it is a
published value, and it is the file users are told to copy to `.env` — so a flag
set here is the flag a real install gets.

Six populated credentials once lived in this file (see
docs/security/CREDENTIAL_ROTATION.md). These tests make that class of regression
fail in CI rather than in a disclosure. They complement, and do not replace, the
Gitleaks `secret-scan` job: Gitleaks catches credential SHAPES anywhere in the
repo, while these tests assert this specific file's contract — named fields
empty, privacy flags off, structure parseable.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# tests/ -> ira/ -> supracloud-jarvis/
ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


def _lines() -> list[str]:
    return ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()


def _assignments() -> dict[str, str]:
    """Parse KEY=VALUE lines, stripping trailing ` # comment` the way dotenv does."""
    out: dict[str, str] = {}
    for raw in _lines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            continue
        value = re.sub(r"\s+#.*$", "", value).strip()
        out[key.strip()] = value
    return out


def test_env_example_exists():
    assert ENV_EXAMPLE.is_file(), f"missing {ENV_EXAMPLE}"


# ── Credentials ──────────────────────────────────────────────────────────────

# The exact six that were exposed, plus the infra passwords that must never be
# populated here. Each MUST be an empty assignment.
MUST_BE_EMPTY = (
    "IRA_SECRET_KEY",
    "IRA_ADMIN_PASSWORD",
    "IRA_CORTEX_KEY",
    "IRA_CANARY_TOKEN",
    "IRA_CANARY_USERNAME",
    "WEBHOOK_SECRET",
    "POSTGRES_PASSWORD",
    "REDIS_PASSWORD",
    "VLLM_API_KEY",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "IRA_VOICE_API_TOKEN",
    "SMTP_PASSWORD",
    "TELEGRAM_BOT_TOKEN",
    "CALCOM_API_KEY",
    "GITHUB_TOKEN",
    "APIFY_API_TOKEN",
    "TWITTER_BEARER_TOKEN",
    "X_FALLBACK_API_KEY",
    "REPLICATE_API_TOKEN",
    "HF_TOKEN",
    "ARMORY_MASTER_KEY_HASH",
)


@pytest.mark.parametrize("key", MUST_BE_EMPTY)
def test_credential_fields_are_empty(key):
    values = _assignments()
    if key not in values:
        pytest.skip(f"{key} is not defined in .env.example")
    assert values[key] == "", (
        f"{key} has a value in .env.example. This file is public — a value here "
        f"is a published credential. Leave it empty and set it in your real .env. "
        f"See docs/security/CREDENTIAL_ROTATION.md."
    )


def test_no_personal_data_fields_populated():
    """Personal name/email/host paths were disclosed here; keep them out."""
    values = _assignments()
    for key in ("OWNER_NAME", "IRA_GIT_AUTHOR_EMAIL", "CODER_ALLOWED_REPOS"):
        if key in values:
            assert values[key] == "", (
                f"{key} carries personal data in the public example file."
            )


def test_no_change_me_placeholders_remain():
    """The master-prompt convention: empty, not CHANGE_ME_*.

    CHANGE_ME_* is also load-bearing elsewhere — setup.sh once substituted on
    those literals and silently no-op'd when they vanished (see R-21). Keeping
    them out of this file keeps that ambiguity out of the installers.
    """
    offenders = [
        f"{n}: {line}"
        for n, line in enumerate(_lines(), 1)
        if "CHANGE_ME" in line
    ]
    assert not offenders, "CHANGE_ME_* placeholder(s) in .env.example:\n" + "\n".join(offenders)


def test_no_high_entropy_values_in_example():
    """Catch a long hex/base64 blob assigned to any key — the shape of the leak."""
    suspicious = []
    for key, value in _assignments().items():
        if re.fullmatch(r"[0-9a-fA-F]{32,}", value) or re.fullmatch(r"[A-Za-z0-9+/=_-]{40,}", value):
            suspicious.append(key)
    assert not suspicious, (
        f"High-entropy value(s) assigned in .env.example: {suspicious}. "
        "That is the shape of a real credential."
    )


# ── Privacy-safe defaults ────────────────────────────────────────────────────

# Every one of these was shipped ON, contradicting both ira/config.py and the
# file's own inline comments, so a documented `cp .env.example .env` install was
# less private than the README promised.
MUST_BE_FALSE = (
    "IRA_ALLOW_EXTERNAL_API",   # master external-egress switch
    "WEB_SEARCH_ENABLED",       # external search
    "DEV_MODE",                 # bypasses auth AND biometrics
    "IRA_BRAIN_ENABLED",        # experimental always-on self-thought stream
    "ARMORY_ENABLED",           # security-tool cabinet
    "ARMORY_ALLOW_UNSANDBOXED", # no-isolation execution backend
    "IRA_CODING_AGENT_ENABLED", # runs Aider on IRA's own source from a chat phrase
    "IRA_ARCHITECT_ENABLED",    # self-modification, and a 12-hour model cycle
    "IRA_WAKEWORD_ENABLED",     # host microphone listening from every boot
)

_FALSEY = {"false", "0", "no", "off", ""}


@pytest.mark.parametrize("key", MUST_BE_FALSE)
def test_unsafe_features_are_off_in_example(key):
    values = _assignments()
    if key not in values:
        return  # absent == inherits the code default, which is off
    assert values[key].strip().lower() in _FALSEY, (
        f"{key}={values[key]!r} in .env.example. This file is what users copy to "
        f".env, so shipping it on makes the documented install path the unsafe one."
    )


def test_example_does_not_contradict_code_default_for_boot_wake_word(monkeypatch):
    """The boot listener is off in code, and the template must not switch it on.

    `voice/wakeword.enabled()` defaults to off. The template shipped
    `IRA_WAKEWORD_ENABLED=true`, and `scripts/setup-native.sh` copies the template
    into every new `.env`, so a documented install asked for the host microphone
    at every boot. It stayed silent only because openwakeword is not installed by
    default; installing it for Wake Mode would have made the microphone listen
    from startup without any action by the owner.
    """
    from voice import wakeword

    monkeypatch.delenv("IRA_WAKEWORD_ENABLED", raising=False)
    assert wakeword.enabled() is False

    example = _assignments().get("IRA_WAKEWORD_ENABLED", "")
    monkeypatch.setenv("IRA_WAKEWORD_ENABLED", example)
    assert wakeword.enabled() is False, (
        f"IRA_WAKEWORD_ENABLED={example!r} in .env.example starts the microphone "
        f"listener at boot on every documented install."
    )


def test_example_does_not_contradict_code_default_for_egress():
    """The example and ira/config.py must agree on the master egress switch."""
    from config import Settings

    example = _assignments().get("IRA_ALLOW_EXTERNAL_API", "").strip().lower()
    assert example in _FALSEY
    assert Settings.model_fields["ira_allow_external_api"].default is False


# ── The browser voice loop ───────────────────────────────────────────────────

VOICE_CONSOLE = ENV_EXAMPLE.parent / "frontend" / "components" / "VoiceConsole.tsx"


def test_browser_voice_loop_stays_on_the_device():
    """The template's voice settings must not send microphone audio off the machine.

    `frontend/components/VoiceConsole.tsx` marks two settings as sending audio to
    the browser vendor, behind its "not private" badge:
    - wake-word activation, which keeps Chrome's Web Speech recognition running
      (`continuous = true`) the whole time voice is armed;
    - Web Speech STT.

    Its own default is on-device: clap and whisper. But an explicit value always
    wins, and `start-ira.sh` exports `.env` before its dev-server fallback compiles
    the frontend. This template shipped `wakeword` until 22 October 2026.
    """
    values = _assignments()
    activation = values.get("NEXT_PUBLIC_VOICE_ACTIVATION", "").strip().lower()
    stt = values.get("NEXT_PUBLIC_VOICE_STT", "").strip().lower()
    assert activation in {"clap", ""}, (
        f"NEXT_PUBLIC_VOICE_ACTIVATION={activation!r} in .env.example. Wake word runs the "
        f"browser vendor's speech recognition continuously; the on-device default is clap."
    )
    assert stt in {"whisper", ""}, (
        f"NEXT_PUBLIC_VOICE_STT={stt!r} in .env.example. Web Speech sends audio to the "
        f"browser vendor; the on-device default is whisper."
    )



# ── Structure ────────────────────────────────────────────────────────────────

def test_no_duplicate_keys():
    """dotenv is last-wins: a duplicated key makes the effective value depend on
    file ordering. IRA_DOMAIN was defined twice."""
    seen: dict[str, int] = {}
    dupes: list[str] = []
    for n, raw in enumerate(_lines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.partition("=")[0].strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if key in seen:
            dupes.append(f"{key} (lines {seen[key]} and {n})")
        else:
            seen[key] = n
    assert not dupes, "Duplicate key(s) in .env.example: " + ", ".join(dupes)


def test_every_line_is_comment_blank_or_assignment():
    """A malformed line (a comment banner missing its '#', or one glued onto a
    value) either breaks parsing or silently corrupts the value next to it."""
    bad = [
        f"{n}: {raw!r}"
        for n, raw in enumerate(_lines(), 1)
        if raw.strip()
        and not raw.lstrip().startswith("#")
        and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", raw)
    ]
    assert not bad, "Malformed line(s) in .env.example:\n" + "\n".join(bad)


# ── An empty value must read as empty under every parser that reads .env ─────
#
# Until 31 October 2026 four lines here were an empty value followed by a
# comment: `WEBHOOK_SECRET=<spaces># openssl rand -hex 32`. python-dotenv before
# 1.2.4 (the lock pinned 1.2.2) and Docker Compose skip the whitespace after '='
# FIRST and only then look for " #", so the '#' had nothing in front of it and
# the comment became the value: the webhooks accepted "# openssl rand -hex 32".
# `_assignments()` above strips "\s+#" while that whitespace is still attached,
# and python-dotenv 1.2.4 (what CI resolves) reads the line as empty, so nothing
# here noticed. The rule below does not depend on any parser's version.

_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _strict_reading(raw_value: str) -> str:
    """An unquoted value as python-dotenv < 1.2.4 and Docker Compose read it.

    python-dotenv 1.2.3 `parser.py`: `_equal_sign = (=[^\\S\\r\\n]*)` consumes the
    whitespace, then `re.sub(r"\\s+#.*", "", value).rstrip()`. compose-go
    `dotenv/parser.go`: `TrimLeftFunc(..., isSpace)`, then `Cut(value, " #")`.
    """
    value = raw_value.lstrip(" \t")
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        return value[1:end] if end > 0 else value[1:]
    return re.sub(r"\s+#.*", "", value).rstrip()


def test_the_strict_reading_is_the_one_that_read_comments_as_values():
    """The helper must reproduce the behaviour it guards against, or the test
    below proves nothing."""
    assert _strict_reading("                          # openssl rand -hex 32") == \
        "# openssl rand -hex 32"
    assert _strict_reading("false                     # true = on") == "false"
    assert _strict_reading("") == ""


def test_no_value_begins_with_a_comment():
    """Put a comment on its own line above the key, never after an empty value."""
    offenders = [
        f"{n}: {raw}"
        for n, raw in enumerate(_lines(), 1)
        if (m := _ASSIGNMENT.match(raw)) and m.group(2).lstrip(" \t").startswith("#")
    ]
    assert not offenders, (
        "Value(s) in .env.example that begin with '#'. python-dotenv before 1.2.4 and "
        "Docker Compose read the comment as the value (a published one, for a "
        "credential). Move the comment onto its own line above the key:\n"
        + "\n".join(offenders)
    )


@pytest.mark.parametrize("key", MUST_BE_EMPTY)
def test_empty_credentials_read_as_empty_under_every_env_parser(key):
    """Each MUST_BE_EMPTY key is empty as the installed python-dotenv reads it
    AND as the older python-dotenv and Docker Compose read it."""
    dotenv = pytest.importorskip("dotenv")
    installed = dotenv.dotenv_values(ENV_EXAMPLE)
    strict = {m.group(1): _strict_reading(m.group(2))
              for raw in _lines() if (m := _ASSIGNMENT.match(raw))}
    if key not in strict:
        pytest.skip(f"{key} is not defined in .env.example")
    assert (installed.get(key) or "") == "", (
        f"python-dotenv reads {key} in .env.example as {installed.get(key)!r}")
    assert strict[key] == "", (
        f"python-dotenv < 1.2.4 and Docker Compose read {key} in .env.example as "
        f"{strict[key]!r}: a published value.")
