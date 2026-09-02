# 0002. Search never refreshes; the caller names the mode; the index stays joint

Status: Accepted
Date: 2026-09-02

## Context

The 2026-08-10 design made hybrid literal-plus-semantic ranking the default
`search` mode and let a stale ranked search admit or join one automatic
background index before answering. The 2026-08-29 and 2026-09-01 repairs kept
that shape and made the automatic index publish literal and semantic state
together as one coherent corpus generation.

The 2026-09-02 review of that repair found, on the production installation:

- Every default-mode search spawned a child that cold-loaded the 8 B BF16
  embedding model, about 16 GB of VRAM and 4.3 s, to embed one query. Ten
  searches in ten minutes were ten model loads, and the display GPU was the
  casualty during video calls. The five-minute debounce governed only the
  background index, never the query side.
- The automatic refresh was wedged. The installed systemd unit carried flags
  the new CLI rejected before it claimed the durable request, so request 6 sat
  in `launched`, a state no code path exits, and every stale search then
  waited 4.0 s for a publication that could not arrive.
- Default search returned nothing at all, because the render reserve was
  subtracted from one deadline twice and a semantic timeout therefore landed
  on the read-deadline boundary, discarding the literal hits that had already
  been retrieved.
- Agents calling the tool treated the default as semantic and did not say
  which mode they wanted, so a degraded literal fallback was indistinguishable
  to them from a semantic answer.

`index --literal-only` exists but, under the joint-publication rule, creates a
generation that `corpus_state` can never select, so it publishes nothing a
search can read.

## Decision

Ruled by Brian on 2026-09-02, answering whether hybrid should stay the default
search mode: *"ok, the problem is that models treat it as semantic by default,
so no, no searching without --literal or --semantic, and they just need to
state the date of most recent index. Context should be obvious to callers."*

Asked whether search should therefore never trigger a refresh: *"Correct, I
don't think refreshing on search works with how we have things set up. So it
should just say "index literal made bleh, index semantic made bar, current time
is baz, missing foo chats in bar directories) and yeah, I don't think the
autorefresh ever worked well nor served my purposes."*

Asked whether literal and semantic publication should be decoupled so those
two dates could differ: *"I mean, that's great? it's more advertising that we
have two modes of engaging? Keep joint, but make sure it's clear that literal
is literal"*.

1. **The caller names the mode.** `search` requires exactly one of `--literal`
   or `--semantic`. Omitting both is a usage error that names both modes and
   what each does. `--literal` is PostgreSQL full-text search with no model.
   `--semantic` is the model-backed ranking, which fuses literal and semantic
   candidates by reciprocal rank; its output says so. `--tools` and
   `--exhaustive` still require `--literal`.

2. **Literal is visibly literal.** Every search answer, human or JSON, states
   its mode before its results, and a semantic request that degrades to literal
   says so as a warning that the caller cannot mistake for a semantic answer.

3. **Search never indexes.** No search admits, launches, joins, or waits for a
   refresh. The automatic-refresh state machine, its systemd unit, the
   search-side LISTEN, and the `--background-refresh` flag are removed rather
   than repaired. `index`, run by a person, an agent, or the nightly timer, is
   the only builder.

4. **Every answer states its staleness.** Search and `index --status` report
   when the selected corpus was made, the current time, the age, and how many
   session files in how many directories have appeared or grown since that
   index. The last figure comes from a bounded scan of the configured source
   roots against the indexed watermarks. When the scan cannot finish inside
   the request budget, the answer says the figure is unknown rather than
   guessing.

5. **The index stays joint.** One `index` run publishes literal and semantic
   state together, as the 2026-09-01 repair established, so the corpus has one
   made-at time. The flags that pretended otherwise, `--literal-only` and
   `--semantic-only`, are removed. A search that wants newer chats than the
   header shows tells the caller to run `cc-search-chats index`.

6. **The deadline degrades, it does not discard.** The five-second budget keeps
   one render reserve, and a budget exhausted after retrieval returns the hits
   already retrieved with a degradation warning instead of an error envelope.

7. **The output contract moves to schema version 4.** Removing
   `background_refresh` and its stale reason, adding the mode and the
   staleness block, and refusing a mode-less search are visible changes, so the
   version says so instead of leaving consumers to discover it.

## Consequences

The GPU is touched only when a caller asks for `--semantic` or when `index`
runs, and the nightly timer is the ordinary owner of that cost. The class of
defects that lived in the automatic path, the unexitable `launched` state, the
non-atomic no-op completion, the silent four-second waits, and the advisory-lock
hang between a search and a service-owned index, is deleted rather than fixed.

Results can be as old as the last `index`. The header makes that age and the
count of unindexed chats explicit on every answer, which is the trade the
ruling accepts: an honest stale answer over an opportunistic refresh that
competed with the operator's own use of the machine.

Callers that omitted a mode must now choose one, and consumers that read
`background_refresh` or pinned `schema_version: 3` must move to 4. The
`skills/search-chat` and `commands/search-chat.md` consumers are updated with
this decision.
