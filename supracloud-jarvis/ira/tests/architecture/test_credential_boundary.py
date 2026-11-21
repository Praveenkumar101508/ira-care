"""Phase 7 — nothing outside core/connectors/ may reach a credential.

`core/connectors/secrets.py` is the only module that decrypts an OAuth token.
That is a claim about the whole codebase, so it is checked against the whole
codebase: this suite parses the AST of every Python module under `ira/` and
fails if anything outside the allowed package imports the decryption path.

Same approach as `test_edition_boundaries.py`, and for the same reason — a
grep for the string would be defeated by an alias, and a convention held by
memory is not held at all.

`test_the_scanner_detects_a_planted_violation` writes a module that breaks the
rule and asserts the scanner catches it, so a scanner that silently stopped
working could not pass this file.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

# tests/architecture/ -> tests/ -> ira/
IRA_ROOT = Path(__file__).resolve().parents[2]

#: The package allowed to handle credentials. Connectors live here, and so does
#: the module that seals and opens them.
ALLOWED_PACKAGE = "core/connectors"

#: The one module OUTSIDE that package permitted to hold key material, and why.
#:
#: P2-05 encrypts TOTP seeds at rest, which needs the same envelope primitives —
#: `wrap_dek`, `_unwrap_dek`, `seal_with`, `use_secret_async`. Three ways to
#: allow that were available and two are worse than this one. Re-implementing
#: AES-GCM in `security/` would put a second, unreviewed cryptographic
#: implementation in the codebase. Moving MFA key handling under
#: `core/connectors/` would file a second factor as a connector credential and
#: make the package name a lie.
#:
#: So the exemption is named, pinned by `test_the_exemption_list_is_exactly_what
#: _is_expected`, and NARROWED by
#: `test_the_exempt_module_cannot_reach_connector_credentials`: mfa_crypto may
#: hold the key material for the secrets it owns, and has no route to a
#: connector DEK. The rule this file enforces is "may not obtain key material
#: you do not own", and that is the shape the exemption keeps.
#:
#: `security/voice_crypto.py` is the second, added by Phase 4 batch 02, and it
#: earns it exactly the same way rather than by resembling the first. A voice
#: template is encrypted under CLOUD-ADR-0012's envelope scheme, so it needs the
#: same four primitives, and the same three options were available: a second
#: unreviewed AES-GCM implementation under `security/`, filing a biometric
#: template as a connector credential, or a named exemption. It owns the secrets
#: it holds keys for — a per-RECORD voice DEK and nothing else — and imports no
#: symbol that resolves a connector DEK, which the narrowing test below is what
#: actually establishes.
#:
#: Two entries is not a trend, and the bar does not move: a module belongs here
#: only when it OWNS the secrets whose key material it handles.
EXEMPT_MODULES = frozenset({"security/mfa_crypto.py", "security/voice_crypto.py"})

#: Symbols that resolve a *connector* DEK specifically — the keyring entry
#: points. The exempt module must not import these: they would hand it the key
#: to every stored OAuth token in a tenant, which is precisely the capability
#: this file exists to confine to one package.
CONNECTOR_KEY_SYMBOLS = frozenset({"dek_for", "active_dek", "rewrap"})


def _is_exempt(path: Path) -> bool:
    posix = path.as_posix()
    return any(posix.endswith(f"/{m}") or posix == m for m in EXEMPT_MODULES)

#: Names that mean "I am handling a raw credential".
#:
#: The second group arrived with blocker B3's envelope encryption. A caller
#: holding a data-encryption key can open every credential belonging to that
#: tenant and version, so a DEK is exactly as sensitive as a decrypted token and
#: is fenced the same way. Adding the crypto without extending this set would
#: have left a second, wider door beside the one this file guards.
CREDENTIAL_SYMBOLS = frozenset({
    "_open_secret",   # the raw decrypt
    "use_secret",     # the callback form — still gives fn the token
    "use_secret_async",
    "rewrap",
    # Key material — B3.
    "_unwrap_dek",    # wrapped DEK → DEK
    "generate_dek",   # fresh key material
    "seal_with",      # takes a DEK
    "wrap_dek",       # takes a DEK
    "dek_for",        # keyring: DB row → DEK
    "active_dek",     # keyring: mints or resolves, returns a DEK
})

#: Modules whose *name* alone means credentials. `from core.connectors import
#: keyring` hands the importer `keyring.dek_for`, so the module is as good as
#: the symbol and is refused the same way `secrets` is.
CREDENTIAL_MODULES = frozenset({"secrets", "keyring"})

#: Files that legitimately mention these without being production code.
def _skip(path: Path) -> bool:
    parts = path.parts
    return (
        "tests" in parts
        or "__pycache__" in parts
        or ".venv" in parts
        or "site-packages" in parts
        or "future-scale" in parts
    )


def _modules() -> list[Path]:
    return sorted(p for p in IRA_ROOT.rglob("*.py") if not _skip(p))


def _credential_imports(path: Path) -> list[str]:
    """Every import in `path` that pulls in a credential-handling symbol.

    Catches all three forms, because each is a real way to write it:

        from core.connectors.secrets import use_secret
        from core.connectors import secrets          → secrets.use_secret(...)
        import core.connectors.secrets as s          → s.use_secret(...)

    ...and the same three against `keyring`, which hands out DEKs.

    One import is deliberately NOT an offence: `from core.connectors.keyring
    import destroy_keys`. Destroying a key is the opposite of reading one, and
    `core/privacy/erasure.py` has to be able to do it — see `erase_keys`. The
    line this file draws is "may not obtain key material", not "may not mention
    the module".
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
        return []

    watched = tuple(f"connectors.{m}" for m in CREDENTIAL_MODULES)

    offences: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.endswith(watched) or module.endswith("connectors"):
                for alias in node.names:
                    if (
                        alias.name in CREDENTIAL_SYMBOLS
                        or alias.name in CREDENTIAL_MODULES
                    ):
                        offences.append(f"from {module} import {alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith(
                    tuple(f"core.connectors.{m}" for m in CREDENTIAL_MODULES)
                ):
                    offences.append(f"import {alias.name}")
    return offences


def test_the_scan_finds_modules_at_all():
    """A scanner that walks nothing passes everything."""
    modules = _modules()
    assert len(modules) > 100, f"only {len(modules)} modules scanned — the walk is wrong"
    assert any("core/connectors" in p.as_posix() for p in modules)


def test_the_secrets_module_exists_and_is_where_the_rule_says():
    assert (IRA_ROOT / "core" / "connectors" / "secrets.py").is_file()


@pytest.mark.parametrize(
    "path",
    [p for p in _modules()
     if ALLOWED_PACKAGE not in p.as_posix() and not _is_exempt(p)],
    ids=lambda p: p.relative_to(IRA_ROOT).as_posix(),
)
def test_no_module_outside_the_connector_package_touches_credentials(path: Path):
    """The claim, checked module by module.

    A leak now requires adding an import that this test refuses, rather than
    merely a careless line inside a file nobody re-reads.
    """
    offences = _credential_imports(path)
    assert not offences, (
        f"{path.relative_to(IRA_ROOT).as_posix()} imports the credential path: "
        f"{offences}. Only {ALLOWED_PACKAGE}/ may handle OAuth tokens — the "
        f"model must never reach them (see core/connectors/secrets.py). If this "
        f"module genuinely needs connector data, call the ConnectorGateway, "
        f"which returns results and receipts and never a credential."
    )


def test_the_exemption_list_is_exactly_what_is_expected():
    """The exemption is a decision, so growing it has to be one too.

    Without this, `EXEMPT_MODULES` is a place to quietly put the next module
    that finds the boundary inconvenient — and an allow-list nobody has to
    argue with is not a boundary.
    """
    assert EXEMPT_MODULES == frozenset({"security/mfa_crypto.py",
                                        "security/voice_crypto.py"}), (
        f"the credential-boundary exemption list changed to {sorted(EXEMPT_MODULES)}. "
        f"Adding a module here grants it the envelope-encryption primitives for "
        f"the whole process. Justify it in the comment above EXEMPT_MODULES and "
        f"update this assertion deliberately."
    )


def test_every_exempt_module_exists():
    """An exemption for a file that has moved is an exemption for nothing, and
    it fails open: the scanner simply stops checking a path that no longer
    matches."""
    for module in EXEMPT_MODULES:
        assert (IRA_ROOT / module).is_file(), (
            f"{module} is exempt from the credential boundary but does not "
            f"exist. Either the file moved — in which case the exemption is "
            f"silently covering nothing — or the exemption is stale."
        )


@pytest.mark.parametrize("module", sorted(EXEMPT_MODULES))
def test_the_exempt_module_cannot_reach_connector_credentials(module: str):
    """The narrowing that makes the exemption safe to grant.

    Each exempt module may hold key material for the secrets it OWNS —
    `mfa_crypto` for MFA seeds, `voice_crypto` for voice templates. Neither may
    have a route to a *connector* DEK, because one `dek_for` would open every
    stored OAuth token in a tenant and the exemption would have become the leak
    it was carved out beside.
    """
    tree = ast.parse((IRA_ROOT / module).read_text(encoding="utf-8"))
    reached: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                # `keyring` by name too: importing the module hands over
                # `keyring.dek_for`, so it is the same reach in one more step.
                if (alias.name in CONNECTOR_KEY_SYMBOLS
                        or alias.name in CREDENTIAL_MODULES):
                    reached.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith(
                    tuple(f"core.connectors.{m}" for m in CREDENTIAL_MODULES)
                ):
                    reached.add(alias.name)
    reached = sorted(reached)
    assert not reached, (
        f"{module} is exempt from the credential boundary so that it can seal "
        f"the secrets it owns. It imports {reached}, which resolves key "
        f"material belonging to the CONNECTOR domain — that is not what the "
        f"exemption is for, and it turns a narrow carve-out into a second door "
        f"onto every stored OAuth token."
    )


def test_the_scanner_detects_a_planted_violation(tmp_path):
    """Proves the scanner works, so a broken scanner cannot pass this file."""
    offender = tmp_path / "leaky.py"
    offender.write_text(
        "from core.connectors.secrets import use_secret\n"
        "def leak(sealed):\n"
        "    return use_secret(sealed, lambda token: token)\n",
        encoding="utf-8",
    )
    assert _credential_imports(offender), "the scanner missed a direct import"


def test_the_scanner_detects_the_aliased_form(tmp_path):
    """`import x as y` is the form a plain grep misses."""
    offender = tmp_path / "aliased.py"
    offender.write_text("import core.connectors.secrets as s\n", encoding="utf-8")
    assert _credential_imports(offender), "the scanner missed an aliased import"


def test_the_scanner_detects_the_package_level_form(tmp_path):
    offender = tmp_path / "pkg.py"
    offender.write_text("from core.connectors import secrets\n", encoding="utf-8")
    assert _credential_imports(offender), "the scanner missed a package-level import"


def test_the_scanner_does_not_flag_an_innocent_module(tmp_path):
    """A scanner that flags everything is as useless as one that flags nothing."""
    fine = tmp_path / "fine.py"
    fine.write_text(
        "from core.connectors.taint import tainted\n"
        "from core.interfaces.connector_gateway import ConnectorCall\n",
        encoding="utf-8",
    )
    assert _credential_imports(fine) == []


# ── B3: the same fence, around key material ──────────────────────────────────


@pytest.mark.parametrize(
    "line",
    [
        "from core.connectors.keyring import dek_for",
        "from core.connectors.keyring import active_dek",
        "from core.connectors import keyring",
        "import core.connectors.keyring as k",
        "from core.connectors.secrets import _unwrap_dek",
        "from core.connectors.secrets import generate_dek",
        "from core.connectors.secrets import wrap_dek",
        "from core.connectors.secrets import seal_with",
    ],
)
def test_the_scanner_detects_a_planted_key_material_import(tmp_path, line):
    """Each way of reaching a DEK, planted and caught.

    A DEK opens every credential of one tenant and version. Fencing the token
    but not the key that decrypts it would have been a boundary with a door
    beside it.
    """
    offender = tmp_path / "leaky_key.py"
    offender.write_text(line + "\n", encoding="utf-8")
    assert _credential_imports(offender), f"the scanner missed: {line}"


def test_destroying_a_key_is_deliberately_not_an_offence(tmp_path):
    """`erase_keys` must be able to import `destroy_keys`.

    Destroying a key is the opposite of reading one: it takes the material out
    of reach permanently and returns nothing that could open anything. If this
    were refused, crypto-shredding could not be wired into account deletion at
    all — so the exception is asserted rather than left to whoever next reads
    the allow-list to infer.
    """
    fine = tmp_path / "eraser.py"
    fine.write_text(
        "from core.connectors.keyring import destroy_keys\n"
        "from core.connectors.keyring import inventory\n",
        encoding="utf-8",
    )
    assert _credential_imports(fine) == []


@pytest.mark.parametrize(
    "relative",
    [
        # Destroys keys during account erasure.
        "core/privacy/erasure.py",
        # Rotates them. Caught by this very test on its first run, importing
        # `from core.connectors import keyring` — which hands the importer
        # `keyring.dek_for` and is therefore refused. It now imports
        # `rotate_tenant` by name.
        "scripts/rotate_connector_keys.py",
    ],
)
def test_the_modules_using_the_narrow_exception_stay_inside_it(relative):
    """The exception is "may destroy or rotate", not "may mention the module".

    Checked per file rather than left to the parametrised sweep, because that
    sweep would pass a module reaching for `dek_for` if this file had allowed
    the package — and these two are the only production modules outside
    `core/connectors/` that touch the keyring at all.
    """
    module = IRA_ROOT / relative
    assert module.is_file(), f"{relative} moved; update this test"
    assert _credential_imports(module) == [], (
        f"{relative} now reaches key material. It may import the specific "
        f"lifecycle functions it needs — `destroy_keys`, `rotate_tenant`, "
        f"`inventory` — and nothing that returns a key or a token."
    )


# ── The shape of the boundary itself ─────────────────────────────────────────


def test_there_is_no_function_that_returns_a_token():
    """`use_secret(sealed, fn)` takes a callback on purpose.

    A function returning a credential is one whose return value ends up in a
    log, a traceback, a metrics label or a model's context. Adding a
    `get_token()` here would undo the design, so its absence is asserted.
    """
    from core.connectors import secrets

    for banned in ("get_token", "decrypt", "reveal", "plaintext", "token_for"):
        assert not hasattr(secrets, banned), (
            f"core.connectors.secrets.{banned} exists. The boundary is a "
            f"callback (`use_secret`) precisely so no credential is ever a "
            f"return value."
        )


def test_the_raw_decrypt_is_private_and_unexported():
    from core.connectors import secrets

    assert "_open_secret" not in secrets.__all__
    assert hasattr(secrets, "_open_secret"), "the private decrypt was renamed"


def test_a_missing_key_refuses_rather_than_storing_plaintext():
    """The brief lists "missing encryption key" among the fail-closed
    conditions. Refusing to connect an account is correct; the alternative is a
    deployment quietly holding refresh tokens in the clear."""
    from core.connectors.secrets import MissingEncryptionKey, seal

    with pytest.raises(MissingEncryptionKey) as exc:
        seal("1//a-refresh-token", env={})
    message = str(exc.value)
    assert "IRA_CONNECTOR_ENCRYPTION_KEY" in message
    assert "clear" in message or "plaintext" in message


def test_the_connector_key_is_not_the_session_signing_key():
    """Reusing IRA_SECRET_KEY for encryption is a classic quiet mistake: it
    widens the blast radius of either key leaking, and rotating one silently
    breaks the other."""
    from core.connectors.secrets import KEY_ENV

    assert KEY_ENV != "IRA_SECRET_KEY"
    assert "CONNECTOR" in KEY_ENV
