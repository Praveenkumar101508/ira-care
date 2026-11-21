# Rollback verification scripts

Every migration in `postgres/` that ships a `down` script has one of these. Each
proves the same two things on a **disposable database of its own**: that the
rollback *reverses* what the migration did, and that it *refuses* when reversing
would destroy something it cannot reconstruct. A rollback that has only ever
been reasoned about is a rollback plan, not a rollback.

CI runs these files. So can you:

```bash
# any database the migration chain can be applied to; the per-migration
# scripts create and drop their own, so this only needs a reachable server
export PGPASSWORD=postgres
bash postgres/rollback/tests/migration-056.sh
```

That takes about two seconds. Until this directory existed the same check was
5,354 lines of inline YAML inside one CI job, which meant the only way to run it
was to push and wait nineteen minutes.

## The two kinds of script here

**`migration-NNN.sh` — self-contained.** Creates its own database, applies the
chain, exercises `rollback/NNN.sql`, drops the database. No preconditions, safe
to run in any order, safe to run repeatedly.

**`chain-rollback.sh` and `chain-recompose.sh` — not self-contained.** They
operate on the shared `iratest` database and they run as a pair, in that order:

- `chain-rollback.sh` takes the whole chain down newest-layer-first and puts it
  back, proving the layers compose.
- `chain-recompose.sh` then proves the recomposed schema is identical to a fresh
  one, and that the chain-era boundary did not move.

They require `iratest` to be **populated first** — the tenancy suites create the
multi-tenant fixture data several guards refuse against, and without it those
guards have nothing to refuse and the script fails reporting a rollback that
"should have refused". That is a real precondition, not a flaky test:

```bash
cd ira && python -m pytest tests/tenancy/    # ~3,000 tests, populates iratest
cd .. && bash postgres/rollback/tests/chain-rollback.sh
bash postgres/rollback/tests/chain-recompose.sh
```

`chain-rollback.sh` hands the chain-era boundary to `chain-recompose.sh`. Inside
CI that travels through `$GITHUB_ENV`; outside CI it goes through `$CHAIN_STATE`,
which defaults to `${TMPDIR:-/tmp}/ira-chain-state.env`. Both are written, so the
runner behaviour is unchanged.

## Adding one

When a migration gains a `down` script, add the matching file here and a step to
the `tenant-isolation` job that runs it. The job's own history is the argument
for not skipping this: the chain cycle went stale **four** times, and every one
was caught by the first CI run against a branch carrying the new migration
rather than by review.
