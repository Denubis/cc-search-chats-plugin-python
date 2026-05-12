# Causal Analysis: `UNIQUE constraint failed: message.uuid` during reindex

Date: 2026-05-12
Investigator: Claude (claude-opus-4-7[1m])
Status: Fix applied; peer review complete; end-to-end verified on BJET

## Summary

`reindex_project` and `jit_reindex` crash with `sqlite3.IntegrityError: UNIQUE
constraint failed: message.uuid` when the JSONL stream contains any message
UUID that the in-pass `INSERT INTO message` has already inserted. The trigger
on this user's data is *intra-file* duplicate logging — the same logical
message rewritten into the same JSONL on a later Claude Code run (the two
records share uuid/parentUuid/sessionId/timestamp/content and differ only in
the writer's `version` and `diagnostics` fields, consistent with a resume or
replay by a newer CLI version). The bug report's instinct that resume causes
duplication is essentially right; only its "cross-file" framing is contradicted
by the evidence — across all 139 projects on this machine, no UUID appears in
more than one JSONL. The fix that addresses both intra-file (observed) and
cross-file (hypothetical but possible elsewhere) phenomena is to add a
conflict policy (`INSERT OR IGNORE`) to the message INSERT.

## Causal Chain

1. The `message` table declares `uuid TEXT PRIMARY KEY` (schema.sql:22), so
   any duplicate uuid value is a constraint violation.
2. `index_session` (storage/index.py:154) issues a bare
   `INSERT INTO message (uuid, ...) VALUES (...)` at line 246-260 with no
   `OR IGNORE` / `OR REPLACE` / `ON CONFLICT` clause.
3. `index_session` does delete the prior session row at line 167
   (`DELETE FROM session WHERE session_id = ?`), and the `ON DELETE CASCADE`
   foreign key on `message.session_id` (schema.sql:23) cascades through to
   wipe that session's messages. This makes *single-session re-index*
   idempotent but does nothing about UUIDs that arrive duplicated *within* the
   pending insert stream or that are owned by a different session_id already
   in the database.
4. Empirical observation: in the BJET project on this machine
   (`~/.claude/projects/-home-brian-people-Jodie-BJET-Phase1-Longi-MixedMethods-RegReport/`)
   one JSONL file (`2b93038b-546a-49e3-91d0-664ef738e0ac.jsonl`) contains 15
   message UUIDs that each appear **twice within the same file**. The two
   records are identical on uuid, parentUuid, sessionId, timestamp, and
   content; they differ only in `version` (the writer Claude Code CLI version
   — e.g. `2.1.119` vs `2.1.123`) and `diagnostics`. This pattern is
   consistent with Claude Code rewriting the same logical message into the
   same JSONL on a later run by a newer CLI version (resume or replay). The
   second `INSERT INTO message` in the same `index_session` call collides
   with the first on the PK.
5. `reindex_project` (line 286) and `jit_reindex` (line 316) both feed
   `index_session` from a sorted-by-mtime list, so the abort kills the entire
   reindex partway through.
6. `compact_event.uuid` is also `PRIMARY KEY` (schema.sql:40) with a bare
   INSERT statement spanning index.py:204-216 (the conflict-prone SQL string
   is on line 205) — the same vulnerability exists there. No duplicate
   compact_event UUIDs were observed in any project on this machine, so it
   has not been triggered in practice. Fixing it would be defense in depth,
   not correction of an observed failure.

## What the bug report got right and wrong

| Report claim | Verdict | Evidence |
|---|---|---|
| Bare INSERT at index.py:246 fails on duplicate UUIDs | Correct | Verified at index.py:246-260; reproduced crash on BJET |
| `message.uuid` is declared PRIMARY KEY / UNIQUE | Correct | schema.sql:22 — `uuid TEXT PRIMARY KEY` |
| `needs_reindex` (line 267) only checks mtime per session_id | Correct | Verified at index.py:267-283 |
| The bug is "purely cross-file" — within-file dedup is fine | **Partially wrong** | The repro on this machine is intra-file dupes (`2b93038b...jsonl` has 15 UUIDs twice each). The report's recommended `(session_id, uuid)` composite key would **not** fix the intra-file case. The underlying intuition that resume produces duplicates is right; the "cross-file" geometry is wrong — Claude Code rewrote the records into the same JSONL, not a new one. |
| Resumed/forked sessions cause cross-file UUID duplication | **Falsified for this machine** | Scanned all 139 projects on this machine: zero cross-file UUID overlap. The report's `jq ... \| sort \| uniq -c` command on `*.jsonl` does not distinguish intra-file from cross-file dupes, so its evidence does not actually establish the resume-fork-to-new-file mechanism. Cannot rule out the cross-file case on other machines. |
| `INSERT OR IGNORE` is the minimal fix | Plausible (semantics verified; not yet run end-to-end against BJET) | Fixes both intra-file and (hypothetical) cross-file cases; SQLite documents that `OR IGNORE` skips AFTER INSERT triggers on conflict, so no FTS5 or epoch_summary churn |
| Composite key `(session_id, uuid)` is the structural fix | **Insufficient alone** | Does not handle intra-file duplicates. Would need `INSERT OR IGNORE` on top of it anyway. |

## Evidence Grading

| # | Finding | Grade | Positive border | Negative border | Upgrade path |
|---|---------|-------|----------------|-----------------|--------------|
| 1 | The bare INSERT at index.py:246 is the failing statement | Demonstrated | Reproduced crash with same error message and stack frame | DB wipe + reindex without the fix still fails | — |
| 2 | Intra-file UUID duplication triggers the crash on real user data | Demonstrated | BJET file `2b93038b...jsonl` has 15 intra-file dupes; reindex on that project crashes | Removing those dupes (or applying `OR IGNORE`) makes the reindex succeed (negative border tested below in Phase 4) | — |
| 3 | Cross-file UUID duplication exists on this user's machine | Falsified | — | Scanned 139 projects, zero cross-file overlap | If user has other machines with resumed sessions, run the same per-file UUID extraction there |
| 4 | `INSERT OR IGNORE` resolves the crash | Demonstrated | Failing test reproduces the crash with the exact `IntegrityError` from the report; patch makes it pass; end-to-end re-run of `reindex_project` against BJET indexes all 12 sessions and 3038 messages with no error; the 2b93038b session's indexed message count equals its unique-UUID count (584=584) | Removing the `OR IGNORE` clause restores the crash on the same test | — |
| 5 | `compact_event` INSERT shares the same vulnerability | Possible | Same schema pattern (`uuid TEXT PRIMARY KEY`) and same bare INSERT idiom at index.py:204 | No duplicate compact_event UUIDs observed in the wild | Would need a project where Claude Code double-logs a compact_boundary record |

## Claim Verification

| # | Claim | Evidence | Falsification test | Result |
|---|-------|----------|---------------------|--------|
| 1 | `message.uuid` is PRIMARY KEY | schema.sql line 22 reads `uuid TEXT PRIMARY KEY` | Read the file | Verified |
| 2 | INSERT at index.py:246 has no conflict clause | index.py lines 246-260 contain `"INSERT INTO message "` followed by VALUES — no OR/ON CONFLICT | Read the file | Verified |
| 3 | Line 167 already deletes the session row before re-inserting | index.py:167 reads `conn.execute("DELETE FROM session WHERE session_id = ?", (sid,))` | Read the file | Verified |
| 4 | CASCADE removes child messages on session delete | schema.sql:23 declares `REFERENCES session(session_id) ON DELETE CASCADE` and pragmas enable foreign_keys | Read schema + pragmas | Verified |
| 5 | The reporter's `jq ... \| sort \| uniq -c` command conflates intra- and cross-file dupes | The command pipes all files through one stream; output cannot distinguish "uuid appears 2x in one file" from "uuid appears 1x in two files" | Re-read the command in the report | Verified |
| 6 | BJET project on this machine has intra-file dupes | `jq -r '...uuid' 2b93038b....jsonl \| sort \| uniq -c \| awk '$1>1'` returns 15 lines | Ran the command | Verified — 15 UUIDs each appear 2x in the same file |
| 7 | BJET project on this machine has zero cross-file dupes | For each file, deduped UUID set is computed; then UUIDs spanning 2+ files are counted | Ran per-file dedup followed by cross-file count | Verified — zero |
| 8 | No project on this machine has cross-file UUID dupes | Per-file dedup + cross-file count across all 139 project directories | Ran the scan over all projects | Verified — zero hits |
| 9 | `INSERT OR IGNORE` does not fire AFTER INSERT triggers when it ignores | SQLite documented behaviour | Tested with a small probe (planned in Phase 4 if needed) | Plausible — relying on documentation |
| 10 | Crash reproduces on BJET reindex | Ran `reindex_project(conn, '/home/brian/people/Jodie/BJET-...')` against a fresh DB | Stack trace matched: `IntegrityError: UNIQUE constraint failed: message.uuid` | Verified |

## Epistemic Boundary

- **Demonstrated:** crash is caused by bare INSERT meeting a duplicate uuid; intra-file duplication is sufficient to trigger it on real user data.
- **Plausible:** `INSERT OR IGNORE` resolves the crash without trigger churn (to be promoted to Demonstrated in Phase 4 by the failing-test-then-fix cycle).
- **Possible:** `compact_event` INSERT shares the same vulnerability — same schema pattern, but not observed in the wild on this machine.
- **Falsified:** the bug report's claim that resumed/forked sessions duplicate UUIDs across files. Not observed in any of 139 project directories on this machine. The diagnostic command quoted in the report cannot distinguish the two phenomena.
- **Not tested:** whether cross-file duplication ever occurs on other users' machines. The fix handles it regardless, so this gap does not block the patch.

## Fix Applied

Two `INSERT` statements changed to `INSERT OR IGNORE`:

1. `storage/index.py:247` — `INSERT INTO message` → `INSERT OR IGNORE INTO message` (corrective)
2. `storage/index.py:205` — `INSERT INTO compact_event` → `INSERT OR IGNORE INTO compact_event` (preventive)

Schema unchanged. Confirmed semantics: first-seen wins, identical or later duplicate UUIDs silently skipped, no trigger churn, fully idempotent on re-index. Verified end-to-end on BJET (12 sessions, 3038 messages, no crash; 2b93038b session indexes 584 messages = 584 unique UUIDs in the JSONL).

Regression tests added in `tests/test_indexing.py::TestDuplicateUuidHandling`:

- `test_intra_file_duplicate_message_uuids_does_not_crash` — fails without the patch with the exact bug-report stack trace
- `test_cross_session_duplicate_message_uuids_does_not_crash` — covers the bug report's hypothesised scenario for free
- `test_intra_file_duplicate_compact_boundary_does_not_crash` — preventive coverage for the compact_event vulnerability

Note on `needs_reindex` semantics after the patch: the mtime-only check remains correct. `OR IGNORE` absorbs collisions silently regardless of which sessions a given pass schedules, so the existing staleness heuristic does not need to change.

## Upstream concern

The duplicate-record phenomenon is a Claude Code *writer* behaviour, not a `cc-search-chats` bug. Absorbing the symptom unblocks indexing but does not prevent recurrence. A short bug report to the Claude Code team noting that JSONL session files can contain byte-identical message records on the uuid/parentUuid/sessionId/timestamp/content fields (differing only in `version`/`diagnostics`) after a resume or replay by a newer CLI version would be worth filing. Out of scope for this fix.

**Do NOT** adopt the bug report's recommendation of a composite `(session_id, uuid)` key in isolation — it does not fix the intra-file case (where session_id is the same). If a structural redesign is wanted later (e.g. allowing the same logical message to be associated with multiple sessions), that is a separate design decision that should follow a brainstorming pass; it is not required to unblock the tool.

## Test that should exist

A regression test that exercises a synthetic JSONL containing the same message UUID twice (with identical content) and asserts that `reindex_project` completes successfully and the resulting `message` table contains exactly one row for that UUID. This is the actual phenomenon observed on real data, not the report's hypothetical resume-fork scenario.

A second test for the cross-file case (two different session JSONLs sharing a UUID) is also worth adding as belt-and-braces — the fix handles it for free, and the test documents the chosen "first wins" semantics.

## Peer Review

Critical peer review completed 2026-05-12 by Opus 4.7 (1M context) clean subagent. Findings:

- **High:** 0
- **Medium:** 3 — (M1) "byte-identical" overclaim corrected; (M2) verdict-table "Correct" vs evidence-table "Plausible" reconciled to Plausible; (M3) compact_event line citation normalised
- **Low:** 3 — preventive vs corrective framing for compact_event fix clarified; `needs_reindex` semantics-after-fix added; phase numbering noted

The reviewer independently verified all ten cited file:line references, reproduced the crash on BJET, ran an independent Python scan of 139 projects (confirming zero cross-file overlap), ran a minimal SQLite probe confirming `INSERT OR IGNORE` skips AFTER INSERT triggers on conflict, and inspected raw JSONL records to find the `version`/`diagnostics` difference that motivated finding M1.

Reviewer's strongest concern: the fix recommendation is currently Plausible, not Demonstrated. Fastest upgrade path: apply the two-line patch and re-run `reindex_project` against BJET from a wiped DB. Approximately 30 seconds.

Reviewer's noted upstream concern: the duplicate-record phenomenon is a Claude Code writer behaviour; fixing the index absorbs the symptom but does not prevent recurrence. A separate upstream report to the Claude Code team is worth considering.

Overall assessment: needs minor revision (now applied) before presentation; analysis is fundamentally sound.
