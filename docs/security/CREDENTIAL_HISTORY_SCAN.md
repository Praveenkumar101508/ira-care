# Credential history scan (D-004 resolved, round 9)

`core/runtime/published_credentials.py` makes IRA refuse to start while a signing key, admin
password or webhook secret holds a value that was once committed to a repository: everyone
with a clone has such a value. The table of digests in that module is only as good as the
history it was built from, so a scanner must be able to rebuild it from the evidence, and a
test must check the two agree. The scanner that did this in the source project was never
carried over (D-004); this is its IRA CARE equivalent.

## What `scripts/security/published_credential_digests.py` does

```bash
python3 scripts/security/published_credential_digests.py out.json                      # this history alone
python3 scripts/security/published_credential_digests.py out.json --include-inherited  # the full table
python3 scripts/security/published_credential_digests.py out.json --repo /some/clone
```

- It lists every commit on **every ref** (`git log --all`) that added or changed an
  environment file (`.env`, `.env.*`, `*.env`, `*.env.example`), reads each version with
  `git show`, and takes the values of the six names the table covers (`IRA_SECRET_KEY`,
  `IRA_ADMIN_PASSWORD`, `WEBHOOK_SECRET`, `IRA_CORTEX_KEY`, `IRA_CANARY_TOKEN`,
  `IRA_CANARY_USERNAME`). Deleted versions and side branches are included: a value that was
  removed later, or was only ever on a branch, is still in every clone.
- A value is **published** when it is not empty and not a placeholder in the sense of
  `core.runtime.environment.is_placeholder`, the same rule the start-up validation applies
  (`CHANGE_ME…`, `change-me`, `<your-…>`, and the CI test values `ci-test-secret-key`,
  `ci-test-password`, `ci-test`). Quotes and trailing comments are stripped.
- Each distinct published value is digested with `published_credentials.digest(name, value)`
  (scrypt, salted with the name), and the digests go to the JSON file. The terminal gets
  provenance only: commit ids, paths, names and counts. Neither a value, nor part of one, nor a
  digest is printed.
- `--include-inherited` merges the module's current table into the output. IRA CARE was split
  from the IRA project on 21 November 2026 (`abbb7c4`) with placeholder environment files; the
  six digests the module ships were computed from the source project's history (July 2026) and
  are provenance this repository inherits. Without the flag the output is what this history
  publishes; with it, the full table the module must hold.

## What the test checks

`supracloud-jarvis/ira/tests/test_published_credentials.py`:

- On a **synthetic repository** built in a temporary directory: a planted signing key in a
  version that was later replaced, a planted admin password that was later emptied, and a
  planted webhook secret (quoted, with a trailing comment) committed and then deleted on a side
  branch are all found; placeholders and names outside the six are not; the planted values and
  their digests never appear on stdout or stderr; a history of placeholders alone publishes
  nothing; `--include-inherited` is a superset of the module's table. The comparison helper is
  shown to fail when a published value is missing from the table (the negative control: the
  planted values are, by design, not in the table).
- On **this repository**, on a full clone only (it skips on a shallow clone, which is what CI
  checks out): the scanner runs, prints no digest, every digest it finds is already in the
  refusal table (nothing this history publishes is unrefused), and the scan merged with the
  inherited provenance equals the table exactly.

## What the scan found on IRA CARE's history (26 November 2026, all refs, 142 commits)

| | |
|---|---|
| Environment-file versions scanned | 12, in 6 files (`deploy/.env.example` ×6, `supracloud-jarvis/.env.example`, `.env.cloud.example`, `.env.test.example`, `ira/config/cortex.env.example`, `ira/config/model_selection.env.example` ×2) |
| Versions with a published value for one of the six | 0 |
| Distinct published values | 0 for every name |

So the six digests in `published_credentials.py` are entirely inherited from the source
project; this repository has published nothing of its own. The test keeps it that way: a
future commit of a real value to an example file fails it on the next full-clone run (and the
secret scan, `scripts/ci/gitleaks.sh`, on every pull request).

## Limits

- The scanner reads environment files only. A credential pasted into a document, a test or a
  script is the job of the secret scan (gitleaks) and `tests/readiness/test_public_repository.py`.
- CI's shallow checkout cannot run the history comparison; it runs on a full local clone (the
  integrated verification does one) and in the synthetic-repository tests everywhere.
- `--include-inherited` trusts the module's table for the source project's values; that
  project's own history is not read here and must not be (it is private).
