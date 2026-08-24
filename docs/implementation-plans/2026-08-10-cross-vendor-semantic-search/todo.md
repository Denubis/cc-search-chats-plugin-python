# Cross-vendor semantic search todo

Only unresolved work remains here. Completed work and its exact evidence live in
`worklog.md`; an empty search result is not completion evidence.

## Outcome 6: verification and acceptance

- [ ] Obtain separate install, production-migration, and UAT authority before
      each gated action; implementation commit authority is already recorded.
- [ ] Validate the production candidate without pruning quarantine tables.
- [ ] Pass positive standard/Ponytail Claude/Codex UAT and obtain human acceptance.
- [ ] If separately authorized, prune superseded snapshots and repeat integrity,
      size, freshness, and positive UAT checks.
