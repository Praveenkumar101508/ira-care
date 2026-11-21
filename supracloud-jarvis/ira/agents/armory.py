"""
Armory Agent — the conversational front door to the Armory Vault.

Routed to when the owner asks to use security tooling. It never runs a tool
itself; it translates intent and delegates every decision to the
:class:`ArmoryGatekeeper` (armory/gatekeeper.py), which owns the fail-closed
pipeline (unlock → whitelist → egress gate → risk → Shadow World → ephemeral
container → Trust Ledger → auto-lock).

Intents handled:
  * unlock   — "unlock the armory with key <KEY>" → verify + decrypt to RAM
  * lock     — "lock the armory"
  * status   — "is the armory unlocked?" / "armory status"
  * run tool — "scan example.com", "nikto on <host>", "run nmap against <ip>"

Non-owners are refused. Tool output is returned verbatim (not paraphrased by an
LLM) so scan results are never altered. The plaintext master key is parsed out of
the message but never echoed or logged.
"""
from __future__ import annotations

import re
import time

from langchain_core.messages import AIMessage

from agents.state import IRAState
from armory.gatekeeper import LOCKED_MESSAGE, NOT_OWNER, get_gatekeeper
from armory.manifest import Manifest, Tool

# ── intent detection ──────────────────────────────────────────────────────────

# "unlock/open the armory/vault [with] [the] [master] key/password/pass/code <KEY>".
# Anchored on the key-indicator word so a connective like "with" cannot be
# mistaken for the key itself.
# The unlock patterns live in utils/secret_redaction.py so that the store, which
# redacts the key before persisting the message, matches exactly what is read here.
from utils.secret_redaction import UNLOCK_KEY_ONLY_RE as _UNLOCK_KEY_ONLY_RE  # noqa: E402
from utils.secret_redaction import UNLOCK_RE as _UNLOCK_RE  # noqa: E402

_LOCK_RE = re.compile(r"\b(lock|close|seal)\b.*\b(armory|vault)\b", re.I)
_STATUS_RE = re.compile(r"\b(armory|vault)\b.*\b(status|state|locked|unlocked|open)\b", re.I)
_APPROVE_RE = re.compile(r"\b(approve|confirm|authorized?|2fa|proceed anyway|yes run it)\b", re.I)
# A 6-digit TOTP code, optionally introduced by "code"/"2fa"/"otp". Kept narrow so
# it isn't confused with a target; a bare 6-digit run is still matched.
_CODE_RE = re.compile(r"\b(?:code|otp|2fa|token)\s*[:=]?\s*(\d{6})\b|(?<!\d)(\d{6})(?!\d)", re.I)

_URL_RE = re.compile(r"https?://\S+")
_HOST_RE = re.compile(
    r"\b((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}|"      # domain
    r"(?:\d{1,3}\.){3}\d{1,3}|localhost)\b", re.I,
)

# Natural-language phrase → manifest tool id (a starter map; extend freely).
_TOOL_KEYWORDS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bnikto\b", re.I), "nikto_scan"),
    (re.compile(r"\bwhatweb\b|fingerprint", re.I), "whatweb"),
    (re.compile(r"\bsslscan\b|ssl scan|tls scan", re.I), "sslscan"),
    (re.compile(r"\bsqlmap\b|sql injection", re.I), "sqlmap"),
    (re.compile(r"\bgobuster\b|dir(?:ectory)? brute|dirbust", re.I), "gobuster_dir"),
    (re.compile(r"\bwhois\b", re.I), "whois_lookup"),
    (re.compile(r"\bdig\b|dns lookup|dns record", re.I), "dig_any"),
    (re.compile(r"\bdnsrecon\b|dns enum", re.I), "dnsrecon"),
    (re.compile(r"\bwafw00f\b|detect waf|web app firewall", re.I), "wafw00f"),
    (re.compile(r"\bwpscan\b|wordpress", re.I), "wpscan"),
    (re.compile(r"\bmasscan\b", re.I), "masscan"),
    (re.compile(r"quick scan|fast scan", re.I), "nmap_quick"),
    (re.compile(r"\bnmap\b|port scan|scan ports|scan .*(site|host|ip|network|website)|vulnerab",
                re.I), "nmap_scan"),
]


def _extract_master_key(query: str) -> str | None:
    for pattern in (_UNLOCK_RE, _UNLOCK_KEY_ONLY_RE):
        m = pattern.search(query.strip())
        if m:
            return m.group("key").strip().strip("\"'")
    return None


def _extract_target(query: str) -> str | None:
    url = _URL_RE.search(query)
    if url:
        return url.group(0)
    host = _HOST_RE.search(query)
    return host.group(1) if host else None


def _pick_tool(query: str, manifest: Manifest) -> Tool | None:
    for pattern, tool_id in _TOOL_KEYWORDS:
        if pattern.search(query) and tool_id in manifest.tools:
            return manifest.get(tool_id)
    return None


def _reply(state: IRAState, text: str, t0: float) -> IRAState:
    return {
        **state,
        "final_response": text,
        "messages": [AIMessage(content=text)],
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "model_used": "armory_gate",
    }


def _format_result(res: dict) -> str:
    status = res.get("status")
    if status == "executed":
        lock_note = "\n\n🔒 Armory locked automatically after the task." if res.get("locked") else ""
        head = (f"✅ Ran **{res['tool']}** (risk: {res['risk']}, network: {res['network']}, "
                f"exit {res['exit_code']}{', TIMED OUT' if res.get('timed_out') else ''}).")
        return f"{head}\n\n```\n{res.get('output', '').strip()}\n```{lock_note}"
    # Every gate/branch already carries a branded message.
    return res.get("message", "Armory: request could not be completed.")


async def armory_agent(state: IRAState) -> IRAState:
    t0 = time.monotonic()

    if not state.get("is_owner"):
        return _reply(state, NOT_OWNER, t0)

    gk = get_gatekeeper()
    if not gk.enabled:
        from armory.gatekeeper import DISABLED_MESSAGE
        return _reply(state, DISABLED_MESSAGE, t0)
    user_id = state.get("user_id", "owner")
    query = state["user_query"]

    # ── unlock ────────────────────────────────────────────────────────────────
    key = _extract_master_key(query)
    if key is not None:
        res = await gk.unlock(key, user_id=user_id)
        msg = res["message"]
        if res.get("status") == "unlocked":
            msg += (f" {res['tools_available']} tools available. "
                    "Tell me what to run (e.g. \"scan localhost\").")
        return _reply(state, msg, t0)

    # ── lock ──────────────────────────────────────────────────────────────────
    if _LOCK_RE.search(query):
        res = await gk.lock(user_id=user_id, reason="owner_request")
        return _reply(state, res["message"], t0)

    # ── status ────────────────────────────────────────────────────────────────
    if _STATUS_RE.search(query):
        st = gk.status()
        if not st["provisioned"]:
            from armory.gatekeeper import NOT_PROVISIONED
            return _reply(state, NOT_PROVISIONED, t0)
        if st["unlocked"]:
            text = (f"🔓 Armory is UNLOCKED — {st.get('tools_available', 0)} tools, "
                    f"auto-locks in {st['seconds_remaining']}s of inactivity.")
        else:
            text = "🔒 Armory is LOCKED. Provide the Master Key to unlock Security Tools."
        return _reply(state, text, t0)

    # ── run a tool ────────────────────────────────────────────────────────────
    if not gk.session.is_unlocked():
        return _reply(state, LOCKED_MESSAGE, t0)

    manifest = gk.session.contents().manifest
    tool = _pick_tool(query, manifest)
    if tool is None:
        listing = ", ".join(manifest.ids()[:20])
        return _reply(state, ("I couldn't match that to a tool. Available tools: "
                              f"{listing}. Try e.g. \"nmap scan localhost\"."), t0)

    args: dict[str, str] = {}
    placeholders = tool.placeholders()
    if "target" in placeholders or "url" in placeholders:
        target = _extract_target(query)
        if target is None:
            return _reply(state, (f"**{tool.id}** needs a target. Which authorized host "
                                  "should I run it against?"), t0)
        args["target" if "target" in placeholders else "url"] = target
    missing = [p for p in placeholders if p not in args]
    if missing:
        return _reply(state, (f"**{tool.id}** also needs: {', '.join(missing)}. Please "
                              "provide them, then re-issue the request."), t0)

    code_match = _CODE_RE.search(query)
    approval_code = (code_match.group(1) or code_match.group(2)) if code_match else None
    res = await gk.execute(
        tool.id, args,
        user_id=user_id,
        is_owner=True,
        approval_code=approval_code,
        approve_high_risk=bool(_APPROVE_RE.search(query)),
    )
    return _reply(state, _format_result(res), t0)


__all__ = ["armory_agent"]
