# Multi-Source Chat Search Resume Handoff

Date: 2026-07-19  
Repository: `/home/brian/people/Brian/cc-search-chats-plugin-python`  
Current branch at handoff: `main` tracking `origin/main`

## Status

The design is complete and approved by the user. No implementation code has been
written yet, no implementation worktree has been created, and none of the new
design documents has been committed.

The approved source documents are:

- `docs/design-plans/2026-07-19-multi-source-chat-search.md`
- `docs/database.md`
- `docs/dependency-rationale.md`

This handoff file is also untracked at the time it is created.

The repository has pre-existing, user-owned dirty state that must be preserved:

- modified `uv.lock`
- untracked `.serena/`
- untracked `docs/implementation-plans/` containing older work

Do not stage, rewrite, remove, or otherwise absorb those items into this feature.

## Approved Product Behavior

The plugin must search both Claude Code and local Codex transcripts by default.
Every relevant command must accept `--source {all,claude,codex}` so either source
can be isolated, while `--source claude` remains the legacy-compatibility
boundary.

Codex discovery uses `$CODEX_HOME`, defaulting to `~/.codex`, and recursively
discovers dated `rollout-*.jsonl` files in both `sessions/` and
`archived_sessions/`. All physical versions remain visible. Never blindly prefer
an active transcript over an archived transcript that has the same provider
session ID. Exact references must be version-qualified; a bare extraction may
show every matching physical version.

Codex parsing must normalize canonical `response_item` messages, `turn_context`,
`session_meta`, and compaction records. It must not duplicate `event_msg`
projections or index system/developer instruction injections as ordinary chat
messages.

Human-readable and JSON output must expose useful provenance wherever available:
source, model, provider, client, primary/subagent identity and names, parent
identity, archive state, physical version, and timestamps. Timestamps must be
shown, not merely retained internally.

Recorded working-directory identity is exact after normalization. A primary
worktree and each linked worktree remain distinct even when they share a Git
common directory. Tests must deeply cover normal repositories, bare repositories
with linked worktrees, detached/locked/prunable/deleted worktrees, Codex temporary
worktrees, dots, hyphens, spaces, Unicode, symlinks, and nested repositories.

Atomic index rebuild is approved.

## Approved Secret-Containment Design

The threat is secret propagation from a source transcript into SQLite, search
output, and then another transcript. This is not merely a pre-commit check.

Vendor Betterleaks 1.6.1 for Linux, macOS, and Windows on x64 and arm64. Invoke it
offline over stdin with pinned package configuration, JSON output,
`--redact=100`, no verbose/banner/color output, `--ignore-gitleaks-allow`, no
validation or network use, and a minimal environment. Never retain or emit its
raw match, secret, source line, or raw stderr.

Scan at three boundaries:

1. Raw JSONL source bytes.
2. A framed projection of decoded plaintext before any SQLite write.
3. Framed output units and the final serialization before stdout.

For live files, open one read-only file descriptor, capture a byte-length prefix,
scan and hash exactly that prefix, normalize it, and commit only when a final
digest still matches the raw scan. Appends beyond the captured prefix are
deferred; in-place mutation rolls back. Do not spool candidate or raw content to
disk. Scanner failure is fail-closed for changed bytes and output, although the
last safe generation may remain available with a stale warning.

Blocked JSONL records may expose only strict structural data: record kind, role
enum, ISO timestamp, ordering, and compaction boundary. They use a non-FTS
redaction marker. Blocked compaction still advances its epoch.

SQLite holds safe finding metadata and an authoritative local allowlist. An
allowlist key is exact to physical version, source line/range, full JSONL record
digest, rule, and scanner configuration. It must not depend on the matched secret
or whole-file digest. An unrelated append therefore preserves an allowlist, but
moving, copying, changing the flagged record, changing scanner configuration, or
switching active/archive physical versions invalidates it. Ignore inline
transcript allow-comments.

Add an `audit-secrets` workflow that lists, rescans, allows, and revokes findings.
Allow reasons are length-limited and scanner-clean. Preserve allowlist policy
through derived-index rebuilds; rebuild conversations and findings. Keep stale
and revoked policy auditable. Ordinary commands warn about unresolved findings.

Tests need canaries proving detected secret material never appears in the
database, WAL, temporary files, stdout, stderr, or snapshots. Standard pre-commit
checks are separate and should cover Ruff/formatting, ty, ordinary file checks,
and the vendored scanner.

## Intended Next Steps

1. Re-read the three approved source documents; do not restart requirements
   discovery unless the files contradict this handoff.
2. Inspect `git status` and preserve the user-owned dirty state listed above.
3. Record only the approved documentation. The previously proposed commit split
   was:
   - `docs: design secure multi-source chat search`
   - `docs: specify containment storage and vendored Betterleaks`
   Include this handoff in the most appropriate documentation commit or in a
   small separate handoff commit.
4. Add `.worktrees/` to `.gitignore` and commit that safety change before creating
   a project-local worktree.
5. Create `.worktrees/multi-source-chat-search` on branch
   `multi-source-chat-search` from the documented local `main`.
6. In the new worktree, run `uv sync` and the complete existing pytest suite as a
   baseline. If baseline tests fail, report the evidence before changing code.
7. Use the approved seven design phases to write detailed implementation-plan
   files. The planning workflow calls for investigator subagents per phase,
   external-dependency verification, a final reviewer loop to zero issues, and a
   generated test-requirements plan.
8. Execute the resulting plan test-first only after the plan workflow has handed
   off cleanly. Preserve the explicit source, version, security, database, and
   output contracts above.

## Copy-Paste Resume Prompt

```text
Resume the approved multi-source chat search work in
/home/brian/people/Brian/cc-search-chats-plugin-python.

Start by reading these files completely:
- docs/design-plans/2026-07-19-multi-source-chat-search-resume.md
- docs/design-plans/2026-07-19-multi-source-chat-search.md
- docs/database.md
- docs/dependency-rationale.md

The design is approved; do not repeat brainstorming or requirements discovery
unless those documents conflict. No implementation has started. Preserve the
pre-existing modified uv.lock, untracked .serena/, and the pre-existing untracked
docs/implementation-plans/ content. Do not stage or edit them.

Continue with the documented next steps: intentionally commit only our design
state, add and commit the .worktrees/ ignore rule, create the isolated
.worktrees/multi-source-chat-search worktree on branch multi-source-chat-search,
run the baseline environment sync and tests, then produce and rigorously review
the seven-phase detailed implementation plan. Use the applicable repository
skills faithfully, including their explicitly required investigator/reviewer
subagents. After a clean planning handoff, continue implementing test-first if
the active user request still authorizes implementation.

Critical invariants: search Claude and Codex by default with --source isolation;
show every active/archive physical version; retain and display model/agent/source
provenance and timestamps; deeply test all worktree forms; and enforce the
vendored Betterleaks three-boundary containment and exact-record SQLite allowlist
without ever persisting or emitting secret values.
```
