"""
armory/manifest.py — the tool catalogue and safe command construction.

The manifest is the vault's index of security tools: each entry names an ``id``,
a container ``image`` (kali/tor), a ``cmd`` template, a ``risk`` level, and a
``network`` posture. This module parses/validates a manifest and — critically —
turns a template plus caller-supplied arguments into an **argv list** (never a
shell string) with every argument sanitised.

Why argv, not a shell string: the tool eventually runs inside a container via
``docker run <image> <argv...>``. Building an argv list and refusing any
argument containing shell metacharacters or whitespace removes command-injection
as a class — there is no shell to inject into, and ``{target}`` can never smuggle
``; rm -rf /``. Placeholders resolve one-token-per-placeholder only.

This module performs NO I/O by itself (the vault hands it decrypted bytes) and
imports nothing heavy, so it is fully unit-testable.
"""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

RISK_LEVELS = ("low", "medium", "high", "critical")
NETWORK_MODES = ("none", "tor")
KNOWN_IMAGES = ("kali", "tor")

#: A placeholder in a cmd template, e.g. ``{target}`` or ``{module}``.
_PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")

#: Characters that must never appear in a resolved argument. Even though we exec
#: without a shell, rejecting these keeps arguments to single, well-formed tokens
#: and blocks flag-injection tricks (leading '-' is handled separately).
_UNSAFE_ARG_RE = re.compile(r"""[\s;&|`$<>(){}\[\]!*?~"'\\]""")


class ManifestError(ValueError):
    """Raised when a manifest is malformed or a tool/arguments are invalid."""


@dataclass(frozen=True)
class Tool:
    """One security tool definition."""

    id: str
    category: str
    image: str
    cmd: str
    risk: str
    network: str = "tor"
    description: str = ""

    def placeholders(self) -> list[str]:
        """Ordered, de-duplicated placeholder names in this tool's template."""
        seen: list[str] = []
        for name in _PLACEHOLDER_RE.findall(self.cmd):
            if name not in seen:
                seen.append(name)
        return seen

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)


@dataclass
class Manifest:
    """A validated collection of :class:`Tool` entries, indexed by id."""

    tools: dict[str, Tool] = field(default_factory=dict)

    def get(self, tool_id: str) -> Tool:
        tool = self.tools.get(tool_id)
        if tool is None:
            raise ManifestError(f"unknown tool id: {tool_id!r}")
        return tool

    def ids(self) -> list[str]:
        return sorted(self.tools)

    def __len__(self) -> int:
        return len(self.tools)


def _validate_tool(raw: dict[str, Any]) -> Tool:
    try:
        tool_id = str(raw["id"]).strip()
        image = str(raw["image"]).strip().lower()
        cmd = str(raw["cmd"])
        risk = str(raw["risk"]).strip().lower()
    except KeyError as exc:
        raise ManifestError(f"tool missing required field: {exc}") from exc

    if not re.fullmatch(r"[a-z0-9][a-z0-9_]*", tool_id):
        raise ManifestError(f"invalid tool id {tool_id!r} (use lowercase/underscore)")
    if risk not in RISK_LEVELS:
        raise ManifestError(f"tool {tool_id!r}: unknown risk {risk!r}")
    if image not in KNOWN_IMAGES:
        raise ManifestError(f"tool {tool_id!r}: unknown image {image!r}")
    network = str(raw.get("network", "tor")).strip().lower()
    if network not in NETWORK_MODES:
        raise ManifestError(f"tool {tool_id!r}: unknown network mode {network!r}")
    if not cmd.strip():
        raise ManifestError(f"tool {tool_id!r}: empty cmd")
    # The template itself must tokenise (catches unbalanced quotes at author time).
    try:
        shlex.split(cmd)
    except ValueError as exc:
        raise ManifestError(f"tool {tool_id!r}: cmd does not tokenise: {exc}") from exc

    return Tool(
        id=tool_id,
        category=str(raw.get("category", "misc")).strip().lower(),
        image=image,
        cmd=cmd,
        risk=risk,
        network=network,
        description=str(raw.get("description", "")),
    )


def load_manifest(data: bytes | str | dict[str, Any]) -> Manifest:
    """Parse and validate a manifest from bytes/str JSON or an already-parsed dict.

    Raises :class:`ManifestError` on any structural problem — fail-closed: a bad
    manifest yields no tools, not a partially-trusted one.
    """
    if isinstance(data, (bytes, str)):
        try:
            doc = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ManifestError(f"manifest is not valid JSON: {exc}") from exc
    else:
        doc = data

    if not isinstance(doc, dict) or "tools" not in doc:
        raise ManifestError("manifest must be an object with a 'tools' array")
    raw_tools = doc["tools"]
    if not isinstance(raw_tools, list):
        raise ManifestError("'tools' must be an array")

    tools: dict[str, Tool] = {}
    for raw in raw_tools:
        if not isinstance(raw, dict):
            raise ManifestError("each tool must be an object")
        tool = _validate_tool(raw)
        if tool.id in tools:
            raise ManifestError(f"duplicate tool id: {tool.id!r}")
        tools[tool.id] = tool
    if not tools:
        raise ManifestError("manifest defines no tools")
    return Manifest(tools=tools)


def _sanitise_arg(name: str, value: str) -> str:
    """Validate a single placeholder value, returning it unchanged if safe.

    Fail-closed: rejects empty values, anything with shell metacharacters or
    whitespace, and values that look like an option flag (leading '-') so a
    caller can't turn ``{target}`` into ``--script=...`` argument injection.
    """
    value = (value or "").strip()
    if not value:
        raise ManifestError(f"argument {name!r} is empty")
    if len(value) > 512:
        raise ManifestError(f"argument {name!r} is too long")
    if value.startswith("-"):
        raise ManifestError(f"argument {name!r} may not start with '-' (flag injection)")
    if _UNSAFE_ARG_RE.search(value):
        raise ManifestError(f"argument {name!r} contains disallowed characters")
    return value


def resolve_command(tool: Tool, args: dict[str, str]) -> list[str]:
    """Turn a tool template + args into a sanitised argv list (no shell string).

    Every required placeholder must be supplied; every supplied value is
    sanitised; the template is tokenised and each ``{name}`` token is replaced by
    exactly one validated token. Unknown extra args are rejected (typo safety).
    """
    required = tool.placeholders()
    missing = [p for p in required if p not in args]
    if missing:
        raise ManifestError(f"tool {tool.id!r} missing arguments: {', '.join(missing)}")
    extra = [k for k in args if k not in required]
    if extra:
        raise ManifestError(f"tool {tool.id!r} got unexpected arguments: {', '.join(extra)}")

    clean = {name: _sanitise_arg(name, args[name]) for name in required}

    argv: list[str] = []
    for token in shlex.split(tool.cmd):
        # A token may be exactly a placeholder, or embed one (e.g. ssh://{target}).
        matches = _PLACEHOLDER_RE.findall(token)
        if not matches:
            argv.append(token)
            continue
        resolved = token
        for name in matches:
            resolved = resolved.replace("{" + name + "}", clean[name])
        # The token stays exactly one argv element. Any whitespace here can only
        # come from the (trusted) template — e.g. an msfconsole ``-x "use …; run"``
        # resource string — never from an injected value, which _sanitise_arg
        # guarantees is a single whitespace/metacharacter-free token. Because tools
        # are exec'd without a shell, a multi-word argv element is passed verbatim
        # to the tool and never re-parsed for shell metacharacters.
        argv.append(resolved)
    return argv


#: Placeholder names whose value is a scan/attack TARGET and must be checked
#: against the whitelist. Other placeholders (module, query, wordlists) are not
#: network targets.
TARGET_PLACEHOLDERS = frozenset({"target", "rhost", "rhosts", "host", "url"})


def target_args(tool: Tool, args: dict[str, str]) -> list[str]:
    """Return the subset of args that name a network target (for whitelist checks)."""
    return [args[name] for name in tool.placeholders()
            if name in TARGET_PLACEHOLDERS and name in args]


__all__ = [
    "RISK_LEVELS", "NETWORK_MODES", "KNOWN_IMAGES", "TARGET_PLACEHOLDERS",
    "Tool", "Manifest", "ManifestError",
    "load_manifest", "resolve_command", "target_args",
]
