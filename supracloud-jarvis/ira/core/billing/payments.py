"""Inbound payment-provider webhooks: verified, recorded once, then acted on.

── The two properties this module exists for ───────────────────────────────

    A WEBHOOK WHOSE SIGNATURE WAS NOT VERIFIED IS AN UNAUTHENTICATED
    REQUEST FROM ANYONE ON THE INTERNET.

    A WEBHOOK PROCESSED TWICE IS A CUSTOMER CHARGED TWICE.

Everything below follows from those two sentences, and neither is decoration
around the feature — they ARE the feature. A billing webhook endpoint with a
missing signature check is a URL that lets a stranger mark their own invoice
paid.

── Why verification is not a boolean on the row ────────────────────────────

`080` has no `signature_valid` column, deliberately. A row exists only if the
signature verified, because verification happens BEFORE the insert and a failed
request is refused with nothing written.

The alternative — store everything, filter later — turns an unauthenticated
request into a row in the billing schema and makes "did we check?" a question
about whether every future reader remembered the filter. One of them will not.

── Why the timestamp matters as much as the signature ──────────────────────

A signature alone proves the bytes came from someone holding the secret. It does
NOT prove they were sent recently. Without a timestamp bound, an attacker who
once observed a valid `payment.succeeded` can replay it forever — every replay
is correctly signed, because it IS the original request.

So the signature covers `timestamp.body`, and a timestamp outside the tolerance
is refused even when the signature is perfect.

── Why intake and action are separate ──────────────────────────────────────

At the moment bytes arrive, nothing has established which tenant they concern.
`080`'s comment header has the full reasoning; the consequence here is that
`record_arrival()` writes down what came in, and `attribute()` decides what it
was about. An event that resolves to no tenant is KEPT, marked `unattributed`,
because a payment nobody can attribute is a thing a human must look at.

── What this module is NOT ─────────────────────────────────────────────────

No real provider is contacted, no key is held, no money moves. The programme
authorisation excludes spending money and signing contracts, so `TestProvider`
is the only implementation and it exists to make the SHAPE testable. A real
provider adds a subclass and a secret; it does not change anything below.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from utils.db import acquire

logger = logging.getLogger("ira.billing.payments")

#: How far out of date a signed request may be, in seconds.
#:
#: A signature proves who sent it, never when. Five minutes is the same
#: tolerance the mainstream providers use: long enough to survive clock skew and
#: a slow retry, short enough that a captured request stops working before
#: anyone can do much with it.
TIMESTAMP_TOLERANCE_SECONDS = 300


class PaymentRefused(RuntimeError):
    """The request was not accepted. Nothing was written."""


class SignatureInvalid(PaymentRefused):
    """The signature did not verify, or was absent.

    A distinct type so a caller cannot accidentally collapse it into a generic
    400 and lose the fact that somebody sent an unauthenticated billing event.
    """


class TimestampOutOfRange(PaymentRefused):
    """Correctly signed, but too old or too far in the future — a replay.

    Separate from `SignatureInvalid` because they mean different things about
    the sender: one failed to hold the secret, the other holds a request that
    was valid once.
    """


@dataclass(frozen=True, slots=True)
class PaymentEvent:
    """What a provider said, after the envelope has been verified."""
    provider: str
    event_id: str
    event_type: str
    customer_ref: str
    payload: dict[str, Any]


class PaymentProvider(Protocol):
    """What any payment provider must supply to be usable here.

    Deliberately tiny. Everything that varies between providers — the header
    names, the signing scheme, where the customer reference sits in their JSON —
    lives behind this, and everything that must NOT vary (verify before write,
    write exactly once, attribute separately) lives in this module.
    """

    name: str

    def verify(self, *, body: bytes, headers: dict[str, str]) -> PaymentEvent:
        """Verify the envelope and return what it said.

        MUST raise `SignatureInvalid` or `TimestampOutOfRange` rather than
        returning anything when verification fails. A provider that returned a
        `PaymentEvent` with a `verified=False` flag would make the caller
        responsible for a check this interface exists to guarantee.
        """
        ...


class TestProvider:
    """A provider with no network, no account and no money.

    Signs the way the mainstream ones do — HMAC-SHA256 over
    `"{timestamp}.{body}"` — so the SHAPE under test is the shape a real
    provider needs, not a simplification that would quietly stop being true when
    one is added.
    """

    name = "test"

    def __init__(self, secret: str):
        if not secret:
            # Refusing beats defaulting. A provider constructed with an empty
            # secret would verify every request against "", i.e. accept
            # anything shaped correctly, while looking configured.
            raise ValueError(
                "a payment provider cannot be constructed without a signing "
                "secret: it would accept any correctly-shaped request")
        self._secret = secret.encode()

    def sign(self, *, body: bytes, timestamp: int) -> str:
        """Produce a signature. Used by tests and by nothing in the request path."""
        return hmac.new(
            self._secret, f"{timestamp}.".encode() + body, hashlib.sha256
        ).hexdigest()

    def verify(self, *, body: bytes, headers: dict[str, str]) -> PaymentEvent:
        lowered = {k.lower(): v for k, v in headers.items()}
        raw_ts = lowered.get("x-payment-timestamp", "")
        supplied = lowered.get("x-payment-signature", "")

        try:
            timestamp = int(raw_ts)
        except (TypeError, ValueError):
            # Before the signature check, because there is nothing to check a
            # signature over without a timestamp — it is part of the signed
            # material, not a field beside it.
            raise SignatureInvalid(
                "the request carries no usable X-Payment-Timestamp, so there "
                "is no signed material to verify") from None

        expected = self.sign(body=body, timestamp=timestamp)
        # compare_digest, never ==. String equality returns as soon as two bytes
        # differ, and the time it took says how much of the prefix was right —
        # enough, over many attempts, to recover a signature a byte at a time.
        if not hmac.compare_digest(expected, supplied):
            raise SignatureInvalid(
                "the signature does not verify. This request was not sent by "
                "anyone holding the signing secret.")

        # AFTER the signature, on purpose: an unsigned request should not be
        # able to learn anything about our clock or tolerance by varying its
        # timestamp and watching which error comes back.
        drift = abs(int(time.time()) - timestamp)
        if drift > TIMESTAMP_TOLERANCE_SECONDS:
            raise TimestampOutOfRange(
                f"the request is {drift}s out of date (tolerance "
                f"{TIMESTAMP_TOLERANCE_SECONDS}s). A correctly signed request "
                f"that is old is a REPLAY: the signature proves who sent it, "
                f"never when.")

        try:
            parsed = json.loads(body)
            if not isinstance(parsed, dict):
                raise ValueError("not an object")
        except (ValueError, UnicodeDecodeError) as exc:
            raise PaymentRefused(f"the signed body is not a JSON object: {exc}") from exc

        event_id = str(parsed.get("id") or "")
        event_type = str(parsed.get("type") or "")
        if not event_id or not event_type:
            # Without an id there is no idempotency key, and an event that
            # cannot be de-duplicated is one that may be applied twice.
            raise PaymentRefused(
                "the event carries no id or no type, so it cannot be recorded "
                "exactly once")

        return PaymentEvent(
            provider=self.name,
            event_id=event_id,
            event_type=event_type,
            customer_ref=str(parsed.get("customer") or ""),
            payload=parsed,
        )


async def record_arrival(event: PaymentEvent) -> tuple[uuid.UUID, bool]:
    """Write the event down exactly once. Returns `(id, was_new)`.

    `was_new=False` means this delivery is a REPEAT — the provider retried, or
    somebody replayed within the tolerance window. The caller must not act on it
    again; that is the whole point.

    Uniqueness is left to `080`'s constraint rather than a SELECT-then-INSERT,
    because two deliveries arriving together would both see "not present".

    Runs on an UNBOUND connection, which is legitimate here and nowhere near
    the tenant-scoped tables: `payment_events` carries no RLS precisely because
    nothing has established a tenant yet (`080`'s header has the reasoning).
    Note what this is NOT — `owner_connection()` binds the single PRIVATE tenant
    and refuses outright in cloud, so using it here would have read one tenant's
    subscriptions in dev and thrown in the only edition that bills anybody.
    """
    async with acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO payment_events (provider, provider_event_id, "
            "  event_type, customer_ref, payload) "
            "VALUES ($1,$2,$3,$4,$5::jsonb) "
            "ON CONFLICT (provider, provider_event_id) DO NOTHING "
            "RETURNING id",
            event.provider, event.event_id, event.event_type,
            event.customer_ref, json.dumps(event.payload),
        )
        if row is not None:
            return row["id"], True

        existing = await conn.fetchval(
            "SELECT id FROM payment_events "
            " WHERE provider = $1 AND provider_event_id = $2",
            event.provider, event.event_id)

    logger.info(
        "payment event %s/%s was already recorded; not acting on it again",
        event.provider, event.event_id)
    return existing, False


async def attribute(event_row_id: uuid.UUID, customer_ref: str) -> Optional[uuid.UUID]:
    """Resolve the provider's customer reference to a tenant, and record which.

    Returns the tenant, or `None` when nothing matched — in which case the row
    is marked `unattributed` and KEPT. Deleting it would destroy the only record
    that a payment arrived for somebody we cannot identify.
    """
    async with acquire() as conn:
        # `tenants` — NOT a tenant-scoped table, and NOT a second copy of the
        # mapping. `tenant_subscriptions` is FORCE RLS, and an unbound read of
        # it does not error: it MATCHES NOTHING. Every payment would come back
        # unattributable, silently, and an earlier version of this module did
        # exactly that.
        #
        # The tenancy ROOT is readable before scope exists — that is what makes
        # it the root — which is precisely what attribution needs and precisely
        # why the mapping lives there rather than in a directory table of its
        # own. One copy cannot disagree with another.
        #
        # The empty string is excluded explicitly: it is the column DEFAULT on
        # every tenant with no provider reference, so matching it would
        # attribute a payment to an arbitrary organisation.
        tenant = None
        if customer_ref:
            tenant = await conn.fetchval(
                "SELECT id FROM tenants WHERE provider_customer_ref = $1",
                customer_ref)

        if tenant is None:
            await conn.execute(
                "UPDATE payment_events SET outcome = 'unattributed', "
                "  processed_at = NOW(), note = $2 WHERE id = $1",
                event_row_id,
                "no tenant carries this provider customer reference; kept for "
                "a person to look at rather than discarded")
            logger.warning(
                "payment event %s could not be attributed to a tenant", event_row_id)
            return None

    # Nothing further is written to the event. Which tenant it concerned is
    # recoverable from `customer_ref` whenever it is asked for, and a stored
    # copy would be a second fact that can disagree with the first.
    return tenant

__all__ = [
    "PaymentEvent", "PaymentProvider", "PaymentRefused", "SignatureInvalid",
    "TestProvider", "TimestampOutOfRange", "TIMESTAMP_TOLERANCE_SECONDS",
    "attribute", "record_arrival",
]
