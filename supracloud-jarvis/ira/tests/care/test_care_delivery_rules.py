"""Remediation group A2, the pure rules: a message's state is what the provider
said and never more, and a status callback is believed only when it is ours and
signed by the provider."""
from __future__ import annotations

import asyncio
import uuid

import pytest

from care import delivery, providers

T, W, A = (uuid.UUID(f"{d * 8}-{d * 4}-{d * 4}-{d * 4}-{d * 12}") for d in "123")
URL = f"https://care.example.invalid/api/v1/care/providers/twilio/status/{T}/{W}/{A}?t=abc123"
PARAMS = {"MessageSid": "SM0123456789abcdef0123456789abcdef", "MessageStatus": "delivered",
          "AccountSid": "AC00000000000000000000000000000000", "To": "+447700900123"}
TOKEN = "test-auth-token-not-real"
#: Computed with Twilio's own `twilio.request_validator.RequestValidator` (twilio
#: 9.x) for exactly URL, PARAMS and TOKEN above: an independent reference.
TWILIO_REFERENCE_SIGNATURE = "/ehdttnxDVIJOj2dryVy02CMr38="

ENV = {"IRA_CARE_CALLBACK_SECRET": "a-test-secret-only", "IRA_CARE_PUBLIC_BASE_URL": "https://care.example.invalid"}


def test_our_signature_matches_twilios_own_validator():
    assert delivery.twilio_signature(URL, PARAMS, TOKEN) == TWILIO_REFERENCE_SIGNATURE
    assert delivery.verify_twilio(URL, PARAMS, TWILIO_REFERENCE_SIGNATURE, TOKEN)


@pytest.mark.parametrize("change", [
    lambda u, p, s, t: (u.replace("delivered", "x"), dict(p, MessageStatus="failed"), s, t),   # a field altered
    lambda u, p, s, t: (u.replace(str(A), str(T)), p, s, t),                                   # another row
    lambda u, p, s, t: (u, dict(p, Extra="1"), s, t),                                          # a field added
    lambda u, p, s, t: (u, p, "", t),                                                          # no signature
    lambda u, p, s, t: (u, p, s, ""),                                                          # no token configured
    lambda u, p, s, t: (u, p, s, "another-token"),                                             # wrong account
])
def test_anything_altered_or_missing_is_refused(change):
    assert not delivery.verify_twilio(*change(URL, PARAMS, TWILIO_REFERENCE_SIGNATURE, TOKEN))


def test_the_callback_url_carries_our_token_and_only_exists_over_https_with_a_secret():
    url = delivery.status_callback_url(T, W, A, env=ENV)
    assert url.startswith(f"https://care.example.invalid/api/v1/care/providers/twilio/status/{T}/{W}/{A}?t=")
    token = url.rsplit("=", 1)[1]
    assert delivery.verify_token(T, W, A, token, env=ENV)
    assert not delivery.verify_token(T, W, uuid.uuid4(), token, env=ENV), "a token is for one row only"
    assert not delivery.verify_token(T, uuid.uuid4(), A, token, env=ENV), "and one workspace only"
    assert delivery.status_callback_url(T, W, A, env=dict(ENV, IRA_CARE_PUBLIC_BASE_URL="http://care.example.invalid")) is None
    assert delivery.status_callback_url(T, W, A, env={"IRA_CARE_PUBLIC_BASE_URL": "https://x.invalid"}) is None
    assert not delivery.verify_token(T, W, A, "", env={}), "no secret: nothing verifies"


def test_states_only_move_forward_and_accepted_is_never_delivered():
    r = delivery.rank
    assert r("queued") < r("accepted") < r("delivered")
    assert r("delivered") < r("undelivered") == r("failed") == r("no_answer") == r("busy")
    assert r("accepted") == r("sent"), "a pre-113 'sent' meant accepted"
    # Twilio's words: taken is not delivered; a call that rang out is not answered.
    for word in ("queued", "sending", "sent", "accepted", "initiated", "ringing"):
        assert delivery.TWILIO_STATUS[word] == "accepted", word
    assert delivery.TWILIO_STATUS["delivered"] == "delivered"
    assert delivery.TWILIO_STATUS["no-answer"] == "no_answer" and delivery.TWILIO_STATUS["busy"] == "busy"
    assert delivery.TWILIO_STATUS["undelivered"] == "undelivered"


def test_twilio_accepting_a_message_is_recorded_as_accepted_not_sent(monkeypatch):
    import httpx

    class _Resp:
        status_code = 201

        def json(self):
            return {"sid": "SMabc"}

    class _Client:
        def __init__(self, *a, **k):
            self.form = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None, auth=None):
            _Client.last = data
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    tw = providers.TwilioProvider("ACx", "tok", "+440000000009")
    d = asyncio.run(tw.send_sms("+447700900123", "hello", status_callback="https://care.example.invalid/cb"))
    assert (d.outcome, d.provider_ref) == ("accepted", "SMabc")
    assert _Client.last["StatusCallback"] == "https://care.example.invalid/cb"
    d = asyncio.run(tw.call("+447700900123", "hello", status_callback="https://care.example.invalid/cb"))
    assert d.outcome == "accepted" and "answered" in _Client.last["StatusCallbackEvent"]
