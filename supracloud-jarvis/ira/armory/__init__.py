"""
ira/armory — the Armory Vault: an encrypted, owner-gated cabinet of security tools.

The Armory keeps a catalogue of cybersecurity tools (Kali toolchain, Tor, Nmap,
Metasploit, …) sealed with AES-256-GCM behind a Master Key that only the owner
knows. Nothing runs a tool except through the gatekeeper, and every decision is
fail-closed.

Module map:
  vault_encryption.py  PBKDF2 key verification (stdlib) + AES-256-GCM body cipher
  vault.py             the on-disk sealed vault; decrypt-to-RAM open path
  session.py           in-RAM unlock state with idle + task-completion auto-lock
  manifest.py          tool catalogue + injection-proof argv construction
  whitelist.py         authorized-target scope (fail-closed)
  docker_manager.py    ephemeral, network-gated (Tor killswitch) containers
  gatekeeper.py        the one chokepoint: unlock/lock/preview/execute pipeline
  vault_guard.py       FastAPI dependency that gates tool routes on unlock state

Integration points (existing IRA systems this reuses, never re-implements):
  * trust/ledger.py    every vault action is hash-chained into the Trust Ledger
  * actions/shadow_world.py  tool commands are simulated before execution
  * security/owner_gate.py   armory intents are owner-only
  * config.py          IRA_ALLOW_EXTERNAL_API governs external egress
"""

__all__: list[str] = []
