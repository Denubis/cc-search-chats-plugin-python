# Pending work

## Direct delivery (authorised by the human on 2026-10-24: "3 yes")
- [ ] Integrate `unknown-shape-reporting` into `main` via
      `finishing-a-development-branch`; push and verify `main`.
- [ ] Deploy per `docs/runbooks/laptop-deployment.md`: install the exact
      `main` SHA, back up per ADR 0007, `index --migrate` to schema 12,
      baseline reparse (`index`), one-off `index --force-retry` for
      Antigravity rows blocked before the upgrade, decide the
      `CC_SEARCH_INDEX_IGNORE_UNKNOWN_RECORDS` line in `index.env`.
- [ ] UAT probes 2-4 on the deployed production index.
