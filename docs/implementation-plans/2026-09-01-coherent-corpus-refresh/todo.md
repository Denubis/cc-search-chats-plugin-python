# Coherent corpus refresh pending work

Move completed work and its exact evidence to `worklog.md`; remove it from this
file rather than checking it off here.

## Human-authorized release and acceptance gates

- Obtain separate authority before any commit, production installation,
  migration 7, full index, or four-root UAT.
- During authorized production UAT, measure the external search interval,
  queue materialization, and batch-fetch p95 against the recorded falsifiers;
  obtain explicit human acceptance of usefulness and age/update reporting.
- Keep the nightly timer disabled. Legacy pruning remains a later, separately
  planned and authorized operation.
