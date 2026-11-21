"""Phase 4 batch 10 — AWS KMS actually holds the master key.

P4-04 built the provider boundary and the production refusal, implemented no
managed provider, and said so plainly. This is the provider, and these tests run
it against a real `boto3` KMS client talking to an in-process KMS
implementation (`moto`) — real API shapes, real error codes, real encryption
context enforcement.

── What that does and does not prove ───────────────────────────────────────

It proves the client code: the calls, the encryption context, the retry
classification, the caching, the failure behaviour, and that a KEK minted by KMS
round-trips through every existing wrap/unwrap path.

It does **not** prove IAM policy, key policy, regional availability, or billing.
Those need a provisioned account, and no test can invent one. What this removes
is the far larger risk — that the integration is written, never executed, and
discovered to be wrong on the day it is first needed.

**And the emulator is weaker than KMS in at least one way that matters here:**
`moto` allows `Decrypt` against a key it has disabled. That was measured, not
assumed. The two tests that depend on key-state enforcement therefore inject the
real AWS error code at the boto3 boundary and assert this repository's handling,
rather than relying on a fixture that would have let them pass without ever
entering the refusal path.

── The property that matters most ──────────────────────────────────────────

**Selecting `aws-kms` must never fall through to the environment KEK.** That
substitution would be invisible: the application starts, encrypts new rows under
the wrong key, and fails to open the old ones only later. Several tests below
exist for that one property, approached from different directions.
"""
from __future__ import annotations

import base64
import os
import secrets as _stdlib_secrets

import pytest

boto3 = pytest.importorskip("boto3", reason="boto3 not installed")
moto = pytest.importorskip("moto", reason="moto not installed")

from moto import mock_aws  # noqa: E402

REGION = "eu-west-2"


@pytest.fixture
def kms():
    """A KMS key, and the environment that points at it.

    `mock_aws` intercepts at the botocore layer, so the code under test builds a
    real client and makes real calls — the seam is below our code, not inside
    it. A test that monkeypatched `aws_kms._client` would be checking a stub.
    """
    from core.crypto import aws_kms

    aws_kms.reset_cache()
    with mock_aws():
        client = boto3.client("kms", region_name=REGION)
        key = client.create_key(Description="ira acceptance")["KeyMetadata"]
        env = {
            aws_kms.REGION_ENV: REGION,
            aws_kms.KEY_ID_ENV: key["KeyId"],
            "AWS_ACCESS_KEY_ID": "testing",
            "AWS_SECRET_ACCESS_KEY": "testing",
            "IRA_KEK_PROVIDER": "aws-kms",
        }
        yield env, key["Arn"], client
    aws_kms.reset_cache()


# ─────────────────────────────────────────────────────────────────────────────
# The round trip
# ─────────────────────────────────────────────────────────────────────────────


def test_a_kek_can_be_minted_and_then_loaded(kms):
    from core.crypto import aws_kms

    env, arn, _ = kms
    ciphertext = aws_kms.create_kek(env)
    assert ciphertext and base64.b64decode(ciphertext, validate=True)

    env[aws_kms.CIPHERTEXT_ENV] = ciphertext
    key, key_id = aws_kms.load_kek(env)
    assert len(key) == 32, "AES-256-GCM needs exactly 32 bytes"
    assert key_id == arn, (
        "the recorded key id is not the ARN KMS reported. An alias would make "
        "a row wrapped before the alias moved indistinguishable from one "
        "wrapped after, and _unwrap_dek refuses on a kek_id mismatch")


def test_create_kek_never_returns_the_plaintext(kms):
    """The operator needs the ciphertext and nothing else. A function handing
    back both would make its caller a place the KEK can be logged."""
    from core.crypto import aws_kms

    env, _, _ = kms
    result = aws_kms.create_kek(env)
    assert isinstance(result, str)

    env[aws_kms.CIPHERTEXT_ENV] = result
    key, _ = aws_kms.load_kek(env)
    assert base64.b64decode(result) != key, (
        "create_kek returned something that decodes to the plaintext key")


def test_the_kek_is_cached_rather_than_fetched_per_operation(kms):
    """One KMS call per process, not one per record.

    The alternative makes KMS a hard dependency of every voice authentication
    and every credential read, so a KMS blip logs everybody out. The trade-off
    is stated in the module: a compromised process keeps its KEK until restart.
    """
    from core.crypto import aws_kms

    env, _, client = kms
    env[aws_kms.CIPHERTEXT_ENV] = aws_kms.create_kek(env)

    calls = {"n": 0}
    real = aws_kms._call

    def counting(operation, *, what, **kwargs):
        if what == "decrypt the KEK":
            calls["n"] += 1
        return real(operation, what=what, **kwargs)

    aws_kms._call = counting
    try:
        first, _ = aws_kms.load_kek(env)
        second, _ = aws_kms.load_kek(env)
    finally:
        aws_kms._call = real

    assert first == second
    assert calls["n"] == 1, f"decrypted the KEK {calls['n']} times, not once"


def test_reset_cache_makes_the_next_load_fetch_again(kms):
    """Rotation depends on it: the point of rotating is that the next load gets
    a different key, and without this the old one survives until the process
    does."""
    from core.crypto import aws_kms

    env, _, _ = kms
    env[aws_kms.CIPHERTEXT_ENV] = aws_kms.create_kek(env)
    first, _ = aws_kms.load_kek(env)

    aws_kms.reset_cache()
    env[aws_kms.CIPHERTEXT_ENV] = aws_kms.create_kek(env)
    second, _ = aws_kms.load_kek(env)

    assert first != second, "a new KEK was minted but the old one was returned"


# ─────────────────────────────────────────────────────────────────────────────
# Fail closed — the property that matters most
# ─────────────────────────────────────────────────────────────────────────────


def test_selecting_kms_does_not_fall_back_to_the_environment_key(kms):
    """**The substitution ADR-0012 exists to prevent.**

    An environment KEK is present and perfectly valid. The provider is `aws-kms`
    and the ciphertext is missing. The only acceptable outcome is a refusal — a
    fallback here starts the application, encrypts new rows under the wrong key,
    and fails to open the old ones only later.
    """
    from core.connectors.secrets import MissingEncryptionKey
    from core.crypto.aws_kms import KmsUnavailable
    from core.connectors import secrets as secrets_module

    env, _, _ = kms
    env["IRA_CONNECTOR_ENCRYPTION_KEY"] = base64.b64encode(
        _stdlib_secrets.token_bytes(32)).decode()
    # No IRA_KEK_CIPHERTEXT.

    with pytest.raises((KmsUnavailable, MissingEncryptionKey)) as excinfo:
        secrets_module._load_key(env)
    assert "IRA_KEK_CIPHERTEXT" in str(excinfo.value), (
        "the failure did not name the missing ciphertext — it may have fallen "
        "through to the environment key path")


def test_the_kek_load_path_actually_routes_through_kms(kms):
    """The wiring, not the module. `secrets._load_key` is what every wrap and
    unwrap in the system calls; a provider nothing routes to is the "correct
    mechanism with no callers" shape this repository keeps finding."""
    from core.connectors import secrets as secrets_module
    from core.crypto import aws_kms

    env, arn, _ = kms
    env[aws_kms.CIPHERTEXT_ENV] = aws_kms.create_kek(env)
    # A DIFFERENT environment key, so if the fallback were taken the returned
    # id would be "default" rather than the ARN.
    env["IRA_CONNECTOR_ENCRYPTION_KEY"] = base64.b64encode(
        _stdlib_secrets.token_bytes(32)).decode()

    key, key_id = secrets_module._load_key(env)
    assert key_id == arn, (
        f"_load_key returned key_id {key_id!r}; the KMS provider was selected "
        f"and should have reported the KMS ARN")
    assert len(key) == 32


def test_a_wrong_encryption_context_is_refused(kms):
    """KMS binds the context to the ciphertext. A KEK ciphertext lifted from one
    deployment is useless in another even to somebody holding both, and this is
    the assertion that the binding is actually in use rather than declared."""
    from core.crypto import aws_kms

    env, _, client = kms

    # An EMPTY context binds nothing, and a test that only compared two
    # different contexts would pass with `CONTEXT = {}` — the mismatch it
    # observes would be between `{}` and the wrong one, not between the
    # module's and the wrong one. A planted defect emptying CONTEXT walked
    # straight past the first version of this test.
    assert aws_kms.CONTEXT, (
        "the encryption context is empty, so the KEK ciphertext is bound to "
        "nothing and is portable to any deployment that can call this key")

    ciphertext = base64.b64decode(aws_kms.create_kek(env))

    with pytest.raises(Exception):
        client.decrypt(CiphertextBlob=ciphertext,
                       EncryptionContext={"application": "somebody-else"})

    # And with NO context at all: this is what proves the module actually
    # applied its context when encrypting, rather than declaring one it never
    # passes to KMS.
    with pytest.raises(Exception):
        client.decrypt(CiphertextBlob=ciphertext, EncryptionContext={})

    opened = client.decrypt(CiphertextBlob=ciphertext,
                            EncryptionContext=aws_kms.CONTEXT)
    assert len(opened["Plaintext"]) == 32


def test_a_disabled_key_refuses_rather_than_degrading(kms, monkeypatch):
    """**The emulator cannot prove this, so the error is injected instead.**

    `moto` allows `Decrypt` against a key it has disabled — verified, not
    assumed. Real KMS raises `KMSInvalidStateException`. Relying on the emulator
    here would have produced a test that passed because the fixture was
    permissive, which is worse than no test: it would report the refusal path as
    proven while never entering it.

    So the AWS error is injected at the boto3 boundary and MY handling is what
    is asserted — which is the part this repository is responsible for.
    """
    from botocore.exceptions import ClientError
    from core.crypto import aws_kms

    env, _, _ = kms
    env[aws_kms.CIPHERTEXT_ENV] = aws_kms.create_kek(env)
    aws_kms.reset_cache()

    class _Disabled:
        def decrypt(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "KMSInvalidStateException",
                           "Message": "key is disabled"}}, "Decrypt")

    monkeypatch.setattr(aws_kms, "_client", lambda env=None: _Disabled())

    with pytest.raises(aws_kms.KmsUnavailable, match="KMSInvalidState"):
        aws_kms.load_kek(env)


def test_a_missing_region_is_configuration_not_a_transient_fault(kms):
    from core.crypto import aws_kms

    env, _, _ = kms
    broken = dict(env)
    broken.pop(aws_kms.REGION_ENV)
    broken[aws_kms.CIPHERTEXT_ENV] = "AAAA"

    with pytest.raises(aws_kms.KmsUnavailable, match="AWS_REGION"):
        aws_kms.load_kek(broken)


def test_corrupt_ciphertext_refuses(kms):
    from core.crypto import aws_kms

    env, _, _ = kms
    env[aws_kms.CIPHERTEXT_ENV] = base64.b64encode(b"not a kms blob").decode()
    aws_kms.reset_cache()

    with pytest.raises(aws_kms.KmsUnavailable):
        aws_kms.load_kek(env)


# ─────────────────────────────────────────────────────────────────────────────
# Retry classification
# ─────────────────────────────────────────────────────────────────────────────


def test_a_permission_failure_is_not_retried(kms):
    """`AccessDeniedException` means fix the IAM policy; `ThrottlingException`
    means wait. A loop that treats them alike turns the first into a
    four-attempt delay before the identical failure, and adds load to a service
    that is refusing on purpose."""
    from botocore.exceptions import ClientError
    from core.crypto import aws_kms

    attempts = {"n": 0}

    def denied(**kwargs):
        attempts["n"] += 1
        raise ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
            "Decrypt")

    with pytest.raises(aws_kms.KmsUnavailable, match="AccessDenied"):
        aws_kms._call(denied, what="decrypt the KEK")
    assert attempts["n"] == 1, f"retried a permission failure {attempts['n']} times"


def test_a_throttle_is_retried_and_can_succeed(kms):
    from botocore.exceptions import ClientError
    from core.crypto import aws_kms

    attempts = {"n": 0}

    def flaky(**kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
                "Decrypt")
        return {"Plaintext": b"x" * 32, "KeyId": "arn:aws:kms:::key/ok"}

    result = aws_kms._call(flaky, what="decrypt the KEK")
    assert result["KeyId"].endswith("ok")
    assert attempts["n"] == 3


def test_retries_are_bounded(kms):
    from botocore.exceptions import ClientError
    from core.crypto import aws_kms

    attempts = {"n": 0}

    def always_throttled(**kwargs):
        attempts["n"] += 1
        raise ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "no"}}, "Decrypt")

    with pytest.raises(aws_kms.KmsUnavailable):
        aws_kms._call(always_throttled, what="decrypt the KEK")
    assert attempts["n"] == aws_kms._ATTEMPTS, (
        f"retried {attempts['n']} times against a bound of {aws_kms._ATTEMPTS}")


# ─────────────────────────────────────────────────────────────────────────────
# Readiness, and no key material anywhere near a log
# ─────────────────────────────────────────────────────────────────────────────


def test_health_does_a_real_decrypt(kms, monkeypatch):
    """Not a `DescribeKey`. Permission to describe a key is not permission to
    use it, and a readiness probe that passes while decryption is denied is the
    probe reporting the wrong thing.

    The unhealthy half injects the AWS error for the same reason as the test
    above: moto does not enforce key state.
    """
    from botocore.exceptions import ClientError
    from core.crypto import aws_kms

    env, _, _ = kms
    env[aws_kms.CIPHERTEXT_ENV] = aws_kms.create_kek(env)

    ok, reason = aws_kms.health(env)
    assert ok is True and "decrypted" in reason

    class _Denied:
        def decrypt(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                "Decrypt")

    monkeypatch.setattr(aws_kms, "_client", lambda env=None: _Denied())
    ok, reason = aws_kms.health(env)
    assert ok is False, "health passed while KMS could not open the KEK"
    assert "kms" in reason


def test_nothing_logs_key_material(kms, caplog):
    """The KEK, its ciphertext, and any fragment of either. A log line is copied
    into bug reports and shipped to aggregators far more widely than a database
    is."""
    import logging

    from core.crypto import aws_kms

    env, _, _ = kms
    with caplog.at_level(logging.DEBUG):
        ciphertext = aws_kms.create_kek(env)
        env[aws_kms.CIPHERTEXT_ENV] = ciphertext
        key, _ = aws_kms.load_kek(env)

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert ciphertext not in logged, "the KEK ciphertext was logged"
    assert base64.b64encode(key).decode() not in logged, "the KEK was logged"
    # Fragments too: 8 bytes is short enough to appear by chance in nothing and
    # long enough that its presence is not a coincidence.
    assert base64.b64encode(key).decode()[:12] not in logged
    assert ciphertext[:16] not in logged


# ─────────────────────────────────────────────────────────────────────────────
# The provider registry knows about it
# ─────────────────────────────────────────────────────────────────────────────


def test_aws_kms_is_implemented_not_planned():
    from core.crypto import key_provider

    assert key_provider.AWS_KMS in key_provider.IMPLEMENTED
    assert key_provider.AWS_KMS not in key_provider.PLANNED, (
        "aws-kms is listed as planned, so resolve() will refuse it as "
        "'not implemented in this build' — the message and the code disagree")


def test_selecting_it_is_not_a_development_provider():
    """The production refusal must not fire on a managed provider. If it did,
    the only way to run production would be the environment key — the opposite
    of what P4-04 built."""
    from core.crypto import key_provider

    assert not key_provider.is_development_provider(key_provider.AWS_KMS)


def test_production_accepts_it_without_an_explicit_opt_in():
    """P4-04 made production refuse the DEFAULT provider unless deliberately
    chosen. A managed provider is the thing that refusal was pushing operators
    towards, so it must resolve cleanly."""
    from core.crypto import key_provider

    class _Prod:
        is_production_like = True
        value = "production"

    resolved = key_provider.resolve(
        _Prod(), env={"IRA_KEK_PROVIDER": key_provider.AWS_KMS})
    assert resolved == key_provider.AWS_KMS


# ─────────────────────────────────────────────────────────────────────────────
# Rotation, end to end through the real wrap/unwrap code
# ─────────────────────────────────────────────────────────────────────────────


def test_a_kms_kek_round_trips_through_the_real_wrap_and_unwrap(kms):
    """**The claim that the integration is real rather than adjacent.**

    A KEK minted by KMS is handed to the same `wrap_dek`/`_unwrap_dek` that
    P2-05 and P4-02 use. If the shape were wrong — length, encoding, id — this
    is where it shows, rather than on the day somebody enrols a second factor.
    """
    from core.connectors import secrets as secrets_module
    from core.crypto import aws_kms

    env, arn, _ = kms
    env[aws_kms.CIPHERTEXT_ENV] = aws_kms.create_kek(env)

    wrapped = secrets_module.wrap_dek(b"D" * 32, aad="p4-10-aad", env=env)
    assert wrapped.kek_id == arn, (
        "the wrapped DEK records the wrong KEK id; a rotation could not tell "
        "a stale wrapping from a current one")

    opened = secrets_module._unwrap_dek(wrapped, aad="p4-10-aad", env=env)
    assert opened == b"D" * 32


def test_rotating_the_kek_strands_material_that_was_not_rewrapped(kms):
    """The hazard P4-05 exists for, now demonstrated with a REAL KMS KEK.

    Rotate, do not re-wrap, and the old DEK no longer opens. This is what makes
    the re-wrap step load-bearing rather than decorative — without this
    assertion, a rotation test that passed would not prove the KEK mattered.
    """
    from core.connectors import secrets as secrets_module
    from core.crypto import aws_kms

    env, _, _ = kms
    env[aws_kms.CIPHERTEXT_ENV] = aws_kms.create_kek(env)
    wrapped = secrets_module.wrap_dek(b"D" * 32, aad="p4-10-aad", env=env)

    # A new KEK, exactly as an operator rotating one would produce.
    aws_kms.reset_cache()
    rotated = dict(env)
    rotated[aws_kms.CIPHERTEXT_ENV] = aws_kms.create_kek(env)

    with pytest.raises(Exception):
        secrets_module._unwrap_dek(wrapped, aad="p4-10-aad", env=rotated)


def test_a_rewrapped_dek_opens_under_the_rotated_kek(kms):
    """And the other half: re-wrapping under the new KEK restores access, so
    the rotation path is survivable rather than merely destructive."""
    from core.connectors import secrets as secrets_module
    from core.crypto import aws_kms

    env, _, _ = kms
    env[aws_kms.CIPHERTEXT_ENV] = aws_kms.create_kek(env)
    dek = b"D" * 32
    wrapped = secrets_module.wrap_dek(dek, aad="p4-10-aad", env=env)

    opened = secrets_module._unwrap_dek(wrapped, aad="p4-10-aad", env=env)

    aws_kms.reset_cache()
    rotated = dict(env)
    rotated[aws_kms.CIPHERTEXT_ENV] = aws_kms.create_kek(env)
    rewrapped = secrets_module.wrap_dek(opened, aad="p4-10-aad", env=rotated)

    assert secrets_module._unwrap_dek(
        rewrapped, aad="p4-10-aad", env=rotated) == dek


# ─────────────────────────────────────────────────────────────────────────────
# Readiness is wired, not merely available
# ─────────────────────────────────────────────────────────────────────────────


async def test_readiness_runs_the_key_provider_check():
    """A health function nothing calls reports nothing. `evaluate_readiness`
    must actually include it — the "correct mechanism with no callers" shape,
    checked rather than assumed."""
    from core.runtime import readiness

    source = __import__("pathlib").Path(readiness.__file__).read_text()
    assert "check_key_provider(required=True)" in source, (
        "check_key_provider is defined but not gathered by evaluate_readiness, "
        "so an instance that cannot reach KMS still reports ready")


async def test_the_key_provider_check_passes_on_the_development_provider():
    """It must be inert when the KEK is an environment variable, or every
    developer's `/readyz` starts failing for a dependency they do not use."""
    from core.runtime.readiness import CheckStatus, check_key_provider

    result = await check_key_provider(required=True)
    assert result.status is not CheckStatus.FAILED
