"""Gmail, Calendar and Drive — read-first connectors behind the gateway.

Three properties shape every method here.

**They read. They do not act.** Gmail can search, read a selected message and
prepare a draft. It cannot send, reply, delete or modify — not because the code
declines to, but because `oauth.OPERATION_SCOPES` never requests a scope that
would permit it, and `oauth.REFUSED_SCOPES` refuses `gmail.send` explicitly. A
build that holds a send scope it never exercises is one compromise away from
using it.

**Everything they return is tainted.** A message body, a subject line, a
document name, an event description — all of it is attacker-controllable text,
and it comes back as `TaintedText` rather than `str`. The gateway then finds it
automatically and the receipt records it. This is the mechanism behind the
Phase 7 headline threat; see `taint.py` and ADR-0006.

**The credential is never returned or logged.** Each connector receives a
`SealedSecret` and reaches the token only inside a `use_secret` callback. There
is no method that hands a token back, which is why
`tests/architecture/test_credential_boundary.py` can make a whole-codebase
claim about it.

── The transport is injected ────────────────────────────────────────────────

`Transport` is a two-method protocol. The real one wraps `httpx`; tests pass a
recording fake. That is not only for testability: it is what lets Phase 7 be
honest about what was verified. The request SHAPES — URLs, query parameters,
headers, and the parsing of Google's documented response bodies — are tested.
No live Google endpoint was reachable from this environment, so the round trip
is unverified, and neither this module nor the phase report claims otherwise.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)

from core.connectors.secrets import SealedSecret, use_secret_async
from core.connectors.taint import Provenance, TaintedText, tainted
from core.identity.principal import Principal
from core.interfaces.connector_gateway import (
    AccessLevel,
    ActionReceipt,
    ConnectorCall,
    ConnectorResult,
)

logger = logging.getLogger("ira.connectors.google")

GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
DRIVE_API = "https://www.googleapis.com/drive/v3"

#: Caps on what one call may pull back. A connector that will happily fetch
#: 10,000 messages because a parameter said so is a data-exfiltration primitive
#: waiting for an injected instruction to name a big number.
MAX_RESULTS: int = 25
MAX_BODY_CHARS: int = 20_000


@runtime_checkable
class Transport(Protocol):
    """The HTTP surface a connector needs. Deliberately two methods."""

    async def get(
        self, url: str, *, token: str, params: Optional[Mapping[str, Any]] = None
    ) -> Mapping[str, Any]:
        ...

    async def post(
        self, url: str, *, token: str, json: Optional[Mapping[str, Any]] = None
    ) -> Mapping[str, Any]:
        ...


class HttpxTransport:
    """The real transport. Bearer auth, short timeout, no redirects followed.

    Redirects are not followed because a redirect on an authenticated request is
    a way to have the Authorization header replayed somewhere else.
    """

    def __init__(self, timeout: float = 15.0) -> None:
        self._timeout = timeout

    async def get(self, url, *, token, params=None):
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as c:
            response = await c.get(
                url, params=dict(params or {}),
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            return response.json()

    async def post(self, url, *, token, json=None):
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as c:
            response = await c.post(
                url, json=dict(json or {}),
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            return response.json()


def _clamp(value: Any, default: int, ceiling: int) -> int:
    """A caller-supplied count, bounded. An injected "give me 10000" is bounded
    to `ceiling` rather than honoured."""
    try:
        wanted = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(wanted, ceiling))


@dataclass
class _GoogleConnector:
    """Shared plumbing: the sealed credential, the transport, and taint.

    `sealed` is `repr`-suppressed so a connector appearing in a traceback does
    not print an encrypted credential alongside it.
    """

    sealed: Optional[SealedSecret] = field(default=None, repr=False)
    transport: Transport = field(default_factory=HttpxTransport)
    #: Binds the credential to its row. Must match what `seal` was given.
    aad: str = ""
    #: Resolves the tenant's data-encryption key for `sealed`, when `sealed` was
    #: sealed under one. Supplied by whoever built the connector, because that
    #: caller has the Principal and this object deliberately does not.
    #:
    #: A callable rather than the key itself, and called on every use rather
    #: than once: a DEK held on a long-lived connector would keep opening
    #: credentials after the key was destroyed, which is exactly the guarantee
    #: `keyring.destroy_keys` exists to provide. See `keyring.dek_for`.
    key_resolver: Optional[Callable[[], Any]] = field(default=None, repr=False)

    async def _with_token(self, fn):
        if self.sealed is None:
            raise RuntimeError(
                "no credential is available for this connector; the gateway "
                "should have refused before reaching here"
            )
        key = None
        if self.key_resolver is not None:
            resolved = self.key_resolver()
            key = await resolved if hasattr(resolved, "__await__") else resolved
        try:
            return await use_secret_async(
                self.sealed, fn, aad=self.aad, key=key
            )
        finally:
            del key

    def _tainted(self, text: str, source: str) -> TaintedText:
        return tainted(text or "", provenance=Provenance.CONNECTOR, source=source)

    async def disconnect(self, principal: Principal) -> None:
        """Drop the in-memory credential.

        Revocation at the provider and deletion from the database are the
        STORE's job (`store.disconnect`), because both must happen even if this
        object was never constructed. Clearing the reference here is the part
        this object owns, and saying so keeps "disconnect" from looking complete
        when only half of it ran.
        """
        self.sealed = None


# ── Gmail ────────────────────────────────────────────────────────────────────


@dataclass
class GmailConnector(_GoogleConnector):
    """Search, read a selected message, prepare a draft. Never send."""

    @property
    def name(self) -> str:
        return "gmail"

    def supported_operations(self) -> Mapping[str, AccessLevel]:
        return {
            "search": AccessLevel.OBSERVE,
            "read_selected": AccessLevel.OBSERVE,
            "prepare_draft": AccessLevel.PREPARE,
        }

    async def execute(self, call: ConnectorCall) -> ConnectorResult:
        handlers = {
            "search": self._search,
            "read_selected": self._read_selected,
            "prepare_draft": self._prepare_draft,
        }
        handler = handlers.get(call.operation)
        if handler is None:
            return ConnectorResult(
                ok=False,
                error=(
                    f"gmail cannot {call.operation!r}. This build reads and "
                    f"drafts; it does not send, reply to or delete anything."
                ),
            )
        return await handler(call)

    async def _search(self, call: ConnectorCall) -> ConnectorResult:
        query = str(call.parameters.get("query", ""))
        limit = _clamp(call.parameters.get("limit"), 10, MAX_RESULTS)

        async def fetch(token: str):
            listed = await self.transport.get(
                f"{GMAIL_API}/users/me/messages",
                token=token, params={"q": query, "maxResults": limit},
            )
            return listed

        listed = await self._with_token(fetch)
        messages = list(listed.get("messages") or [])[:limit]
        return ConnectorResult(
            ok=True,
            # Ids and nothing else. A search result carries snippets, and a
            # snippet is attacker-controlled text that nothing has asked to read
            # yet — the user selects a message and THEN it is read.
            data={"message_ids": [m.get("id") for m in messages if m.get("id")]},
            receipt=ActionReceipt(
                connector=self.name, operation="search", purpose=call.purpose,
                performed_at="",
                # Shape only, per PERMISSION_MODEL.md §7. Never the query, which
                # is itself sensitive ("divorce solicitor").
                data_accessed=(f"{len(messages)} message id(s) from a search",),
            ),
        )

    async def _read_selected(self, call: ConnectorCall) -> ConnectorResult:
        message_id = str(call.parameters.get("message_id", ""))
        if not message_id:
            return ConnectorResult(ok=False, error="read_selected needs a message_id")

        async def fetch(token: str):
            return await self.transport.get(
                f"{GMAIL_API}/users/me/messages/{message_id}",
                token=token, params={"format": "full"},
            )

        message = await self._with_token(fetch)
        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in (message.get("payload", {}).get("headers") or [])
        }
        source = f"gmail:message/{message_id}"
        body = str(message.get("snippet", ""))[:MAX_BODY_CHARS]

        return ConnectorResult(
            ok=True,
            # Every field is TaintedText: a subject line is as attacker-controlled
            # as a body, and "From" is trivially spoofed.
            data={
                "id": message_id,
                "subject": self._tainted(headers.get("subject", ""), source),
                "from": self._tainted(headers.get("from", ""), source),
                "date": headers.get("date", ""),
                "body": self._tainted(body, source),
            },
            receipt=ActionReceipt(
                connector=self.name, operation="read_selected",
                purpose=call.purpose, performed_at="",
                data_accessed=(f"1 message ({source})",),
            ),
        )

    async def _prepare_draft(self, call: ConnectorCall) -> ConnectorResult:
        """Create a Gmail draft. It is NOT sent, and cannot be.

        `gmail.compose` writes to Drafts and has no send capability, so "IRA
        cannot send this" is a property of the credential rather than a promise
        about this function's behaviour.
        """
        import base64
        import email.message

        to = str(call.parameters.get("to", ""))
        subject = str(call.parameters.get("subject", ""))
        raw_body = call.parameters.get("body", "")
        # A body quoted from an email is TaintedText; take its text explicitly so
        # the laundering is visible here rather than implicit in an f-string.
        body = raw_body.text if isinstance(raw_body, TaintedText) else str(raw_body)

        if not to:
            return ConnectorResult(ok=False, error="prepare_draft needs a recipient")

        message = email.message.EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

        async def create(token: str):
            return await self.transport.post(
                f"{GMAIL_API}/users/me/drafts",
                token=token, json={"message": {"raw": encoded}},
            )

        created = await self._with_token(create)
        return ConnectorResult(
            ok=True,
            data={"draft_id": created.get("id", ""), "sent": False},
            receipt=ActionReceipt(
                connector=self.name, operation="prepare_draft",
                purpose=call.purpose, performed_at="",
                data_accessed=("1 draft created in your Drafts folder",),
                # Nothing left the machine for a recipient: a draft is stored at
                # Google, not delivered. Claiming otherwise in either direction
                # would be wrong, so this says what happened.
                sent_externally=(),
                undo_available=True,
            ),
        )


# ── Calendar ─────────────────────────────────────────────────────────────────


@dataclass
class GoogleCalendarConnector(_GoogleConnector):
    """Read events. Cannot create, move or delete anything."""

    @property
    def name(self) -> str:
        return "google_calendar"

    def supported_operations(self) -> Mapping[str, AccessLevel]:
        return {"list_events": AccessLevel.OBSERVE}

    async def execute(self, call: ConnectorCall) -> ConnectorResult:
        if call.operation != "list_events":
            return ConnectorResult(
                ok=False,
                error=(
                    f"google_calendar cannot {call.operation!r}. This build "
                    f"reads the calendar; it does not create, move or delete "
                    f"events."
                ),
            )
        limit = _clamp(call.parameters.get("limit"), 10, MAX_RESULTS)
        params = {
            "maxResults": limit,
            "singleEvents": "true",
            "orderBy": "startTime",
        }
        for key in ("timeMin", "timeMax"):
            if call.parameters.get(key):
                params[key] = str(call.parameters[key])

        async def fetch(token: str):
            return await self.transport.get(
                f"{CALENDAR_API}/calendars/primary/events", token=token, params=params
            )

        listed = await self._with_token(fetch)
        events = []
        for item in list(listed.get("items") or [])[:limit]:
            event_id = item.get("id", "")
            source = f"google_calendar:event/{event_id}"
            events.append({
                "id": event_id,
                # Summaries and descriptions are user- OR attacker-authored: an
                # invitation from outside carries whatever text the sender chose.
                "summary": self._tainted(item.get("summary", ""), source),
                "description": self._tainted(item.get("description", ""), source),
                "start": (item.get("start") or {}).get("dateTime")
                         or (item.get("start") or {}).get("date", ""),
                "end": (item.get("end") or {}).get("dateTime")
                       or (item.get("end") or {}).get("date", ""),
            })
        return ConnectorResult(
            ok=True,
            data={"events": events},
            receipt=ActionReceipt(
                connector=self.name, operation="list_events",
                purpose=call.purpose, performed_at="",
                data_accessed=(f"{len(events)} calendar event(s)",),
            ),
        )


# ── Drive ────────────────────────────────────────────────────────────────────


@dataclass
class GoogleDriveConnector(_GoogleConnector):
    """Search file metadata and read a selected file. Cannot write or delete."""

    @property
    def name(self) -> str:
        return "google_drive"

    def supported_operations(self) -> Mapping[str, AccessLevel]:
        return {"search": AccessLevel.OBSERVE, "read_selected": AccessLevel.OBSERVE}

    async def execute(self, call: ConnectorCall) -> ConnectorResult:
        if call.operation == "search":
            return await self._search(call)
        if call.operation == "read_selected":
            return await self._read_selected(call)
        return ConnectorResult(
            ok=False,
            error=(
                f"google_drive cannot {call.operation!r}. This build reads; it "
                f"does not create, modify or delete files."
            ),
        )

    async def _search(self, call: ConnectorCall) -> ConnectorResult:
        query = str(call.parameters.get("query", ""))
        limit = _clamp(call.parameters.get("limit"), 10, MAX_RESULTS)

        async def fetch(token: str):
            return await self.transport.get(
                f"{DRIVE_API}/files",
                token=token,
                params={
                    "q": f"name contains '{query}'" if query else "",
                    "pageSize": limit,
                    "fields": "files(id,name,mimeType,modifiedTime)",
                },
            )

        listed = await self._with_token(fetch)
        files = []
        for item in list(listed.get("files") or [])[:limit]:
            file_id = item.get("id", "")
            files.append({
                "id": file_id,
                # A filename is attacker-controlled whenever a document was
                # shared with the user.
                "name": self._tainted(item.get("name", ""),
                                      f"google_drive:file/{file_id}"),
                "mime_type": item.get("mimeType", ""),
                "modified": item.get("modifiedTime", ""),
            })
        return ConnectorResult(
            ok=True,
            data={"files": files},
            receipt=ActionReceipt(
                connector=self.name, operation="search",
                purpose=call.purpose, performed_at="",
                data_accessed=(f"metadata for {len(files)} file(s)",),
            ),
        )

    async def _read_selected(self, call: ConnectorCall) -> ConnectorResult:
        file_id = str(call.parameters.get("file_id", ""))
        if not file_id:
            return ConnectorResult(ok=False, error="read_selected needs a file_id")

        async def fetch(token: str):
            return await self.transport.get(
                f"{DRIVE_API}/files/{file_id}/export",
                token=token, params={"mimeType": "text/plain"},
            )

        exported = await self._with_token(fetch)
        source = f"google_drive:file/{file_id}"
        content = str(exported.get("content", ""))[:MAX_BODY_CHARS]
        return ConnectorResult(
            ok=True,
            data={"id": file_id, "content": self._tainted(content, source)},
            receipt=ActionReceipt(
                connector=self.name, operation="read_selected",
                purpose=call.purpose, performed_at="",
                data_accessed=(f"1 document ({source})",),
            ),
        )


async def revoke_at_provider(
    sealed: SealedSecret, *, aad: str = "", timeout: float = 10.0
) -> tuple[bool, str]:
    """POST a credential to Google's revocation endpoint. Returns (ok, error).

    Lives here rather than in the HTTP route, and that placement is the point.
    Revoking needs the token, so whatever performs it must be inside
    `core/connectors/` — the one package the credential boundary permits to
    handle one (`tests/architecture/test_credential_boundary.py`). The first
    draft of Phase 7 called `use_secret` from `api/routes/connectors.py` and the
    boundary test refused it, which is the test working: an HTTP handler is
    exactly where a credential should not be.

    Returns a tuple rather than raising because the caller must report BOTH
    outcomes. A failed revocation is not an error to swallow — it means the grant
    is still live at the provider and the user has to go and remove it.
    """
    from core.connectors.oauth import revocation_request

    async def post_it(token: str):
        url, body = revocation_request(token)
        import httpx

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, data=body)
        return response.status_code < 400, (
            "" if response.status_code < 400
            else f"the provider returned {response.status_code}"
        )

    try:
        return await use_secret_async(sealed, post_it, aad=aad)
    except Exception as exc:  # noqa: BLE001 - reported, never hidden
        logger.warning("provider revocation failed: %s", exc)
        return False, f"{type(exc).__name__}: {exc}"


def all_connectors(
    *, sealed: Optional[SealedSecret] = None,
    transport: Optional[Transport] = None,
    aad: str = "",
    key_resolver: Optional[Callable[[], Any]] = None,
) -> tuple[Any, ...]:
    """Build the three connectors with one credential and transport.

    `key_resolver` is passed through to each. Omit it only for a credential
    sealed under the environment key — a `dek:` credential with no resolver
    refuses at first use rather than silently opening under the wrong key.
    """
    kwargs = {"sealed": sealed, "aad": aad, "key_resolver": key_resolver}
    if transport is not None:
        kwargs["transport"] = transport
    return (
        GmailConnector(**kwargs),
        GoogleCalendarConnector(**kwargs),
        GoogleDriveConnector(**kwargs),
    )


__all__ = [
    "CALENDAR_API",
    "DRIVE_API",
    "GMAIL_API",
    "MAX_BODY_CHARS",
    "MAX_RESULTS",
    "GmailConnector",
    "GoogleCalendarConnector",
    "GoogleDriveConnector",
    "HttpxTransport",
    "Transport",
    "all_connectors",
    "revoke_at_provider",
]
