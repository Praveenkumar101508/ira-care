"""
ira/trust — Trust Ledger and proof-carrying actions (PR 73).

  ledger.py      hash-chained, append-only action records over the
                 trust_ledger table (migration 020)
  journal.py     what may run when the ledger cannot be written (P1-09),
                 and the owner-only, HMAC-chained fallback journal that
                 holds the two recovery actions' records until replay
  proof_card.py  the owner-facing explanation of one action: what, why
                 allowed, data touched, what leaves the device, risks,
                 reversal, verification, approver

Honesty boundary (threat model T7): the chain detects tampering only when
the head hash is anchored outside this database. export_head() exists for
exactly that; nothing here is described as "cryptographically secure".
"""
