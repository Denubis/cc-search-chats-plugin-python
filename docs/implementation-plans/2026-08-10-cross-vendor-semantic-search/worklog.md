# Cross-vendor semantic search worklog

## 2026-08-21 — planning baseline

- Recovered and audited the current request against the native Codex session.
- Accepted design:
  `docs/design-plans/2026-08-10-cross-vendor-semantic-search.md`.
- Working tree: `main` at baseline commit
  `fb76d17ee55c55681dca7043bbefc50b8e7223a2`, package version 2.0.4.
- Initial dirty state for this delivery: accepted design documentation only; no
  implementation, commit, installation, production migration, or prune.
- Existing deterministic baseline before design edits: 581 pytest tests passed;
  Ruff lint, Ruff format check, and ty passed.
- Deployed database evidence used to constrain the design:
  approximately 130 GB total; `message` approximately 53 GB with 19.55 million
  estimated rows but 1,651,678 rows in selected corpus revision 14;
  `physical_alias` approximately 16 GB with 19.97 million estimated rows but
  1,682,012 selected rows; `message_embedding` approximately 13 GB with
  2,413,617 rows across ten semantic revisions. Selected semantic revision 9
  has 270,012 vectors for corpus revision 13; revision 10 targets current corpus
  revision 14 but has zero vectors.
- Native input evidence: standard Claude/Codex roots plus present isolated
  Ponytail session roots are in scope. Only their session corpora are readable;
  Ponytail configuration/runtime isolation is preserved.
- Explicit exclusion: message attribution, receipt correlation, authorship
  classification, and their quarantine schema.
- The former `delivery.md` was retired because it prescribed retained full
  revisions and receipt-evidence work, both contrary to the accepted design.
- Current next gate: human assent to implement directly in the current `main`
  working tree with changes left uncommitted.

## 2026-08-22 — execution authorized

- The human instructed `commit, implement`, authorizing a planning-state commit
  followed by implementation in the current `main` working tree.
- No installation, production migration, production prune, push, or publication
  was authorized.
