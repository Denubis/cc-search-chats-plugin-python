# Exact-resolution query-plan regression

Date: 2026-08-19 (Australia/Sydney)

## Incident mechanism

The previous exact-locator query joined every current-revision message to its
physical aliases, filtered with `canonical_locator = $1 OR alias.locator = $1`,
then applied `SELECT DISTINCT` to the complete message row. Neither locator had
a revision-scoped lookup index. Repeated CLI processes therefore multiplied a
wide scan, join, sort, and deduplication plan.

On the disposable PostgreSQL 18 fixture with 20,000 messages, 20,000 aliases,
`work_mem = 64kB`, and prose widened to make spills observable, the old executed
plan:

- scanned both 20,000-row relations and discarded 19,999 joined rows;
- touched 1,468 shared buffers;
- wrote 871 temporary blocks on the alias lookup; and
- took about 15.46 ms for one locator before process and connection overhead.

This is a bounded reproduction of the production mechanism, not a production
throughput benchmark.

## Corrected plan

The migration adds `(revision_id, canonical_locator)` and
`(revision_id, locator)` indexes. Resolution now unnests an ordered locator
array, runs separate indexed canonical and alias branches, deduplicates only
narrow logical identity keys, and fetches message bodies by the message primary
key.

`auto_explain` captured the query actually executed by the resolver with
analysis, buffers, and JSON formatting enabled. The same fixture produced:

| Locator path | Required index used | Relation sequential scans | Temporary blocks written | Actual total time |
|---|---|---:|---:|---:|
| canonical | `message_revision_canonical_locator_idx` | 0 | 0 | 0.045 ms |
| physical alias | `physical_alias_revision_locator_idx` | 0 | 0 | 0.053 ms |

Both branches also fetched the final row through `message_pkey`. The regression
test fails unless the named locator index appears positively in the executed
plan, either large relation is sequentially scanned, or any plan node writes a
temporary block. It separately proves that duplicate physical aliases collapse
to one logical message.

The roughly 300-fold fixture latency reduction is directional evidence only;
the durable claim is the change in plan shape and the elimination of temporary
I/O. Production-corpus load testing remains intentionally unrun.
