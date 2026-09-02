# cc-search-chats

Last verified: 2026-09-02

## Purpose

`cc-search-chats` recovers context from native Claude Code and Codex JSONL
sessions. PostgreSQL is a rebuildable search projection; the native logs remain
the content authority and are always read-only.

## Tech Stack

- Python 3.14+, packaged with uv/hatchling
- PostgreSQL 18 full-text search and psycopg
- pgvector plus optional PyTorch/Transformers semantic runtime
- pytest, Ruff, ty, complexipy, vulture, and pre-commit
- Claude Code and Codex plugin wrappers over the independently installed CLI

## Commands

- `uv run --frozen pytest -q -m 'not postgresql'` — non-PostgreSQL tests (pre-commit)
- `uv run --frozen pytest -q -m postgresql` — disposable PostgreSQL 18 tests (pre-push)
- `uv run --frozen ruff check src tests scripts` — lint
- `uv run --frozen ruff format --check src tests scripts` — formatting gate
- `uv run --frozen ty check src tests scripts` — type check (ty is pinned exactly; bump
  deliberately)
- `uv run --frozen complexipy --failed --plain` — cognitive-complexity ratchet against
  `complexipy-snapshot.json`; no function may exceed 15 unless already recorded, and
  no recorded function may worsen. A passing run rewrites the snapshot, so an
  improvement lowers the baseline; stage the rewritten file with the change
- `uv run --frozen vulture` — high-confidence dead-code gate
- `uv run --frozen pre-commit validate-config` — hook configuration; `pre-commit install`
  arms the pre-commit and pre-push stages in a fresh clone
- `uv run --frozen cc-search-chats --help` — CLI surface

PostgreSQL tests create isolated schemas in the repository's disposable cluster.
They do not use the operator's production database.

## CLI Contract

- PostgreSQL is the default backend. `search` opens the currently selected
  coherent corpus in a repeatable-read snapshot inside a five-second
  invocation-to-answer deadline. It never indexes, launches, joins, or waits
  for index work. `index`, run by a person, an agent, or the nightly timer, is
  the only builder and always publishes literal and semantic state together
  (ADR 0002). Search never migrates or performs maintenance in its own process.
- Hybrid search is the default. `--literal` avoids the model. Default search
  returns visible primary-session prose; `--agents` includes agent and unknown
  sessions; `--literal --tools` includes tool name/input/output rows;
  `--exhaustive` requires literal mode and returns deterministic complete
  occurrences. No supported mode exposes reasoning, system/developer
  instructions, injected context, or unrecognised record shapes.
- Ranked `--limit` is 1–200. Hybrid ranking uses bounded literal and semantic
  components with exact reciprocal-rank-fusion arithmetic. Query embedding is
  a bounded, reaped child; timeout or unavailable semantic state returns the
  literal answer with explicit degradation.
- `resolve` verifies exact `ccchat:v1:` locators against native source bytes.
  `--reference-only` retains verified identity and coordinates while omitting
  text.
- `events` reads the selected corpus generation inside required half-open,
  timezone-aware bounds. It exports canonical human-message timestamps and
  provenance without message bodies, plus positive retained, excluded, and
  unresolved population counts.
- PostgreSQL JSON stdout uses schema version 3. Every command returns an object
  with `command`, `status`, `coverage`, `refresh`, `semantic`, `indexed_at`,
  `corpus_age_ms`, and `warnings`. Public generation identity is
  `refresh.corpus_generation`; semantic identity is
  `semantic.semantic_build` plus `semantic.corpus_generation`; events use
  `source_corpus_generation`.
  JSON/non-TTY progress is ordered NDJSON on stderr and ends with one terminal
  event; stdout remains one JSON document.
- Schema migration is explicit `index --migrate` maintenance. Other PostgreSQL
  commands report `maintenance_required` without changing schema.

## Source Roots and Isolation

Default roots include standard and isolated Ponytail session corpora:

- `~/.claude/projects`
- `~/.codex/sessions`
- `~/.claude-ponytail/projects` when present
- `~/.codex-ponytail/sessions` when present

`CC_SEARCH_CLAUDE_ROOTS` and `CC_SEARCH_CODEX_ROOTS` replace the corresponding
collections using the platform path separator. Singular variables remain
one-root migration compatibility. Discovery traverses session roots only; it
does not read isolated configuration, credentials, plugins, skills, caches,
locks, or runtime state.

## Architecture

- `src/cc_search_chats/core/` — provider-neutral identities and pure parsing/
  query transformations
- `src/cc_search_chats/providers/` — native-source discovery and provider schema
  adapters
- `src/cc_search_chats/storage/postgresql/` — checksummed migrations, canonical
  current rows, incremental refresh, bounded event export, exact resolution,
  and semantic retrieval
- `src/cc_search_chats/semantic/` — local-only model preflight and embedding
- `src/cc_search_chats/cli.py` — imperative shell and v3 output/progress contract
- `skills/search-chat/` and `commands/search-chat.md` — agent consumers
- `docs/architecture/database.md` — data ownership and relational invariants
- `docs/runbooks/postgresql-index-maintenance.md` — migration, recovery, and
  separately authorized prune procedure

## Invariants

- Native session logs are never written or locked.
- Equal native identities share one canonical message; physical occurrences are
  retained as aliases. Conflicting content for one identity aborts publication.
- An unchanged refresh reads no JSONL content bytes and creates no corpus
  generation. Same-device/inode growth reads only after the last complete-record
  watermark; truncation, replacement, same-size modification, or parser-version
  change reparses from byte zero.
- `message_current` and `physical_alias_current` contain only current canonical
  state. Generations retain bounded status/recovery metadata, not corpus copies.
- One embedding value exists per profile/input digest; current mappings reuse
  it. `corpus_state` selects only a completed corpus generation with its own
  completed semantic build; candidate failure leaves the previous coherent
  selection current.
- Applied SQL migration bytes are immutable. Add a new ordered migration instead
  of editing one whose checksum may already be recorded.
- Runtime commands use configured package/model caches and local model files.
  They do not download models or redirect caches.
- Legacy snapshot relations remain quarantined until the exact installed commit,
  positive four-corpus UAT, semantic completeness, a matching fresh prune plan,
  and separate human prune authority all exist.

## Boundaries

- Installation, production migration, production UAT, pruning, pushing, and
  publication are separate release actions.
- Database/tablespace provisioning, backup, mount validation, and credentials are
  operator responsibilities; the application creates only its schema objects.
- Message attribution, receipt correlation, rendered archives, summaries, and
  project-note authorship are deferred and must not become search dependencies.
- The legacy `CC_SEARCH_DB_PATH` backend exists for compatibility tests; do not
  extend it as the production architecture.
