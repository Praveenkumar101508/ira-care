"""Cryptographic key management.

Separate from `core/connectors/secrets.py` on purpose: that module owns the
primitives and the credential boundary test fences it. This package owns the
question of WHERE key material comes from, which is configuration rather than
cryptography.
"""
