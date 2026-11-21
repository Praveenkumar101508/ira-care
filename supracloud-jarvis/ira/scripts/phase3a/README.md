# Phase 3A tooling

Regenerates the tenancy classification evidence. Committed so the artefacts are
reproducible rather than one-off output nobody can re-derive.

## Order

```bash
# 1. Fresh database, all migrations, non-superuser app role.
#    See docs/cloud/SESSION_HANDOFF.md for the full sequence.

# 2. Schema snapshot (reads the DSN from the environment; never writes it out)
PG3A_DSN="postgresql://postgres@127.0.0.1:55433/ira3a" \
  python3 introspect_schema.py ../../../../artifacts/cloud/phase-3a/database-schema.json
sha256sum ../../../../artifacts/cloud/phase-3a/database-schema.json \
  > ../../../../artifacts/cloud/phase-3a/database-schema.sha256

# 3. Application access map (no database needed)
python3 build_access_map.py ../../../../artifacts/cloud/phase-3a/application-access-map.json

# 4. Classification manifest — the authoritative output
python3 build_manifest.py

# 5. Generated Markdown
python3 generate_docs_1.py && python3 generate_docs_2.py && python3 generate_docs_3.py
```

## Rules

- **The JSON manifest is the source of truth.** The Markdown is generated from
  it. Editing the Markdown by hand is how the two drift; edit the generator.
- `ira/tests/tenancy/test_classification_manifest.py` enforces the manifest, and
  compares it against the live schema when a DSN is available.
- **No credential is written to any artefact.** The DSN is read from the
  environment. The only credential-shaped strings in the snapshot are the column
  name `password_hash` and a CHECK-constraint literal.

## A note on booleans

`introspect_schema.py` has a `truthy()` helper that accepts both `t`/`f` and
`true`/`false`. This is not defensive padding: the first version compared
`boolean::text` output against psql's raw `t`, so **every RLS flag was recorded
as false**. The manifest test caught it by comparing against the live schema.
