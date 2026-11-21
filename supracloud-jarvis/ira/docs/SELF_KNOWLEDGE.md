# Self-Knowledge RAG

IRA answering questions about **the owner's own work** — CV, project write-ups,
and this repository's documentation — from indexed source material, with
citations, and with a refusal when the corpus doesn't cover the question.

## Why it is separate from memory

Self-knowledge chunks are written to the same `memory_embeddings` table as the
rest of memory, but under their own namespace:

| field         | value            |
| ------------- | ---------------- |
| `user_id`     | `self_knowledge` |
| `source_type` | `self_knowledge` |

`memory.store.retrieve()` filters by `user_id`, and the Memory Vault lists only
`user_id = 'owner'`, so the two corpora are invisible to each other. The
isolation is covered by tests in `tests/test_self_knowledge.py`.

## The corpus

Drop files into `ira/data/self_knowledge/` (override with
`SELF_KNOWLEDGE_DIR`). Supported: `.md`, `.markdown`, `.txt`, `.rst`, and `.pdf`
(PDF text extraction needs `pypdf`; without it PDFs are skipped with a warning,
never a crash). The repository's own `README.md`, `ARCHITECTURE.md` and
`STATUS.md` are indexed too so IRA can explain itself.

Chunking is ~500 tokens with 50-token overlap (`SELF_KNOWLEDGE_CHUNK_TOKENS` /
`SELF_KNOWLEDGE_CHUNK_OVERLAP`), measured in words at ~0.75 words/token so the
path stays tokenizer-free.

Reindexing is a **full replace** of the namespace, so renames and deletions on
disk never leave orphan chunks behind.

## Grounding

`memory.self_knowledge.ask()`:

1. retrieves from the self-knowledge namespace only, applying a cosine floor
   (`SELF_KNOWLEDGE_MIN_SIMILARITY`) and, when the reranker is enabled, a
   cross-encoder floor (`SELF_KNOWLEDGE_MIN_RERANK`, default 0.1: the reranker
   returns a probability in [0, 1], and unrelated passages score ~0.00);
2. **if nothing clears the floors, the model is never called** — the answer is
   the literal refusal `I don't have that in my records.` There is no
   general-knowledge fallback on this path;
3. otherwise builds a strict prompt (context is *data*, never instructions) and
   returns the answer plus every chunk used, for a citation panel.

The path is local-only: `use_deep=False`, so it never escalates to an external
provider.

## API

```
POST /api/v1/self-knowledge/ask       {"question": "...", "top_k": 6}
POST /api/v1/self-knowledge/reindex   {"corpus_dir": null, "include_repo_docs": true}
GET  /api/v1/self-knowledge/status
```

`ask` returns `{answer, grounded, sources[], refusal_reason}`.

## CLI

```
ira self-knowledge reindex [--corpus-dir PATH] [--tenant-id UUID]
ira self-knowledge ask "what did the IBM RAG project achieve?" [--tenant-id UUID]
```

Both run against the default workspace of the private install, or of the tenant
`--tenant-id` names, using the database configured in `.env`.
