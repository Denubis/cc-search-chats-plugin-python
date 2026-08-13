# Cross-vendor semantic search delivery

The accepted design is the source of product and operational obligations. This
checklist records only the shortest executable path to a consumer.

## 1. PostgreSQL corpus

- Connect through one required DSN without logging credentials.
- Refuse the wrong server, database, role, schema, extension, or tablespace.
- Apply ordered, transactional SQL migrations owned by the application owner.
- Store provider-qualified sessions, logical messages, physical aliases,
  source watermarks, diagnostics, and one current literal revision.
- Stream the existing Claude and Codex adapters into an unselected revision.
- Promote only a complete revision; preserve the previous revision on failure.
- Expose literal search, list, extract, context, and exact identity resolution.

Done when the current CLI uses PostgreSQL for both providers and the SQLite
implementation has no remaining consumer.

## 2. Semantic search

- Keep the model and Python vector adapter in an optional semantic extra.
- Store immutable model/chunker profiles and reusable 1024-dimensional vectors.
- Embed a complete staged semantic revision and promote it atomically.
- Query PostgreSQL FTS and exact pgvector candidates, then fuse ranks with RRF.
- Fail hybrid search clearly when its semantic revision is unavailable or stale;
  explicit literal search remains available.
- Benchmark the real corpus before adding HNSW or IVFFlat.

Done when a natural-language query returns useful Claude and Codex results and
failure cannot silently serve mismatched literal/vector revisions.

## 3. Receipt evidence

- Read only the producer-owned published PostgreSQL contract views.
- Verify the published contract version before consuming evidence.
- Correlate provider, session, repository/cwd, normalized digest, lengths,
  confirmed outcome, and causal time bounds.
- Attribute only mutually unique matches; ambiguity remains unknown.
- Native-positive provenance takes precedence.
- Never add a receipt writer or producer migration to this repository.

Done when receipt evidence can upgrade one indexed message without granting the
search role access to producer tables or write functions.

## 4. Cutover and UAT

- Run ordinary unit tests plus fixture-backed PostgreSQL, pgvector, refresh,
  retrieval, and receipt-correlation behaviors.
- Run exploratory cross-vendor searches against the real read-only native roots.
- Record sanitized usage notes in `docs/uat/cross-vendor-search-wip.md`.
- Provision production storage explicitly; application commands never create or
  drop roles, databases, extensions, or tablespaces.
- Remove SQLite and duplicated tests only after every CLI consumer has moved.

Done when another agent can use the shipped CLI for cross-vendor semantic search
and exact follow-up without knowing provider file layouts.
