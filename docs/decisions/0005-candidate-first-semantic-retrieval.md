# 0005. Bound exact semantic retrieval before loading payloads

Status: Accepted
Date: 2026-09-03

## Context

The first production-scale semantic retrieval after removal of the answer
deadline exceeded the application's 64 MB transaction-local temporary-file
limit. The shipped query joined every eligible chunk to message prose and then
used a full-set window sort to choose each message's best chunk. The measured
failure and candidate-first probes are recorded in Prompt 15 and
`codex-prompts/context/15-explain-probes.out`.

Pgvector's `<#>` operator returns negative inner product, so exact highest-inner-
product retrieval orders that value ascending. A bounded query also needs a
total order: PostgreSQL may choose an implementation-dependent subset when rows
at a `LIMIT` boundary are equal under every specified ordering expression.

During Prompt 15, the supervisor ruled that candidate ordering must be total and
consistent with final message ordering: distance, provider, source session,
logical message, content class, then chunk ordinal.

## Decision

Semantic retrieval first selects only message keys, chunk ordinal, and score for
the top `max(2000, 8 * limit)` eligible chunks. It orders by negative inner
product ascending, then by the ruled message-key and chunk-ordinal tie-break.
`DISTINCT ON` reduces that bounded set to the best-scoring, lowest-ordinal chunk
per message. Only then does retrieval join `message_current` for payload columns
and apply the final score and message-key ordering.

When the initial candidate set reaches its bound but contains fewer than
`limit` messages, retrieval retries once at eight times the bound, capped at
64,000 chunks. A transaction-local 256 MB `work_mem` applies only to semantic
retrieval. The 64 MB temporary-file limit and cluster configuration do not
change, and no approximate index is introduced.

## Consequences

The top-K chunk boundary is deterministic. A message absent from it has a best
chunk that either scores worse than every included chunk or, at an exact score
tie, follows the boundary under the same message-key order. Once K contains at
least `limit` messages, the returned first `limit` messages therefore have their
true best scores and deterministic exact order. The bounded retry mitigates
candidate starvation by messages with many highly ranked chunks; if the retry
is still short, semantic retrieval returns the available bounded candidates as
the hybrid contract permits.

Message prose no longer participates in the full candidate sort. The read queue
serializes application reads, so only one queued semantic transaction receives
the larger transaction-local `work_mem` setting at a time.
