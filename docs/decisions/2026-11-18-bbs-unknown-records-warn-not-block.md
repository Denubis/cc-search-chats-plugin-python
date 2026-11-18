# 2026-11-18 — An unfamiliar record warns; only a failure blocks

Status: Accepted
Date: 2026-11-18

**Question answered:** Should indexing around an unfamiliar record, with a warning, become the chat-search tool's built-in default on every machine, instead of a switch that each machine has to turn on?

**Human answer (verbatim):** "go look at prior precedent. if we are able to search it, then it's a warn. a block is because something's failed."

**Decision:** An unregistered record shape no longer blocks its file by
default. The file is indexed with that record omitted, counted and reported
as an `unknown_record_shape` warning. A file is blocked only when something
has failed. This reverses point 2 of ADR 0009 ("Fail-closed remains the
default") and the corresponding sentence of ADR 0003.

## Follow-up: a message whose body is unusable

**Question answered:** When one user or assistant message in a chat file has a body the reader cannot use, should the rest of that file still be indexed, with a warning saying a message was left out?

**Human answer (verbatim):** "yes, but also is this a real issue? and \"cannot\" is a strong word. Please don't include false pieties"

**Decision:** Format drift in the body of a known record type (a payload or
message that is missing or not an object, a missing or mistyped field) warns
and is indexed around, including when the record is a user or assistant
message that still has its identifiers. ADR 0009 point 2 listed "payload"
failures as still blocking; that part is reversed. A record whose session or
message identity is itself missing or unreadable, and admission rules, keep
blocking. No occurrence of an unusable message body was found on 2026-11-18:
the live index held one failed file, of a different kind, and no parser
release in the git history addressed this case.

**Original-message resolver:**
`ccchat:v1:claude:df736ff6-7aff-45a9-9d02-b9a279a93026:uuid:32974400-4d3e-451c-98c8-91a291b3c233`,
same native source, line 638, timestamp `2026-11-18T01:35:07.181Z`;
`cc-search-chats resolve` returned `status: resolved` on 2026-11-18.

## Commit and push authorisation

**Question answered:** May I commit the 2.5.3 chat-search fix to the main branch of its repository, with the new architecture decision marked accepted?

**Human answer (verbatim):** "yes, commit and push"

**Decision:** Commit the 2.5.3 change set to `main` with ADR 0011 accepted, and
push it. Installation remains a separate action.

**Original-message resolver:**
`ccchat:v1:claude:df736ff6-7aff-45a9-9d02-b9a279a93026:uuid:5091de64-0f26-4236-85e5-26db7fa2ccaa`,
same native source, line 761, timestamp `2026-11-18T02:09:54.231Z`; not
checked with `cc-search-chats resolve` at commit time.

**Precedent consulted:** ADR 0003 (2026-09-02, "can we skip them without
alarming out? ... this shouldn't cause an alarm/partial state"); the
2026-10-10 record, ADR 0010 (`docs/decisions/0010-claude-2-1-295-record-shapes.md`)
("so we're making sure current chats index, right?"); ADR 0009 and its design's authority table (2026-10-24, "yes,
we need a noisy ignore so that things can get on with the work and I know to
fix things properly"). The opt-in switch that ADR 0009 delivered was never set
on this machine: `~/.config/cc-search-chats/index.env` did not exist on
2026-11-18, and 102 Claude Code 2.1.325 files were blocked by one added
`cost-state` key.

**Original-message resolver:**
`ccchat:v1:claude:df736ff6-7aff-45a9-9d02-b9a279a93026:uuid:de6e6ce4-c9e2-4104-a300-5aa71401d0a0`,
native source
`~/.claude/projects/-home-brian-people-Brian-cc-search-chats-plugin-python/df736ff6-7aff-45a9-9d02-b9a279a93026.jsonl`,
line 222, timestamp `2026-11-17T23:43:19.955Z`. `cc-search-chats resolve '<locator>' --reference-only --json` returned
`status: resolved` on 2026-11-18 after the session was indexed.
