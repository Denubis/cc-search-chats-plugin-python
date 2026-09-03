# ADR register

Index of architecture decision records for cc-search-chats. Each record lives in
this directory as `NNNN-<slug>.md`. A record stays `Proposed` until the human
rules on it, at which point it becomes `Accepted` and carries the date of the
ruling.

| ADR | Title | Status | Date |
|-----|-------|--------|------|
| [0001](0001-classify-index-failures-before-acting.md) | Classify index failures before acting on them | Accepted | 2026-07-31 |
| [0002](0002-explicit-search-modes-no-search-triggered-refresh.md) | Search never refreshes; the caller names the mode; the index stays joint | Accepted | 2026-09-02 |
| [0003](0003-native-record-policy.md) | Persist visible conversation and bounded tool metadata | Accepted | 2026-09-03 |
| [0004](0004-semantic-search-no-deadline-warm-window.md) | Semantic search has no deadline and reuses one short-lived warm model | Accepted | 2026-09-03 |
