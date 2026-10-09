# Antigravity Primary Sessions Design

**Status:** Draft

**Supersedes:** in `docs/design-plans/2026-08-10-cross-vendor-semantic-search.md`,
the Definition of Done sentence (lines 55–56) and the AC1 failure criterion
(lines 96–98) that keep Agy sessions out of the searchable corpus, as narrowed
by ADR 0008. Rendered transport archives stay excluded; the rest of that design
stands.

## Purpose

An Antigravity agent could not verify its own chat citations because
`cc-search-chats` knows Antigravity only as an excluded artefact signature.
This design admits primary Antigravity CLI sessions started on or after
1 October 2026 as a third native provider: searchable, exactly resolvable and
incrementally refreshed. Everything outside that scope stays out and stays
quiet in freshness and coverage reporting.

## Authority Sources

| Decision or instruction | Exact source | Resolver | Resolution condition |
|---|---|---|---|
| Antigravity is a new design; subagent work is out; only the most recent sessions matter (ADR 0008). | `ccchat:v1:claude:42102a00-89eb-4f30-ad4e-e12c7a65e697:uuid:078509b3-01ba-4056-981f-d7e288b6ad33` | `cc-search-chats resolve '<exact source>' --reference-only --json` | `resolved`; one user message, 2026-10-08T07:34:13.656Z. |
| Gemini CLI chats stay out; only Antigravity primaries come in (ADR 0008). | `ccchat:v1:claude:42102a00-89eb-4f30-ad4e-e12c7a65e697:uuid:780c7360-079f-425d-90f8-a379384d3358` | `cc-search-chats resolve '<exact source>' --reference-only --json` | `resolved`; one user message `yes`, 2026-10-08T22:35:06.499Z. |
| Scope is October onwards; nothing slides out once indexed; old sessions need not be present. | `ccchat:v1:claude:42102a00-89eb-4f30-ad4e-e12c7a65e697:uuid:4bb6a285-93fd-4d14-9a58-ecbb6db5d6cf` | `cc-search-chats resolve '<exact source>' --reference-only --json` | `resolved`; one user message, 2026-10-08T23:22:06.681Z. |
| No native project is required; search must not complain about unindexed things inside the exclusions. | `ccchat:v1:claude:42102a00-89eb-4f30-ad4e-e12c7a65e697:uuid:e1f036eb-5a60-4f62-b3aa-7c7f01edea48` | `cc-search-chats resolve '<exact source>' --reference-only --json` | `resolved`; one user message, 2026-10-09T00:18:47.638Z. |
| The written design is accepted, on the condition that intentionally excluded sources never produce a stale loop. | `ccchat:v1:claude:42102a00-89eb-4f30-ad4e-e12c7a65e697:uuid:73a5b70e-6778-4405-acb9-de0a4e4ff855` | `cc-search-chats resolve '<exact source>' --reference-only --json` | `resolved`; one user message, 2026-10-09T00:52:00.539Z. |

On 2026-10-09 the installed CLI (2.3.6) returns `stale_index`, exit 3, for these
locators: the session file is a blocked source until 2.3.7 (commit `9d12c31`) is
installed and indexed. Until then the fallback is
`grep -c '"uuid":"<uuid>"' ~/.claude/projects/-home-brian-people-Brian-cc-search-chats-plugin-python/42102a00-89eb-4f30-ad4e-e12c7a65e697.jsonl`,
which prints `1` for each of the five. The human accepted the design on
2026-10-09 (fifth row). The status stays Draft until all five locators resolve
through the installed CLI.

## Universe of discourse

- **Writer:** the Antigravity CLI, uncoordinated with this tool. **Builder:**
  `index`. **Readers:** `search`, `resolve`, `context`, `extract`, `list`,
  `events`, `index --status`, and the bundled skill.
- **Source:** `~/.gemini/antigravity-cli/brain/<uuid>/.system_generated/logs/transcript_full.jsonl`.
  Observed 2026-10-09: 237 UUID-named directories, 228 with that file; 220
  start before October; of the eight October sessions seven open with
  `USER_EXPLICIT`/`USER_INPUT` and one with `SYSTEM`/`SYSTEM_MESSAGE`.
- **Never read:** siblings of `brain/` (`antigravity-oauth-token`,
  `settings.json`, `history.jsonl`, `conversation_summaries.db`, others);
  inside a session, `transcript.jsonl`, `logs/chunks/`, `messages/`, `steps/`,
  `.user_uploaded/`, `scratch/`; Gemini CLI files under `~/.gemini/tmp/`.

## Current state

- `Provider` has two values and ordinal locators are Codex-only
  (`core/identity.py:19-23`, `135-142`). Antigravity appears only as the
  `non_native_agy` signature (`providers/source_discovery.py:530-538`).
- Default roots are the standard root always and Ponytail when present
  (`providers/source_discovery.py:147-179`). Discovery walks every regular
  file, treats non-Claude as Codex rollouts, and opens every `.md`/`.json` for
  one byte (`source_discovery.py:554-569`, `756-770`).
- Provider dispatch is binary with `else` meaning Codex: `refresh.py:420-477`,
  `741-744`, `1072-1128`, `1139-1166`, `1255-1270`, `1405-1410`;
  `resolution.py:239-266`; `staleness.py:88-92`. Five affected functions are on
  the complexity ratchet (`complexipy-snapshot.json:83,143,147,151,173`), so a
  third branch would worsen a recorded function (inference; not run).
  `source_failure_summary` joins a literal two-provider list
  (`refresh.py:263-291`).
- Excluded artefacts are checkpointed at `complete_byte_offset = 0`
  (`refresh.py:1374-1387`), so `pending_bytes` equals file size
  (`refresh.py:1292`). Such a row counts as unindexed (`staleness.py:108-121`),
  as a pending tail (`cli.py:736`, `923`) and in `refresh.pending_bytes`
  (`cli.py:762-765`). Production has none (11,980 rows, all `indexed`).
- An unchanged checkpoint is metadata-only (`refresh.py:818-824`). Append
  stages only records past the watermark (`refresh.py:838-846`); publication
  rewrites only staged messages and those of replaced or removed sources
  (`refresh.py:2127-2150`, `1822-1856`).
- Three live CHECK constraints admit two providers
  (`message_current_provider_check`, `source_root_current_provider_check`,
  `source_failure_current_provider_check`; declared at `schema.sql:24`,
  `refresh_schema.sql:3`, `incremental_refresh_schema.sql:17`). The ledger is at
  version 10 (`migrations.py:22-33`).
- `repository` is NULL on all 1,598,992 current rows, so `--project` is an
  exact match on `cwd` (`index.py:138`).

## Goals and non-goals

**Goals.** Index visible prose and tool names/inputs of in-scope primary
sessions; resolve their locators against native bytes; refresh by suffix; keep
excluded sources silent; derive a working directory for `--project`; change
no behaviour for Claude or Codex.

**Non-goals.** Subagent sessions, Gemini CLI chats, pre-October sessions, a
sliding window, any sidecar or `history.jsonl` read, message attribution, and
retiring the `non_native_agy` signature check.

## Design

### Root and discovery

Provider token `antigravity`. The default root `~/.gemini/antigravity-cli/brain`
is configured only when present, as Ponytail is. `CC_SEARCH_ANTIGRAVITY_ROOTS`
replaces it with required roots; there is no singular variable.

Discovery lists the root's immediate children. A directory named as a canonical
lowercase UUID yields one candidate when
`<uuid>/.system_generated/logs/transcript_full.jsonl` is a regular file. Using
metadata only, it opens no file, follows no symlinked session directory, walks
no further, and emits no archive diagnostic. Other children are ignored
silently. The session ID is the directory name.

### Admission

Admission is decided on the first complete record whenever a parse starts at
ordinal 0, in order:

1. Not an object with string `source`, `type` and a `created_at` shaped
   `YYYY-MM-DDTHH:MM:SSZ`: **blocked**,
   `antigravity_unrecognised_first_record`.
2. `created_at` before `2026-10-01T00:00:00Z`: **excluded**,
   `antigravity_before_scope_start`.
3. `SYSTEM`/`SYSTEM_MESSAGE`: **excluded**, `antigravity_not_human_initiated`.
4. `USER_EXPLICIT`/`USER_INPUT`: **admitted** as `primary`.
5. Anything else: blocked as in 1.

The boundary is a code constant in UTC. Changing it advances the parser-state
version so every source is re-admitted. A file with no complete first record is
undecided and its bytes are ordinary pending bytes. The `non_native_agy`
signature probe is not applied to this provider.

### Excluded checkpoints and freshness

An excluded source is checkpointed `excluded` with
`complete_byte_offset = observed_size`, zero pending bytes, no messages, and its
code in `parser_state.excluded_code`. Exclusion is sticky: while device, inode
and parser version are unchanged and the file has not shrunk, later growth is
not a change. It causes no plan, read or generation, and the freshness scan
treats the source as fully represented. Replacement, truncation or a version
advance re-admits. `coverage.excluded_files` is the only place these sources
appear.

### Record policy

| `source`/`type` | Projection |
|---|---|
| `USER_EXPLICIT`/`USER_INPUT` | `user` prose: the text inside the leading `<USER_REQUEST>` element. Everything after it is injected context. |
| `MODEL`/`PLANNER_RESPONSE` | `assistant`: `content` is prose; `thinking` is excluded; `tool_calls` names and arguments become one `tool_name` and one `tool_input` row per record, joined in call order. |
| `MODEL`/`GENERIC` | Tool result, excluded (ADR 0003). |
| `SYSTEM`/`SYSTEM_MESSAGE` | Injected context, excluded. |
| `SYSTEM`/`CHECKPOINT` | Epoch boundary; later messages are one epoch higher. |
| Any other pair, a registered pair missing a required key, a `USER_INPUT` without the wrapper, or an unrecognised `created_at` | Blocks the source deterministically. |

Extra keys never block. A malformed line is skipped and consumes its ordinal
(`source_discovery.py:446-469`). Blank content yields no row.

### Identity

The locator is `ccchat:v1:antigravity:<uuid>:ordinal:<N>:sha256:<D>`; no other
key kind is valid. The logical ID is `record-<N>-<D>`, as Codex derives it
(`core/canonicalization.py:269-273`). Each record is its own logical message;
its prose and tool rows share one locator. `timestamp` is `created_at`
verbatim, `submitted_by` is unknown, `repository` is NULL.

### Derived working directory

`cwd` is derived, not native: the `Cwd` argument of the first `run_command`
call inside the checkpointed prefix, else NULL. Every row of a session carries
the same value. Because append cannot rewrite earlier rows, a tail that first
establishes the value causes that one source to be reparsed from byte zero in
the same run; identities and embedding digests do not change. Parser state
(version 1) is `next_conversation_epoch` plus the derived value.

### Provider registry

One adapter entry per provider owns: root defaults and variables; discovery;
parser-state version, initial value and serialisation; the parse entry with its
blocking, skippable and repaired codes; the unindexed-locator scan; permitted
locator key kinds; and optional admission. Its consumers are the dispatch sites
listed under Current state and the `--provider` choices.

### Public contract

JSON schema version stays 5; `provider` gains `antigravity`. `--agents` adds
nothing for this provider. `events` exports its user prose as retained human
events (`events.py:72-80`). Resolving an unindexed locator opens only the named
session, and an excluded session gives `no_match`.

## Failure and recovery

- **Unknown shape in an admitted session:** blocked, coverage partial, counted
  in `source_issues`. A parser update with a version advance retries it.
- **Truncated prose** (`<truncated N bytes>`): the kept text is indexed with a
  run diagnostic. October has 14 markers, all in excluded record types.
- **In-place rewrite with growth:** undetected, as for every provider; affected
  locators report `stale_source` until a reparse. Append-only rests on an
  asserted live watch and one `RUNNING` record that persisted mid-file.
- **Session directory deleted upstream:** removed under existing semantics
  (`refresh.py:2476-2481`). This is source deletion, not ageing.
- **Default root absent:** unconfigured, so its rows are removed at the next
  index and return when the directory does.
- **Migration failure:** the transaction rolls back and the ledger is
  unchanged. Afterwards an older CLI refuses the database
  (`migrations.py:63-69`).

## Decisions

| Choice | Viable alternatives | Evidence and consequence | Invalidated when |
|---|---|---|---|
| `transcript_full.jsonl` is the source. | `transcript.jsonl`. | The sibling truncates. | Upstream drops the full file. |
| Admission from the first record. | Cross-file subagent registry; sidecar or `history.jsonl`; content filtering at discovery. | The registry fails on the current build (asserted, unverified); the sidecar breaks isolation; filtering breaks metadata-only discovery. The subagent signal rests on one observed session. | A subagent opens with `USER_INPUT`, or a primary with `SYSTEM_MESSAGE`. |
| Fixed UTC start constant. | Sliding window. | Ruled: nothing slides out. | A new ruling. |
| Ordinal-plus-digest identity. | `step_index`. | Records carry no ID. | Upstream adds one. |
| Excluded checkpoint at full size, sticky (proposed). | Offset 0, as today. | Offset 0 reports every excluded file as unindexed and pending for ever. | Upstream rewrites first records in place. |
| Registry before the third provider (proposed). | Third branch in place. | The ratchet forbids worsening recorded functions. | The ratchet rule is withdrawn. |
| Keep `non_native_agy`. | Retire it. | It guards Claude and Codex roots against a different artefact. | That signature can no longer occur. |
| `cwd` from the first `run_command` (adopted default); reparse when first established (proposed). | Forward-only; always NULL. | Forward-only makes incremental and rebuilt rows differ. `Cwd` is model-chosen (inference): a repository root in two of seven sessions. | A native workspace field appears. |
| Fail closed; `antigravity` token; UTC boundary (adopted defaults). | Skip unknown records. | Matches Codex and ADR 0003. | Overturned by the human. |
| JSON schema stays 5 (proposed). | Bump to 6. | A new `provider` value is additive. | A consumer rejects unknown providers. |
| A never-classified file counts as unindexed until the next `index` (adopted default). | A first-record read in the search-time freshness scan. | Search never reads content (ADR 0002); new files of every provider are pending between runs, which CLAUDE.md calls expected. Once `index` classifies the file it is silent for ever. | The human rules that search may read first records. |
| The existing artefact exclusion adopts the full-size checkpoint (adopted default). | Leave it at offset 0. | Production has 0 excluded rows (checked 2026-10-09), so the change has no retroactive effect and the two paths stay identical. | An excluded source must become re-readable without a version advance. |
| An absent default root removes its rows, as Ponytail does today (adopted default). | Fail the run; keep rows for an unconfigured root. | Matches `refresh.py` removal semantics and Ponytail. Consequence: uninstalling Antigravity or moving `brain/` drops its rows at the next `index` unless `CC_SEARCH_ANTIGRAVITY_ROOTS` names the surviving path. This is source removal, not ageing. | The human rules that removal needs explicit authority. |
| `ASK_QUESTION` answers are excluded as tool results (adopted default). | Index them as user prose. | ADR 0003 classifies tool results as excluded; none exist in October. | A current-build question record carries the answer as `USER_INPUT`. |


## Acceptance criteria

Fixtures: an October human session, a pre-October session, a system-first
session, an unknown-first session.

### antigravity-primary-sessions.AC1: Admission

- **Success:** only the October human session yields messages, as
  `antigravity` and `primary`. The next two are `excluded` with their codes.
- **Failure:** the unknown-first session is a deterministic blocked source and
  none of its text is searchable.
- **Boundary:** `2026-09-30T23:59:59Z` is excluded; `2026-10-01T00:00:00Z` is
  admitted.

### antigravity-primary-sessions.AC2: Quiet exclusions

- **Success:** with only the first three fixtures present, a second index
  reads zero content bytes and creates no generation. `index --status` and
  search `index_state` report zero unindexed files and bytes and
  `no_source_changes`; `pending_tail_files` and `pending_bytes` are zero.
- **Success:** appending to an excluded file changes none of those.
- **Failure:** replacing an excluded file with an October human session
  admits it.
- **Failure (stale loop):** across three consecutive `index` runs with no
  source change, an excluded source is never re-planned, re-read, re-counted
  as unindexed, or reported through `coverage.source_issues`; its checkpoint
  row is byte-identical after each run.

### antigravity-primary-sessions.AC3: Incremental identity

- **Success:** an append reads only past the watermark and earlier locators
  still resolve.
- **Failure:** a one-byte edit of an indexed record gives `stale_source`; a
  locator into an excluded session gives `no_match`.

### antigravity-primary-sessions.AC4: Content boundary

- **Success:** tool arguments are found only with `--literal --tools`;
  messages after a `CHECKPOINT` carry epoch 1; prose holding a truncation
  marker is indexed with a run diagnostic.
- **Failure:** a phrase present only in `thinking`, a `GENERIC` record,
  `<ADDITIONAL_METADATA>`, a `SYSTEM_MESSAGE` or a `CHECKPOINT` is found in no
  mode. An unregistered `source`/`type` pair inside an admitted session blocks
  that source.

### antigravity-primary-sessions.AC5: Isolation

- **Success:** an unreadable credentials file beside `brain/` and unreadable
  session siblings produce no diagnostic. A directory without
  `transcript_full.jsonl` is not discovered.
- **Failure:** text present only in `transcript.jsonl` is never found; a
  `non_native_agy` file in a Claude root is still excluded.

### antigravity-primary-sessions.AC6: Derived working directory

- **Success:** `--project <derived path>` matches every prose row of the
  session, whether the `run_command` was indexed in one pass or arrived in a
  later append.
- **Failure:** a session without `run_command` has NULL `cwd` and is found
  only without `--project`.

### antigravity-primary-sessions.AC7: Migration 11

- **Success:** it applies when the three constraints carry other names;
  `antigravity` then inserts into all three tables.
- **Failure:** `gemini` is rejected. Before migration, commands other than
  `index --migrate` report `maintenance_required` with pending version 11 and
  change no schema.

### antigravity-primary-sessions.AC8: Unchanged providers

- **Success:** the pre-existing suites pass and the complexity gate reports no
  recorded function worse.
- **Boundary:** with a real `~/.gemini/antigravity-cli/brain` present, the
  suites index nothing from it.

## Implementation phases

1. **Provider registry.** No dependency. Owns AC8. Claude and Codex run through
   the registry with identical behaviour.
2. **Antigravity indexed and resolvable.** Depends on 1. Adds migration 11, the
   provider value and locator grammar, discovery, admission, the parser,
   excluded checkpoints, the freshness rule, `--provider antigravity`, and an
   autouse test fixture pointing `CC_SEARCH_ANTIGRAVITY_ROOTS` at an empty
   directory. Owns AC1–AC5 and AC7. In-scope sessions are searchable and
   resolvable with NULL `cwd`.
3. **Derived working directory.** Depends on 2. Owns AC6.
4. **Consumer truth and release.** Depends on 3. Updates the consumers below
   and owns the human judgments; production migration and UAT are separately
   authorised release actions.

## Verification and human judgment

AC1–AC8 are deterministic in disposable PostgreSQL. On production the first
index should report 228 sources: seven admitted, 221 excluded, none blocked (as
of 2026-10-09): an operational check, not UAT.

Human judgment: a content search finds a real October Antigravity session; its
result resolves; the surrounding context reads as that conversation; and
`index --status` reads calm.

## Migration

Migration 11 is a new ordered resource; applied bytes stay immutable
(`migrations.py:79-82`, `162-180`). For `message_current`,
`source_root_current` and `source_failure_current` it finds each CHECK
constraint whose only column is `provider`, drops it, and adds
`<table>_provider_check` admitting `claude`, `codex` and `antigravity`. It
touches neither legacy `message` nor the quarantine schema. ADR 0007 requires
a backup before `index --migrate`. Rollback is that backup with the previous
CLI, or a forward repair.

Tests pinned to ten migrations change: `tests/postgresql/test_migrations.py:326`,
`361`; `tests/postgresql/test_cli_journey.py:306`; and
`tests/test_identity.py:53-55` for the enum.

## Consumers to update at implementation

`cli.py:2689`, `2729`, `2759` and help; `README.md`; `skills/search-chat/SKILL.md`;
`commands/search-chat.md`; `CLAUDE.md` (Source Roots, CLI Contract);
`docs/architecture/database.md`; `docs/runbooks/laptop-deployment.md`. On
acceptance, the superseded lines of the 2026-08-10 design are amended so one
design owns the criterion.
