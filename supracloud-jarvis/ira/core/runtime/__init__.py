"""Cloud runtime foundation — environment, logging, errors, timeouts, build info.

Phase 1 of the cloud transformation. Everything here is *additive*: the existing
application, model gateway, Trust Ledger and migration system are untouched, and
nothing in this package changes behaviour for an install that does not set
``IRA_ENVIRONMENT``.
"""
