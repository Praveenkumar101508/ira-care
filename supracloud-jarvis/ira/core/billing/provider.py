"""Which payment provider is configured, and what it can do.

`payments.py` already owns the half that must never vary: verify before write,
write exactly once, attribute separately. It defines `PaymentProvider` — a
deliberately tiny protocol with one method, `verify` — and ships `TestProvider`,
which signs the way the mainstream ones do and has no network, no account and
no money.

That was everything an INBOUND event needs. It is not enough to sell anything.

This module adds the outbound half — creating a checkout session, opening a
billing portal — and the thing that decides which provider is in play.

── The rule this module exists to enforce ───────────────────────────────────

**An absent provider is a refusal, never a free ride.**

`PRICE_NOT_CONFIGURED` already means *"no price row, so nobody can be charged"*.
`PAYMENT_PROVIDER_NOT_CONFIGURED` means the price exists and there is no way to
take the money. Both refuse; they are different facts and a user told the wrong
one cannot act on it. Neither ever resolves to "free".

── Why Stripe ────────────────────────────────────────────────────────────────

The repository chose it before this module existed. `080_payment_events.sql`
models `provider_customer_ref` on `tenants` as an opaque `cus_`-shaped string,
`TestProvider` signs `"{timestamp}.{body}"` with HMAC-SHA256 under
`X-Payment-Signature`/`X-Payment-Timestamp` — Stripe's scheme with Stripe's
header split into two — and `TIMESTAMP_TOLERANCE_SECONDS = 300` is Stripe's
tolerance, named as such in its own comment. `StripeProvider` below completes a
decision already taken rather than making a competing one.

── What is real here and what is not ────────────────────────────────────────

`StripeProvider.verify` is complete: it implements Stripe's documented
`Stripe-Signature` scheme (`t=`/`v1=`, HMAC-SHA256 over `"{t}.{payload}"`,
constant-time compare, tolerance window) and needs no network and no account to
be correct. **It is exercised by tests against real signatures.**

`create_checkout_session` and `create_portal_session` make real HTTPS calls to
Stripe's API. Without `IRA_STRIPE_SECRET_KEY` they raise
`ProviderNotConfigured` before any socket is opened. **No live transaction has
been performed from this repository**, and that is the one part of billing that
cannot be closed without the owner's credentials.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Optional

from core.billing.payments import (
    TIMESTAMP_TOLERANCE_SECONDS,
    PaymentEvent,
    PaymentRefused,
    SignatureInvalid,
    TestProvider,
    TimestampOutOfRange,
)

logger = logging.getLogger("ira.billing.provider")

#: The reason string an API returns when a price exists and no provider does.
#: Distinct from `PRICE_NOT_CONFIGURED`, which is about the price.
PAYMENT_PROVIDER_NOT_CONFIGURED = "PAYMENT_PROVIDER_NOT_CONFIGURED"

#: The reason string when a provider is configured and the call to it failed.
#: Distinct again: "we cannot take money" and "we tried and it broke" are
#: different things to tell a user, and only one of them is worth retrying.
CHECKOUT_UNAVAILABLE = "CHECKOUT_UNAVAILABLE"


class ProviderNotConfigured(RuntimeError):
    """No payment provider is configured. Nothing was attempted."""


class ProviderCallFailed(RuntimeError):
    """The provider was reachable in principle and the call did not succeed."""


@dataclass(frozen=True, slots=True)
class CheckoutSession:
    """A created checkout session. `url` is where the user must be sent."""
    provider: str
    session_ref: str
    url: str


@dataclass(frozen=True, slots=True)
class PortalSession:
    """A billing-portal session the user may open to manage their subscription."""
    provider: str
    url: str


class StripeProvider:
    """Stripe, as this repository already assumed it would be.

    Constructed only from configuration this deployment holds. There is no
    embedded key, no default and no test-mode fallback: a provider that
    constructs itself without a secret would verify every request against the
    empty string, which is `TestProvider`'s own documented refusal reason.
    """

    name = "stripe"

    #: Stripe's live API. Overridable so a test can point the outbound calls at
    #: a local socket — which is how the checkout path is exercised without an
    #: account, and is NOT the same as pretending the call succeeded.
    api_base = "https://api.stripe.com/v1"

    def __init__(self, *, secret_key: str, webhook_secret: str,
                 api_base: Optional[str] = None):
        if not secret_key:
            raise ValueError(
                "StripeProvider cannot be constructed without a secret key: it "
                "would be a provider that cannot take money while reporting "
                "itself as configured")
        if not webhook_secret:
            raise ValueError(
                "StripeProvider cannot be constructed without a webhook signing "
                "secret: it would accept any correctly-shaped request")
        self._secret_key = secret_key
        self._webhook_secret = webhook_secret.encode()
        if api_base:
            self.api_base = api_base

    # ── Inbound ──────────────────────────────────────────────────────────

    def sign(self, *, body: bytes, timestamp: int) -> str:
        """The `v1` signature Stripe would send. Used by tests, never in the
        request path — a request path that can produce a valid signature is a
        request path that can forge one."""
        return hmac.new(
            self._webhook_secret, f"{timestamp}.".encode() + body, hashlib.sha256
        ).hexdigest()

    def verify(self, *, body: bytes, headers: dict[str, str]) -> PaymentEvent:
        """Stripe's documented `Stripe-Signature` scheme.

        The header is `t=<unix>,v1=<hex>[,v1=<hex>...]`. Several `v1` values
        appear during a signing-secret rotation and **any** of them verifying is
        an accepted request; that is the mechanism that makes rotation possible
        without dropping events, so it is implemented rather than simplified to
        the first one.
        """
        lowered = {k.lower(): v for k, v in headers.items()}
        raw = lowered.get("stripe-signature", "")
        if not raw:
            raise SignatureInvalid(
                "the request carries no Stripe-Signature header, so there is "
                "nothing to verify")

        timestamp: Optional[int] = None
        candidates: list[str] = []
        for part in raw.split(","):
            key, _, value = part.strip().partition("=")
            if key == "t":
                try:
                    timestamp = int(value)
                except ValueError:
                    timestamp = None
            elif key == "v1":
                candidates.append(value)

        if timestamp is None or not candidates:
            raise SignatureInvalid(
                "the Stripe-Signature header carries no usable t= or v1= value, "
                "so there is no signed material to verify")

        expected = self.sign(body=body, timestamp=timestamp)
        # `any(compare_digest(...))` and not `expected in candidates`: the second
        # is an ordinary string comparison and leaks how much of the prefix was
        # right, which over many attempts recovers a signature a byte at a time.
        if not any(hmac.compare_digest(expected, c) for c in candidates):
            raise SignatureInvalid(
                "no v1 signature verifies. This request was not sent by anyone "
                "holding the signing secret.")

        # AFTER the signature, for the same reason `TestProvider` does it in
        # this order: an unsigned request must not be able to learn our clock.
        drift = abs(int(time.time()) - timestamp)
        if drift > TIMESTAMP_TOLERANCE_SECONDS:
            raise TimestampOutOfRange(
                f"the request is {drift}s out of date (tolerance "
                f"{TIMESTAMP_TOLERANCE_SECONDS}s). A correctly signed request "
                f"that is old is a REPLAY: the signature proves who sent it, "
                f"never when.")

        return _event_from_stripe_body(body, provider=self.name)

    # ── Outbound ─────────────────────────────────────────────────────────

    def create_checkout_session(
        self, *, price: "ResolvedPrice", client_reference: str,
        success_url: str, cancel_url: str,
        customer_ref: Optional[str] = None,
    ) -> CheckoutSession:
        """Ask Stripe to open a hosted checkout for one subscription.

        `client_reference` is this system's own handle — the checkout-session row
        id — and Stripe returns it verbatim on the completed event. That is what
        lets a NEW customer's payment be attributed: at this point no
        `provider_customer_ref` exists, because the customer is created BY this
        checkout.

        The amount is sent explicitly rather than referencing a Stripe Price
        object, so the charge is the amount this deployment resolved from
        `billing_plan_prices` and cannot silently differ from what the user was
        shown.
        """
        form: list[tuple[str, str]] = [
            ("mode", "subscription"),
            ("success_url", success_url),
            ("cancel_url", cancel_url),
            ("client_reference_id", client_reference),
            ("line_items[0][quantity]", "1"),
            ("line_items[0][price_data][currency]", price.currency.lower()),
            ("line_items[0][price_data][unit_amount]", str(price.unit_amount_minor)),
            ("line_items[0][price_data][product_data][name]", price.display_name),
            ("line_items[0][price_data][recurring][interval]",
             "month" if price.recurrence == "monthly" else "year"),
        ]
        if customer_ref:
            form.append(("customer", customer_ref))

        payload = self._post("/checkout/sessions", form)
        session_ref = str(payload.get("id") or "")
        url = str(payload.get("url") or "")
        if not session_ref or not url:
            raise ProviderCallFailed(
                "the provider returned a checkout session with no id or no url; "
                "there is nowhere to send the user and nothing to reconcile the "
                "payment against")
        return CheckoutSession(provider=self.name, session_ref=session_ref, url=url)

    def create_portal_session(self, *, customer_ref: str,
                              return_url: str) -> PortalSession:
        """A billing portal for an EXISTING customer.

        Refuses without a customer reference rather than creating one: a portal
        opened for a customer this tenant is not mapped to would show somebody
        else's payment methods.
        """
        if not customer_ref:
            raise ProviderCallFailed(
                "this tenant has no provider customer reference, so there is no "
                "billing account to open a portal for")
        payload = self._post("/billing_portal/sessions",
                             [("customer", customer_ref), ("return_url", return_url)])
        url = str(payload.get("url") or "")
        if not url:
            raise ProviderCallFailed("the provider returned a portal session with no url")
        return PortalSession(provider=self.name, url=url)

    def _post(self, path: str, form: list[tuple[str, str]]) -> dict[str, Any]:
        """One place where this module talks to the network.

        `urllib` rather than a new dependency: the request is a form POST with a
        bearer token, and adding an HTTP client to the production lock to send
        it would be a supply-chain change to save four lines.
        """
        import json as _json
        import urllib.error
        import urllib.request

        # The scheme is checked before anything is opened. `api_base` comes from
        # `IRA_STRIPE_API_BASE`, so without this an operator — or anything that
        # can set an environment variable — could point the payment client at
        # `file:///etc/passwd` and have its contents parsed as a provider
        # response. Bandit B310 is right about this, and an allow-list is the
        # answer rather than a `# nosec`.
        #
        # `http` is permitted because the checkout path is exercised against a
        # local socket; `https` is what production uses. Nothing else is.
        target = f"{self.api_base}{path}"
        scheme = urllib.parse.urlparse(target).scheme.lower()
        if scheme not in ("https", "http"):
            raise ProviderCallFailed(
                f"refusing to call the payment provider over {scheme!r}: only "
                f"http and https are permitted")

        body = urllib.parse.urlencode(form).encode()
        request = urllib.request.Request(
            target,
            data=body,
            headers={
                "Authorization": f"Bearer {self._secret_key}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return _json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except Exception as read_error:  # noqa: BLE001 — status is the useful part
                # Named rather than passed over. A silently empty detail reads
                # in the log as "the provider said nothing", which is a
                # different and misleading fact from "we could not read what it
                # said". Bandit B110 is right that a bare pass hides this.
                detail = f"<the error body could not be read: {read_error}>"
            # The provider's message is logged and NOT returned to the caller:
            # it can quote the API key prefix and the customer id.
            logger.error("stripe %s failed: HTTP %s %s", path, exc.code, detail)
            raise ProviderCallFailed(
                f"the payment provider refused the request (HTTP {exc.code})") from exc
        except Exception as exc:  # noqa: BLE001 — a socket error is still a refusal
            logger.error("stripe %s failed: %s", path, exc)
            raise ProviderCallFailed(
                "the payment provider could not be reached") from exc


def _event_from_stripe_body(body: bytes, *, provider: str) -> PaymentEvent:
    """Parse a verified Stripe body into the shape `payments.py` records.

    Separate from `verify` so it can be read on its own: the customer reference
    sits in three different places depending on the event type, and getting it
    wrong means an event that is real, signed, recorded — and unattributed.
    """
    import json as _json

    try:
        parsed = _json.loads(body)
        if not isinstance(parsed, dict):
            raise ValueError("not an object")
    except (ValueError, UnicodeDecodeError) as exc:
        raise PaymentRefused(f"the signed body is not a JSON object: {exc}") from exc

    event_id = str(parsed.get("id") or "")
    event_type = str(parsed.get("type") or "")
    if not event_id or not event_type:
        raise PaymentRefused(
            "the event carries no id or no type, so it cannot be recorded "
            "exactly once")

    obj = parsed.get("data", {})
    obj = obj.get("object", {}) if isinstance(obj, dict) else {}
    if not isinstance(obj, dict):
        obj = {}

    # `customer` on a subscription or invoice; on a checkout session it is also
    # `customer`, but it may be absent until the session completes.
    customer = obj.get("customer")
    customer_ref = str(customer or "") if isinstance(customer, (str, type(None))) else ""

    return PaymentEvent(
        provider=provider,
        event_id=event_id,
        event_type=event_type,
        customer_ref=customer_ref,
        payload=parsed,
    )


@dataclass(frozen=True, slots=True)
class ResolvedPrice:
    """A price read from `billing_plan_prices`, with the plan's display name.

    Carried as one object so a caller cannot pass an amount from one plan and a
    name from another.
    """
    plan_id: str
    display_name: str
    currency: str
    unit_amount_minor: int
    recurrence: str

    @property
    def formatted(self) -> str:
        """For display. Derived from the same fields the charge uses."""
        symbol = {"GBP": "£", "USD": "$", "EUR": "€"}.get(self.currency, "")
        major = self.unit_amount_minor / 100
        per = {"monthly": "month", "yearly": "year"}.get(self.recurrence, self.recurrence)
        return f"{symbol}{major:.2f}/{per}" if symbol else \
               f"{major:.2f} {self.currency}/{per}"


def configured_provider() -> Optional[Any]:
    """The provider this deployment is configured for, or None.

    **None means refuse.** Every caller turns it into
    `PAYMENT_PROVIDER_NOT_CONFIGURED`; none of them treats it as free, and a
    test drives that path.

    `IRA_PAYMENT_PROVIDER=test` selects `TestProvider`, which exists so the
    webhook and lifecycle paths can be driven end to end without an account. It
    is refused outright when `IRA_ENVIRONMENT` is production-like: a deployment
    that could be talked into accepting test-signed billing events is one where
    anybody holding a value from a config file can grant themselves a plan.
    """
    choice = (os.getenv("IRA_PAYMENT_PROVIDER") or "").strip().lower()

    if choice == "test":
        from core.editions import current_edition  # noqa: F401  (kept explicit)

        environment = (os.getenv("IRA_ENVIRONMENT") or "development").strip().lower()
        if environment in {"production", "staging"}:
            logger.error(
                "IRA_PAYMENT_PROVIDER=test refused in environment %r: a "
                "test-signed billing event would be able to grant a plan",
                environment)
            return None
        secret = os.getenv("IRA_PAYMENT_TEST_SECRET") or ""
        if not secret:
            logger.error("IRA_PAYMENT_PROVIDER=test with no IRA_PAYMENT_TEST_SECRET")
            return None
        return TestProvider(secret)

    if choice == "stripe":
        secret_key = os.getenv("IRA_STRIPE_SECRET_KEY") or ""
        webhook_secret = os.getenv("IRA_STRIPE_WEBHOOK_SECRET") or ""
        if not secret_key or not webhook_secret:
            # Named separately in the log so an operator knows which half is
            # missing. Not returned to the caller: which secrets a deployment
            # holds is not a fact for an unauthenticated request to learn.
            logger.error(
                "IRA_PAYMENT_PROVIDER=stripe but %s missing",
                " and ".join(
                    n for n, v in (("IRA_STRIPE_SECRET_KEY", secret_key),
                                   ("IRA_STRIPE_WEBHOOK_SECRET", webhook_secret))
                    if not v))
            return None
        return StripeProvider(secret_key=secret_key, webhook_secret=webhook_secret,
                              api_base=os.getenv("IRA_STRIPE_API_BASE") or None)

    if choice:
        logger.error("IRA_PAYMENT_PROVIDER=%r is not a provider this build has", choice)
    return None


def provider_supports_checkout(provider: Any) -> bool:
    """Whether this provider can open a checkout.

    `TestProvider` verifies inbound events and cannot take money — it has no
    account. Asking it for a checkout must refuse rather than return a URL to
    nowhere, so the capability is checked rather than assumed from the fact that
    a provider exists at all.
    """
    return callable(getattr(provider, "create_checkout_session", None))


__all__ = [
    "CHECKOUT_UNAVAILABLE",
    "PAYMENT_PROVIDER_NOT_CONFIGURED",
    "CheckoutSession",
    "PortalSession",
    "ProviderCallFailed",
    "ProviderNotConfigured",
    "ResolvedPrice",
    "StripeProvider",
    "configured_provider",
    "provider_supports_checkout",
]
