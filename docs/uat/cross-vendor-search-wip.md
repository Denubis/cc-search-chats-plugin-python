# Cross-vendor search UAT

Status: prepared, not run

This acceptance checks the exact installed candidate against standard and
Ponytail Claude/Codex native sessions. It does not authorize installation,
production migration, or prune. Append actual results only after the human
authorizes and performs UAT.

Semantic search has no answer deadline. This UAT requires one cold hybrid result,
an immediate warm-helper reuse, and hybrid results for all four vendor/root
controls; a `literal_fallback` is a rejection of the candidate, not a pass.

## Preconditions

- Run PostgreSQL maintenance runbook §1 and prove that the installed CLI is the
  exact accepted clean-main commit with the semantic extra.
- Under separate human migration authority, complete runbook §3, including the
  selected-pair inspection before migration. Retain the separate stdout JSON
  and stderr NDJSON from `index --migrate --json`; the JSON must report
  `applied_schema_version == 10`.
- The pinned model snapshot is already present in the configured cache and
  runtime network access is disabled.
- These four session directories exist and remain read-only to the tool:
  `~/.claude/projects`, `~/.claude-ponytail/projects`, `~/.codex/sessions`, and
  `~/.codex-ponytail/sessions`.
- `cc-search-chats-index.timer` is enabled. Record its next activation and
  confirm that it falls outside the planned UAT window.
- This UAT proves `cc-search-chats-refresh.service` is absent from the user unit
  inventory rather than merely inactive.
- Record the reviewed baseline's exact `coverage.completeness`, `blocked_files`,
  and `skipped_records` in the environment below. A different value requires a
  new structural reconciliation before UAT continues, and
  `transient_failure_files` must be zero.

## Configure the exact four roots and controls

Choose four unique benign sentinel phrases and identify the four native sessions
that will receive them. Do not send the messages yet: the acceptance script first
records the selected baseline and the UTC lower bound, then pauses for the four
native-client submissions. Never edit JSONL.

Run this environment block in fish after replacing every placeholder. The two
migration evidence paths refer to the already-authorized runbook §3 artifacts;
the UAT script does not migrate.

```fish
set -x CC_SEARCH_CLAUDE_ROOTS "$HOME/.claude/projects:$HOME/.claude-ponytail/projects"
set -x CC_SEARCH_CODEX_ROOTS "$HOME/.codex/sessions:$HOME/.codex-ponytail/sessions"
set -e CC_SEARCH_SEMANTIC_WARM_SECONDS

set -x UAT_CLAUDE_STANDARD_SESSION 'REPLACE'
set -x UAT_CLAUDE_STANDARD_QUERY 'REPLACE UNIQUE SENTINEL PHRASE'
set -x UAT_CLAUDE_PONYTAIL_SESSION 'REPLACE'
set -x UAT_CLAUDE_PONYTAIL_QUERY 'REPLACE UNIQUE SENTINEL PHRASE'
set -x UAT_CODEX_STANDARD_SESSION 'REPLACE'
set -x UAT_CODEX_STANDARD_QUERY 'REPLACE UNIQUE SENTINEL PHRASE'
set -x UAT_CODEX_PONYTAIL_SESSION 'REPLACE'
set -x UAT_CODEX_PONYTAIL_QUERY 'REPLACE UNIQUE SENTINEL PHRASE'

set -x UAT_EXPECTED_COMPLETENESS 'REPLACE REVIEWED BASELINE VALUE'
set -x UAT_EXPECTED_BLOCKED_SOURCES 'REPLACE REVIEWED BASELINE COUNT'
set -x UAT_EXPECTED_SKIPPED_RECORDS 'REPLACE REVIEWED BASELINE COUNT'
set -x UAT_MIGRATION_JSON '/REPLACE/WITH/index-migrate.stdout.json'
set -x UAT_MIGRATION_NDJSON '/REPLACE/WITH/index-migrate.stderr.ndjson'
```

The explicit plural roots are part of the test. They include only session
directories; they do not point at either isolated home.

## Acceptance script

Machine-readable invocations keep stdout JSON and stderr NDJSON separate. The
script retains those artifacts, the user-unit and timer observations, two human
staleness displays, the provenance rows, query-helper lifecycle evidence, and a
locator ledger in one evidence directory.

```fish
set -g uat_dir (mktemp -d)
or exit 1

function assert_progress --argument-names path
    # Invariant: stderr is pure ordered schema-v4 NDJSON with exactly one final terminal event.
    python -c 'import json,sys; lines=open(sys.argv[1],encoding="utf-8").read().splitlines(); assert lines and all(line.strip() for line in lines); events=[json.loads(line) for line in lines]; assert all(event["schema_version"]==4 for event in events); assert [event["sequence"] for event in events]==list(range(1,len(events)+1)); assert sum(event["event"]=="terminal" for event in events)==1; assert events[-1]["event"]=="terminal"' "$path"
end

function assert_reviewed_coverage --argument-names path
    # Invariant: coverage stays at the reviewed baseline and has no transient source failures.
    python -c 'import json,sys; data=json.load(open(sys.argv[1],encoding="utf-8")); coverage=data["coverage"]; refresh=data["refresh"]; expected_completeness=sys.argv[2]; expected_blocked=int(sys.argv[3]); expected_skipped=int(sys.argv[4]); assert data["schema_version"]==4; assert coverage["completeness"]==expected_completeness; assert coverage["blocked_files"]==expected_blocked; assert coverage["skipped_records"]==expected_skipped; assert coverage["transient_failure_files"]==0; assert refresh["blocked_sources"]==expected_blocked; assert refresh["transient_failure_sources"]==0' "$path" "$UAT_EXPECTED_COMPLETENESS" "$UAT_EXPECTED_BLOCKED_SOURCES" "$UAT_EXPECTED_SKIPPED_RECORDS"
end

function assert_complete_status --argument-names path
    # Invariant: status selects one complete joint corpus with legible bounded staleness and four roots.
    python -c 'import json,sys; data=json.load(open(sys.argv[1],encoding="utf-8")); state=data["index_state"]; semantic=data["semantic"]; refresh=data["refresh"]; assert data["schema_version"]==4 and data["command"]=="index" and data["status"]=="complete"; assert data["selected"] is True and data["completed"]==data["total"]; assert semantic["fresh"] is True and semantic["corpus_generation"]==refresh["corpus_generation"]; assert data["coverage"]["configured_root_count"]==4 and data["coverage"]["resolved_root_count"]==4; assert set(state)=={"made_at","now","age_ms","corpus_generation","semantic_build","unindexed","unindexed_reason"}; assert isinstance(state["made_at"],str) and state["made_at"]==data["indexed_at"]; assert isinstance(state["now"],str) and state["now"]; assert isinstance(state["age_ms"],int) and state["age_ms"]>=0 and state["age_ms"]==data["corpus_age_ms"]; assert state["corpus_generation"]==refresh["corpus_generation"] and isinstance(state["corpus_generation"],int); assert state["semantic_build"]==semantic["semantic_build"] and isinstance(state["semantic_build"],int); unindexed=state["unindexed"]; reason=state["unindexed_reason"]; counts=isinstance(unindexed,dict) and set(unindexed)=={"files","directories","bytes"} and all(isinstance(unindexed[key],int) and unindexed[key]>=0 for key in unindexed) and reason is None; closed=unindexed is None and isinstance(reason,str) and bool(reason); assert counts or closed' "$path"
end

function assert_user_units
    systemctl --user is-enabled cc-search-chats-index.timer >$uat_dir/index-timer-enabled.txt 2>&1
    or return 1
    systemctl --user list-timers --all --no-pager cc-search-chats-index.timer >$uat_dir/index-timer-schedule.txt 2>&1
    or return 1
    systemctl --user list-units --all --no-legend --no-pager >$uat_dir/user-units.txt 2>&1
    or return 1
    systemctl --user list-unit-files --no-legend --no-pager >$uat_dir/user-unit-files.txt 2>&1
    or return 1

    # Invariant: the nightly index timer is enabled and the retired unit is absent, not inactive.
    python -c 'from pathlib import Path; import sys; enabled=Path(sys.argv[1]).read_text(encoding="utf-8").strip(); schedule=Path(sys.argv[2]).read_text(encoding="utf-8"); inventories=[Path(path).read_text(encoding="utf-8").splitlines() for path in sys.argv[3:]]; assert enabled=="enabled" and "cc-search-chats-index.timer" in schedule; assert all(all("cc-search-chats-refresh.service" not in line.split() for line in inventory) for inventory in inventories)' "$uat_dir/index-timer-enabled.txt" "$uat_dir/index-timer-schedule.txt" "$uat_dir/user-units.txt" "$uat_dir/user-unit-files.txt"
end

function probe_staleness
    set -l stale "$uat_dir/stale-search.json"
    set -l stale_progress "$uat_dir/stale-search.ndjson"
    set -l started (date +%s%N)
    or return 1
    cc-search-chats search "$UAT_CLAUDE_STANDARD_QUERY" --literal --provider claude --limit 200 --json >$stale 2>$stale_progress
    or return 1
    set -l finished (date +%s%N)
    or return 1
    set -l wall_ms (math "($finished - $started) / 1000000")
    or return 1
    assert_progress "$stale_progress"
    or return 1
    assert_reviewed_coverage "$stale"
    or return 1

    # Invariant: stale literal search answers within five seconds from the selected baseline without exposing the new control.
    python -c 'import json,sys; data=json.load(open(sys.argv[1],encoding="utf-8")); baseline=json.load(open(sys.argv[2],encoding="utf-8")); query=sys.argv[3]; wall_ms=float(sys.argv[4]); assert wall_ms<=5000 and data["deadline_ms"]==5000; assert data["mode"]=="literal" and data["retrieval_mode"]=="literal" and data["status"]=="complete"; assert all(query not in result["text"] for result in data["results"]); assert data["refresh"]["corpus_generation"]==baseline["refresh"]["corpus_generation"]; assert data["index_state"]["made_at"]==baseline["index_state"]["made_at"]; unindexed=data["index_state"]["unindexed"]; reason=data["index_state"]["unindexed_reason"]; counted=isinstance(unindexed,dict) and unindexed["files"]>=1 and reason is None; closed=unindexed is None and isinstance(reason,str) and bool(reason); assert counted or closed; print("unindexed.files="+str(unindexed["files"]) if counted else "unindexed_reason="+reason)' "$stale" "$uat_dir/baseline-status.json" "$UAT_CLAUDE_STANDARD_QUERY" "$wall_ms" >$uat_dir/staleness-observation.txt
    or return 1

    cc-search-chats search "$UAT_CLAUDE_STANDARD_QUERY" --literal --provider claude --limit 200 --progress human >$uat_dir/stale-search.human.txt 2>$uat_dir/stale-search.human-progress.txt
    or return 1
    cc-search-chats index --status --json >$uat_dir/post-search-status.json 2>$uat_dir/post-search-status.ndjson
    or return 1
    assert_progress "$uat_dir/post-search-status.ndjson"
    or return 1
    assert_reviewed_coverage "$uat_dir/post-search-status.json"
    or return 1

    # Invariant: search did not publish or move the selected corpus.
    python -c 'import json,sys; current=json.load(open(sys.argv[1],encoding="utf-8")); baseline=json.load(open(sys.argv[2],encoding="utf-8")); assert current["refresh"]["corpus_generation"]==baseline["refresh"]["corpus_generation"]; assert current["semantic"]["semantic_build"]==baseline["semantic"]["semantic_build"]; assert current["index_state"]["made_at"]==baseline["index_state"]["made_at"]' "$uat_dir/post-search-status.json" "$uat_dir/baseline-status.json"
end

function publish_intentionally
    cc-search-chats index --json >$uat_dir/published-index.json 2>$uat_dir/published-index.ndjson
    or return 1
    assert_progress "$uat_dir/published-index.ndjson"
    or return 1
    assert_reviewed_coverage "$uat_dir/published-index.json"
    or return 1

    # Invariant: intentional indexing publishes a newer joint corpus with a complete fresh semantic build.
    python -c 'import json,sys; data=json.load(open(sys.argv[1],encoding="utf-8")); baseline=json.load(open(sys.argv[2],encoding="utf-8")); generation=data["refresh"]["corpus_generation"]; build=data["semantic"]["semantic_build"]; assert data["schema_version"]==4 and data["command"]=="index" and data["status"]=="complete"; assert isinstance(generation,int) and generation>baseline["refresh"]["corpus_generation"]; assert isinstance(build,int) and build>baseline["semantic"]["semantic_build"]; assert data["semantic"]["corpus_generation"]==generation and data["semantic"]["fresh"] is True; assert data["semantic"]["completed_units"]==data["semantic"]["total_units"]' "$uat_dir/published-index.json" "$uat_dir/baseline-status.json"
    or return 1

    cc-search-chats index --json >$uat_dir/unchanged-index.json 2>$uat_dir/unchanged-index.ndjson
    or return 1
    assert_progress "$uat_dir/unchanged-index.ndjson"
    or return 1
    assert_reviewed_coverage "$uat_dir/unchanged-index.json"
    or return 1

    # Invariant: an unchanged second index is a no-op that retains generation and semantic build identity.
    python -c 'import json,sys; current=json.load(open(sys.argv[1],encoding="utf-8")); published=json.load(open(sys.argv[2],encoding="utf-8")); assert current["schema_version"]==4 and current["status"]=="complete"; assert current["refresh"]["corpus_generation"]==published["refresh"]["corpus_generation"]; assert current["semantic"]["semantic_build"]==published["semantic"]["semantic_build"]; assert current["semantic"]["corpus_generation"]==published["semantic"]["corpus_generation"]' "$uat_dir/unchanged-index.json" "$uat_dir/published-index.json"
    or return 1

    cc-search-chats index --status --json >$uat_dir/post-index-status.json 2>$uat_dir/post-index-status.ndjson
    or return 1
    assert_progress "$uat_dir/post-index-status.ndjson"
    or return 1
    assert_reviewed_coverage "$uat_dir/post-index-status.json"
    or return 1
    assert_complete_status "$uat_dir/post-index-status.json"
    or return 1

    # Invariant: status selects the generation and semantic build published by the intentional index.
    python -c 'import json,sys; status=json.load(open(sys.argv[1],encoding="utf-8")); published=json.load(open(sys.argv[2],encoding="utf-8")); assert status["refresh"]["corpus_generation"]==published["refresh"]["corpus_generation"]; assert status["semantic"]["semantic_build"]==published["semantic"]["semantic_build"]' "$uat_dir/post-index-status.json" "$uat_dir/published-index.json"
    or return 1

    cc-search-chats index --status --progress human >$uat_dir/post-index-status.human.txt 2>$uat_dir/post-index-status.human-progress.txt
    or return 1
end

function run_case --argument-names label provider expected_root session query cold_check
    set -l literal "$uat_dir/$label.literal.json"
    set -l literal_progress "$uat_dir/$label.literal.ndjson"
    cc-search-chats search "$query" --literal --provider "$provider" --limit 200 --json >$literal 2>$literal_progress
    or return 1
    assert_progress "$literal_progress"
    or return 1
    assert_reviewed_coverage "$literal"
    or return 1

    # Invariant: literal mode finds the expected native control and search does not publish a new selected pair.
    set -l locator (python -c 'import json,sys; data=json.load(open(sys.argv[1],encoding="utf-8")); prior=json.load(open(sys.argv[2],encoding="utf-8")); provider=sys.argv[3]; session=sys.argv[4]; query=sys.argv[5]; assert data["schema_version"]==4 and data["status"]=="complete"; assert data["mode"]=="literal" and data["retrieval_mode"]=="literal"; matches=[result for result in data["results"] if result["identity"]["provider"]==provider and result["identity"]["source_session_id"]==session and query in result["text"]]; assert matches; assert data["refresh"]["corpus_generation"]==prior["refresh"]["corpus_generation"]; assert data["semantic"]["semantic_build"]==prior["semantic"]["semantic_build"]; print(matches[0]["identity"]["canonical_locator"])' "$literal" "$uat_dir/post-index-status.json" "$provider" "$session" "$query")
    or return 1

    set -l resolved "$uat_dir/$label.resolve.json"
    set -l resolved_progress "$uat_dir/$label.resolve.ndjson"
    cc-search-chats resolve "$locator" --reference-only --json >$resolved 2>$resolved_progress
    or return 1
    assert_progress "$resolved_progress"
    or return 1
    assert_reviewed_coverage "$resolved"
    or return 1

    # Invariant: exact reference-only resolution preserves identity and aliases while omitting message bodies.
    python -c 'import json,sys; data=json.load(open(sys.argv[1],encoding="utf-8")); locator=sys.argv[2]; assert data["schema_version"]==4 and data["status"]=="resolved" and data["messages"]; matches=[message for message in data["messages"] if message["identity"]["canonical_locator"]==locator]; assert matches; assert all("text" not in message and message["identity"]["physical_aliases"] for message in matches)' "$resolved" "$locator"
    or return 1

    psql service=cc_search_chats -v ON_ERROR_STOP=1 -v locator="$locator" -At -c "SELECT DISTINCT root.resolved_path FROM cc_search_chats.message_current AS message JOIN cc_search_chats.physical_alias_current AS alias USING (provider, source_session_id, logical_message_id, content_class) JOIN cc_search_chats.source_root_current AS root USING (source_root_id) WHERE message.canonical_locator = :'locator' ORDER BY root.resolved_path" >$uat_dir/$label.provenance-roots.txt
    or return 1

    # Invariant: the indexed physical alias resolves to the expected standard or Ponytail root.
    python -c 'from pathlib import Path; import sys; roots={line for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if line}; assert sys.argv[2] in roots' "$uat_dir/$label.provenance-roots.txt" "$expected_root"
    or return 1

    printf '%s\t%s\t%s\t%s\n' "$label" "$provider" "$session" "$locator" >>$uat_dir/positive-cases.tsv

    set -l semantic "$uat_dir/$label.semantic.json"
    set -l semantic_progress "$uat_dir/$label.semantic.ndjson"
    cc-search-chats search "$query" --semantic --provider "$provider" --limit 200 --json >$semantic 2>$semantic_progress
    or return 1
    assert_progress "$semantic_progress"
    or return 1
    assert_reviewed_coverage "$semantic"
    or return 1

    # Invariant: semantic mode has no deadline, returns hybrid RRF evidence, and the first semantic call after index is cold.
    python -c 'import json,sys; data=json.load(open(sys.argv[1],encoding="utf-8")); locator=sys.argv[2]; cold=sys.argv[3]=="cold"; timing=data["semantic"]; assert data["schema_version"]==4 and data["status"]=="complete" and data["deadline_ms"] is None; assert data["mode"]=="semantic" and data["retrieval_mode"]=="hybrid"; matches=[result for result in data["results"] if result["identity"]["canonical_locator"]==locator]; assert matches; ranking=matches[0]["ranking"]; assert ranking["method"]=="rrf" and ranking["semantic_rank"] is not None and ranking["semantic_chunk_ordinal"] is not None; assert all(warning["code"]!="semantic_search_degraded" for warning in data["warnings"]); assert (not cold) or (timing["warm_reused"] is False and isinstance(timing["model_load_ms"],int) and timing["model_load_ms"]>0)' "$semantic" "$locator" "$cold_check"
    or return 1

    if test "$cold_check" = cold
        set -l cold_finished (date +%s%N)
        or return 1
        set -l warm_semantic "$uat_dir/$label.semantic-warm.json"
        set -l warm_progress "$uat_dir/$label.semantic-warm.ndjson"
        set -l warm_started (date +%s%N)
        or return 1
        cc-search-chats search "$query" --semantic --provider "$provider" --limit 200 --json >$warm_semantic 2>$warm_progress
        or return 1
        assert_progress "$warm_progress"
        or return 1
        assert_reviewed_coverage "$warm_semantic"
        or return 1

        # Invariant: the immediate second semantic call starts inside thirty seconds and reuses the warm model without loading it again.
        python -c 'import json,sys; data=json.load(open(sys.argv[1],encoding="utf-8")); locator=sys.argv[2]; gap_ms=(int(sys.argv[4])-int(sys.argv[3]))/1000000; timing=data["semantic"]; assert gap_ms<30000; assert data["schema_version"]==4 and data["status"]=="complete" and data["deadline_ms"] is None; assert data["mode"]=="semantic" and data["retrieval_mode"]=="hybrid"; assert timing["warm_reused"] is True and timing["model_load_ms"]==0; assert any(result["identity"]["canonical_locator"]==locator for result in data["results"])' "$warm_semantic" "$locator" "$cold_finished" "$warm_started"
        or return 1
    end

    printf '%s\n' "$locator"
end

function probe_exhaustive
    cc-search-chats search "$UAT_CLAUDE_STANDARD_QUERY" --literal --tools --exhaustive --provider claude --json >$uat_dir/claude-exhaustive.json 2>$uat_dir/claude-exhaustive.ndjson
    or return 1
    assert_progress "$uat_dir/claude-exhaustive.ndjson"
    or return 1
    assert_reviewed_coverage "$uat_dir/claude-exhaustive.json"
    or return 1

    # Invariant: exhaustive literal mode is unbounded, includes tools, and still contains the Claude control.
    python -c 'import json,sys; data=json.load(open(sys.argv[1],encoding="utf-8")); query=sys.argv[2]; assert data["schema_version"]==4 and data["status"]=="complete"; assert data["mode"]=="literal" and data["retrieval_mode"]=="exhaustive_literal"; assert data["exhaustive"] is True and data["result_limit"] is None and data["deadline_ms"] is None; assert any(query in result["text"] for result in data["results"])' "$uat_dir/claude-exhaustive.json" "$UAT_CLAUDE_STANDARD_QUERY"
end

function probe_events --argument-names from_utc
    set -l until_utc (date -u '+%Y-%m-%dT%H:%M:%SZ')
    or return 1
    cc-search-chats events --from "$from_utc" --until "$until_utc" --json >$uat_dir/events.json 2>$uat_dir/events.ndjson
    or return 1
    assert_progress "$uat_dir/events.ndjson"
    or return 1
    assert_reviewed_coverage "$uat_dir/events.json"
    or return 1

    # Invariant: the published generation exports all four human controls as content-free canonical events.
    python -c 'import json,sys; data=json.load(open(sys.argv[1],encoding="utf-8")); published=json.load(open(sys.argv[2],encoding="utf-8")); expected={("claude",sys.argv[3]),("claude",sys.argv[4]),("codex",sys.argv[5]),("codex",sys.argv[6])}; generation=published["refresh"]["corpus_generation"]; events=data["events"]; keys={"event_id","occurred_at_utc","canonical_locator","provider","source_session_id","session_kind","cwd","repository","submitted_by","retention_status","physical_alias_count","source_corpus_generation"}; assert data["schema_version"]==4 and data["command"]=="events" and data["status"]=="complete"; assert data["window"]=={"from_utc":sys.argv[7],"until_utc":sys.argv[8]}; assert data["source_corpus_generation"]==generation; assert data["population"]["retained"]>=4 and events; assert set(events[0])==keys and "text" not in set(events[0]); assert all(set(event)==keys and event["source_corpus_generation"]==generation for event in events); observed={(event["provider"],event["source_session_id"]) for event in events}; assert expected<=observed' "$uat_dir/events.json" "$uat_dir/published-index.json" "$UAT_CLAUDE_STANDARD_SESSION" "$UAT_CLAUDE_PONYTAIL_SESSION" "$UAT_CODEX_STANDARD_SESSION" "$UAT_CODEX_PONYTAIL_SESSION" "$from_utc" "$until_utc"
end

function execute_uat
    cp -- "$UAT_MIGRATION_JSON" "$uat_dir/index-migrate.stdout.json"
    or return 1
    cp -- "$UAT_MIGRATION_NDJSON" "$uat_dir/index-migrate.stderr.ndjson"
    or return 1
    assert_progress "$uat_dir/index-migrate.stderr.ndjson"
    or return 1

    # Invariant: the separately authorized migration evidence is one schema-v4 index result at ledger version 10.
    python -c 'import json,sys; data=json.load(open(sys.argv[1],encoding="utf-8")); assert data["schema_version"]==4 and data["command"]=="index" and data["status"]=="complete"; assert data["applied_schema_version"]==10' "$uat_dir/index-migrate.stdout.json"
    or return 1

    assert_user_units
    or return 1
    read -P 'Confirm the retained timer schedule is not due inside the planned UAT window, then press Enter: ' timer_confirmation

    cc-search-chats index --status --json >$uat_dir/baseline-status.json 2>$uat_dir/baseline-status.ndjson
    or return 1
    assert_progress "$uat_dir/baseline-status.ndjson"
    or return 1
    assert_reviewed_coverage "$uat_dir/baseline-status.json"
    or return 1
    assert_complete_status "$uat_dir/baseline-status.json"
    or return 1

    set -l uat_from_utc (date -u '+%Y-%m-%dT%H:%M:%SZ')
    or return 1
    printf '%s\n' "$uat_from_utc" >$uat_dir/uat-from-utc.txt
    read -P 'Send the four configured sentinel phrases through their native clients, wait for newline-terminated records, then press Enter: ' sentinel_confirmation

    probe_staleness
    or return 1
    publish_intentionally
    or return 1

    set -l claude_standard_locator (run_case claude-standard claude "$HOME/.claude/projects" "$UAT_CLAUDE_STANDARD_SESSION" "$UAT_CLAUDE_STANDARD_QUERY" cold)
    or return 1
    set -l claude_ponytail_locator (run_case claude-ponytail claude "$HOME/.claude-ponytail/projects" "$UAT_CLAUDE_PONYTAIL_SESSION" "$UAT_CLAUDE_PONYTAIL_QUERY" ordinary)
    or return 1
    set -l codex_standard_locator (run_case codex-standard codex "$HOME/.codex/sessions" "$UAT_CODEX_STANDARD_SESSION" "$UAT_CODEX_STANDARD_QUERY" ordinary)
    or return 1
    set -l codex_ponytail_locator (run_case codex-ponytail codex "$HOME/.codex-ponytail/sessions" "$UAT_CODEX_PONYTAIL_SESSION" "$UAT_CODEX_PONYTAIL_QUERY" ordinary)
    or return 1

    probe_exhaustive
    or return 1
    probe_events "$uat_from_utc"
    or return 1

    cc-search-chats index --status --json >$uat_dir/final-status.json 2>$uat_dir/final-status.ndjson
    or return 1
    assert_progress "$uat_dir/final-status.ndjson"
    or return 1
    assert_reviewed_coverage "$uat_dir/final-status.json"
    or return 1
    assert_complete_status "$uat_dir/final-status.json"
    or return 1

    # Invariant: the final selected pair is still the one intentionally published for this UAT.
    python -c 'import json,sys; final=json.load(open(sys.argv[1],encoding="utf-8")); published=json.load(open(sys.argv[2],encoding="utf-8")); assert final["refresh"]["corpus_generation"]==published["refresh"]["corpus_generation"]; assert final["semantic"]["semantic_build"]==published["semantic"]["semantic_build"]' "$uat_dir/final-status.json" "$uat_dir/published-index.json"
    or return 1

    systemctl --user is-enabled cc-search-chats-index.timer >$uat_dir/final-index-timer-enabled.txt 2>&1
    or return 1

    # Invariant: the intended nightly builder remains enabled after all UAT controls.
    python -c 'from pathlib import Path; import sys; assert Path(sys.argv[1]).read_text(encoding="utf-8").strip()=="enabled"' "$uat_dir/final-index-timer-enabled.txt"
    or return 1

    sleep 60
    set -l helper_runtime "$HOME/.cc-search-chats"
    if set -q CC_SEARCH_RUNTIME_DIR
        set helper_runtime "$CC_SEARCH_RUNTIME_DIR"
    end
    ps -u (id -u) -o pid=,args= >$uat_dir/final-user-processes.txt
    or return 1

    # Invariant: sixty seconds after the final semantic call, no helper process or socket remains and its lifetime lock is acquirable.
    python -c 'import fcntl,sys; from pathlib import Path; processes=Path(sys.argv[1]).read_text(encoding="utf-8"); rows=[line.split(None,2) for line in processes.splitlines() if line.strip()]; helpers=[row for row in rows if len(row)==3 and Path(row[1]).name.startswith("python") and " -m cc_search_chats.semantic.query_embedder" in row[2]]; socket_path=Path(sys.argv[2]); lock_path=Path(sys.argv[3]); assert processes.strip() and not helpers; assert not socket_path.exists(); lock_path.parent.mkdir(mode=0o700,parents=True,exist_ok=True); handle=lock_path.open("a+"); fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB); print("query embedder absent; socket absent; lifetime lock acquired")' "$uat_dir/final-user-processes.txt" "$helper_runtime/query-embedder.sock" "$helper_runtime/query-embedder.lock" >$uat_dir/final-query-embedder-state.txt
    or return 1

    printf 'UAT evidence: %s\n' "$uat_dir"
end

execute_uat
```

The final status total is semantic chunks, not logical messages. The evidence
directory is intentionally retained for review.

## Human acceptance

The human reviews:

- all four expected messages and their surrounding native context;
- literal and semantic ranking usefulness, including false positives;
- staleness legibility in the retained human header and JSON `index_state`
  before and after the intentional index;
- cold semantic latency, immediate warm reuse, and the absence of a semantic
  deadline;
- coverage, warnings, and the exact roots associated with each control;
- stdout JSON and stderr NDJSON purity, including exactly one terminal event;
- the initial and final nightly timer state.
- helper process/socket release after the final semantic call.

Record an acceptance or rejection with the exact installed commit and evidence
directory. Do not infer acceptance from script exit status. Prune remains a
separate later authorization.

## Results

No production UAT has been run for this candidate.
