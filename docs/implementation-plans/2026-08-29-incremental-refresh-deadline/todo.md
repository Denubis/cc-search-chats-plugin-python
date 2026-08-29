# Incremental refresh and bounded search repair todo

## Outcome 1: repeat failures become metadata-only no-ops

- [x] Add red migration and refresh tests for durable failed observations,
      retry invalidation/backoff, force retry, and truthful attempted-byte
      metrics.
- [x] Implement migration 6 and the minimal refresh behavior; pass focused
      PostgreSQL tests.

## Outcome 2: bounded search and background maintenance

- [x] Add red CLI/deadline/cooldown/systemd tests.
- [x] Implement literal-first bounded search and durable background refresh;
      pass focused CLI, PostgreSQL journey, concurrency, and unit-file tests.

## Outcome 3: verification and release

- [x] Reconcile living documentation and explicit migration ownership.
- [x] Pass complete mechanical and falsification-first sanity verification.
- [x] Install the initial exact candidate and migrate production to version 6
      without pruning; keep the timer disabled.
- [x] Reproduce and repair production provider-compatibility gaps, then replay
      the repaired parser read-only across the complete four-root corpus.
- [ ] Commit and install the exact repaired candidate, rerun literal and semantic
      production refresh, and complete positive four-corpus UAT with the 39
      corrupt native sources reported as explicit partial coverage.
- [ ] Obtain human UAT acceptance before final release normalization or timer
      enablement.
