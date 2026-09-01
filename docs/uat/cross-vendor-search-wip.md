# Cross-vendor search UAT

Status: prepared, not run

This acceptance checks the exact installed candidate against standard and
Ponytail Claude/Codex native sessions. It does not authorize installation,
production migration, or prune. Append actual results only after the human
authorizes and performs UAT.

## Preconditions

- The release gate in the PostgreSQL maintenance runbook proves the installed
  CLI is the exact accepted clean-main commit with the semantic extra.
- PostgreSQL migration/storage preflight passed and legacy snapshot relations
  remain quarantined.
- The pinned model snapshot is already present in the configured cache; network
  access is disabled for runtime checks.
- These four session directories exist and remain read-only to the tool:
  `~/.claude/projects`, `~/.claude-ponytail/projects`, `~/.codex/sessions`, and
  `~/.codex-ponytail/sessions`.
- A previous full `index` completed before the append actions below. Record that
  baseline's corpus generation and `indexed_at`; it must be at least five
  minutes old when the first search begins so automatic admission is eligible.
- Record the reviewed baseline's exact deterministic blocked-source count.
  Coverage must remain partial with zero transient failures; a different count
  requires a new structural reconciliation before UAT continues.
- `cc-search-chats-refresh.service` is installed, while
  `cc-search-chats-index.timer` is disabled and inactive. Keep the timer in that
  state through human acceptance.

## Create four positive append controls

Through each native client—not by editing JSONL—send one benign, unique visible
message containing a newly generated sentinel phrase. Wait until the native
writer has completed a newline-terminated record. Record the provider, expected
session root, native session ID, and exact phrase for:

1. standard Claude;
2. Claude Ponytail;
3. standard Codex;
4. Codex Ponytail.

Do not run `index` after creating these controls. The first ranked search below
must durably request one full background update and return inside its deadline.
If the update publishes in time, the answer may use the new completed corpus;
otherwise it must use the recorded baseline, explicitly omit the new sentinel,
and report the baseline's time/age plus the continuing update. A literal-only
candidate must never become visible without matching fresh semantic state.

## Configure the exact four roots

Run in fish after replacing the four session IDs and sentinel phrases:

```fish
set -x CC_SEARCH_CLAUDE_ROOTS "$HOME/.claude/projects:$HOME/.claude-ponytail/projects"
set -x CC_SEARCH_CODEX_ROOTS "$HOME/.codex/sessions:$HOME/.codex-ponytail/sessions"

set -x UAT_CLAUDE_STANDARD_SESSION 'REPLACE'
set -x UAT_CLAUDE_STANDARD_QUERY 'REPLACE UNIQUE SENTINEL PHRASE'
set -x UAT_CLAUDE_PONYTAIL_SESSION 'REPLACE'
set -x UAT_CLAUDE_PONYTAIL_QUERY 'REPLACE UNIQUE SENTINEL PHRASE'
set -x UAT_CODEX_STANDARD_SESSION 'REPLACE'
set -x UAT_CODEX_STANDARD_QUERY 'REPLACE UNIQUE SENTINEL PHRASE'
set -x UAT_CODEX_PONYTAIL_SESSION 'REPLACE'
set -x UAT_CODEX_PONYTAIL_QUERY 'REPLACE UNIQUE SENTINEL PHRASE'
set -x UAT_EXPECTED_BLOCKED_SOURCES 'REPLACE REVIEWED BASELINE COUNT'
```

The explicit plural roots are part of the test. They include only session
directories; they do not point at either isolated home.

## Acceptance script

This script keeps stdout JSON and stderr NDJSON separate. It first proves the
bounded wait, coherent-corpus fallback, and durable systemd handoff; waits for
the full-update oneshot without starting it a second time; then pauses for one
additional native-client message in each root. A second ranked search must stay
inside the post-completion quiet period, retain the same request/corpus/build,
and leave the systemd invocation unchanged even though every native root grew.
The remaining cases positively locate the original four appended messages with
exact native resolution and fresh hybrid ranking over the partial-coverage
corpus. There is no separate semantic catch-up step.

```fish
set -g uat_dir (mktemp -d)

function assert_progress --argument-names path
    python -c 'import json,sys; e=[json.loads(x) for x in open(sys.argv[1],encoding="utf-8") if x.strip()]; assert e and all(x["schema_version"]==3 for x in e); assert [x["sequence"] for x in e]==list(range(1,len(e)+1)); assert sum(x["event"]=="terminal" for x in e)==1; assert e[-1]["event"]=="terminal"' "$path"
end

function assert_reviewed_partial_coverage --argument-names path
    python -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); expected=int(sys.argv[2]); assert d["schema_version"]==3; c=d["coverage"]; r=d["refresh"]; assert c["completeness"]=="partial" and c["blocked_files"]==expected and c["transient_failure_files"]==0; assert r["blocked_sources"]==expected and r["transient_failure_sources"]==0' "$path" "$UAT_EXPECTED_BLOCKED_SOURCES"
end

function probe_coherent_refresh
    test (systemctl --user is-enabled cc-search-chats-index.timer 2>/dev/null) = disabled
    or return 1

    set -l probe "$uat_dir/background-probe.json"
    set -l probe_progress "$uat_dir/background-probe.ndjson"
    set -l started (date +%s%N)
    cc-search-chats search "$UAT_CLAUDE_STANDARD_QUERY" --literal --provider claude --limit 200 --json >$probe 2>$probe_progress
    or return 1
    set -l finished (date +%s%N)
    set -l elapsed_ms (math "($finished - $started) / 1000000")
    test "$elapsed_ms" -lt 5000
    or return 1
    assert_progress "$probe_progress"
    or return 1
    assert_reviewed_partial_coverage "$probe"
    or return 1
    python -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); baseline=json.load(open(sys.argv[2],encoding="utf-8")); query=sys.argv[3]; assert d["status"]=="complete" and d["deadline_ms"]==5000 and d["indexed_at"] and d["corpus_age_ms"]>=0; b=d["background_refresh"]; assert b["request_id"]>0 and b["state"] in {"launching","launched","running","complete"}; current=d["refresh"]["corpus_generation"]; same=current==baseline["corpus_generation"]; assert (same and d["corpus_age_ms"]>=300000 and d["elapsed_ms"]>=4000 and all(query not in r["text"] for r in d["results"])) or ((not same) and d["semantic"]["fresh"] is True and d["semantic"]["corpus_generation"]==current and any(query in r["text"] for r in d["results"]))' "$probe" "$uat_dir/baseline-status.json" "$UAT_CLAUDE_STANDARD_QUERY"
    or return 1

    set -l wait_deadline (math (date +%s) + 900)
    while true
        set -l request_state (psql service=cc_search_chats -v ON_ERROR_STOP=1 -At -c "SELECT state FROM cc_search_chats.auto_refresh_state WHERE singleton")
        or return 1
        test "$request_state" = complete
        and break
        test "$request_state" = failed
        and return 1
        test (date +%s) -lt "$wait_deadline"
        or return 1
        sleep 0.2
    end
    while systemctl --user is-active --quiet cc-search-chats-refresh.service
        test (date +%s) -lt "$wait_deadline"
        or return 1
        sleep 0.1
    end
    test (systemctl --user show cc-search-chats-refresh.service --property=ActiveState --value) = inactive
    or return 1
    test (systemctl --user show cc-search-chats-refresh.service --property=Result --value) = success
    or return 1

    cc-search-chats index --status --json >$uat_dir/post-refresh-status.json 2>$uat_dir/post-refresh-status.ndjson
    or return 1
    assert_progress "$uat_dir/post-refresh-status.ndjson"
    or return 1
    assert_reviewed_partial_coverage "$uat_dir/post-refresh-status.json"
    or return 1
end

function root_jsonl_bytes --argument-names root
    python -c 'from pathlib import Path; import sys; print(sum(path.stat().st_size for path in Path(sys.argv[1]).rglob("*.jsonl") if path.is_file()))' "$root"
end

function probe_post_completion_quiet
    set -l roots "$HOME/.claude/projects" "$HOME/.claude-ponytail/projects" "$HOME/.codex/sessions" "$HOME/.codex-ponytail/sessions"
    set -l sizes_before
    for root in $roots
        set -a sizes_before (root_jsonl_bytes "$root")
        or return 1
    end

    read -P 'Within five minutes, send one additional benign native-client message in each of the four roots, then press Enter: ' quiet_confirmation

    set -l sizes_after
    for root in $roots
        set -a sizes_after (root_jsonl_bytes "$root")
        or return 1
    end
    for position in (seq 1 (count $roots))
        test "$sizes_after[$position]" -gt "$sizes_before[$position]"
        or return 1
    end

    set -l service_start_before (systemctl --user show cc-search-chats-refresh.service --property=ExecMainStartTimestampMonotonic --value)
    test "$service_start_before" -gt 0
    or return 1
    set -l quiet "$uat_dir/quiet-probe.json"
    set -l quiet_progress "$uat_dir/quiet-probe.ndjson"
    cc-search-chats search "$UAT_CLAUDE_STANDARD_QUERY" --literal --provider claude --limit 200 --json >$quiet 2>$quiet_progress
    or return 1
    assert_progress "$quiet_progress"
    or return 1
    assert_reviewed_partial_coverage "$quiet"
    or return 1
    python -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); prior=json.load(open(sys.argv[2],encoding="utf-8")); assert d["status"]=="complete" and d["deadline_ms"]==5000 and d["elapsed_ms"]<5000 and 0<=d["corpus_age_ms"]<300000; assert d["refresh"]["corpus_generation"]==prior["corpus_generation"]; assert d["semantic"]["semantic_build"]==prior["semantic_build"] and d["semantic"]["corpus_generation"]==prior["corpus_generation"]; current=d["background_refresh"]; previous=prior["background_refresh"]; assert current["request_id"]==previous["request_id"] and current["state"]==previous["state"]=="complete" and current["refresh_run_id"]==previous["refresh_run_id"]' "$quiet" "$uat_dir/post-refresh-status.json"
    or return 1
    test (systemctl --user show cc-search-chats-refresh.service --property=ExecMainStartTimestampMonotonic --value) = "$service_start_before"
    or return 1
end

function run_case --argument-names label provider expected_root session query
    set -l literal "$uat_dir/$label.literal.json"
    set -l literal_progress "$uat_dir/$label.literal.ndjson"
    cc-search-chats search "$query" --literal --provider "$provider" --limit 200 --json >$literal 2>$literal_progress
    or return 1
    assert_progress "$literal_progress"
    or return 1

    assert_reviewed_partial_coverage "$literal"
    or return 1
    set -l locator (python -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); assert d["schema_version"]==3 and d["command"]=="search" and d["status"]=="complete"; m=[r for r in d["results"] if r["identity"]["provider"]==sys.argv[2] and r["identity"]["source_session_id"]==sys.argv[3] and sys.argv[4] in r["text"]]; assert m; print(m[0]["identity"]["canonical_locator"])' "$literal" "$provider" "$session" "$query")
    or return 1

    set -l resolved "$uat_dir/$label.resolve.json"
    set -l resolved_progress "$uat_dir/$label.resolve.ndjson"
    cc-search-chats resolve "$locator" --reference-only --json >$resolved 2>$resolved_progress
    or return 1
    assert_progress "$resolved_progress"
    or return 1
    python -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); assert d["schema_version"]==3 and d["status"]=="resolved" and d["messages"]; m=d["messages"][0]; assert "text" not in m; assert m["identity"]["canonical_locator"]==sys.argv[2]; assert m["identity"]["physical_aliases"]' "$resolved" "$locator"
    or return 1

    set -l observed_roots (psql service=cc_search_chats -v ON_ERROR_STOP=1 -v locator="$locator" -At -c "SELECT DISTINCT root.resolved_path FROM cc_search_chats.message_current AS message JOIN cc_search_chats.physical_alias_current AS alias USING (provider, source_session_id, logical_message_id, content_class) JOIN cc_search_chats.source_root_current AS root USING (source_root_id) WHERE message.canonical_locator = :'locator' ORDER BY root.resolved_path")
    or return 1
    string match -q -- "$expected_root" $observed_roots
    or return 1

    set -l hybrid "$uat_dir/$label.hybrid.json"
    set -l hybrid_progress "$uat_dir/$label.hybrid.ndjson"
    cc-search-chats search "$query" --provider "$provider" --limit 200 --json >$hybrid 2>$hybrid_progress
    or return 1
    assert_progress "$hybrid_progress"
    or return 1
    assert_reviewed_partial_coverage "$hybrid"
    or return 1
    python -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); assert d["status"]=="complete" and d["semantic"]["fresh"] is True; m=[r for r in d["results"] if r["identity"]["canonical_locator"]==sys.argv[2]]; assert m; rank=m[0]["ranking"]; assert rank["method"]=="rrf" and rank["semantic_rank"] is not None and rank["semantic_chunk_ordinal"] is not None' "$hybrid" "$locator"
    or return 1

    printf '%s\t%s\t%s\t%s\n' "$label" "$provider" "$session" "$locator"
end

function execute_uat
    cc-search-chats index --status --json >$uat_dir/baseline-status.json 2>$uat_dir/baseline-status.ndjson
    or return 1
    assert_progress "$uat_dir/baseline-status.ndjson"
    or return 1
    python -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); assert d["schema_version"]==3 and d["corpus_generation"]>0 and d["indexed_at"] and d["corpus_age_ms"]>=300000' "$uat_dir/baseline-status.json"
    or return 1

    probe_coherent_refresh
    or return 1
    probe_post_completion_quiet
    or return 1

    run_case claude-standard claude "$HOME/.claude/projects" "$UAT_CLAUDE_STANDARD_SESSION" "$UAT_CLAUDE_STANDARD_QUERY"
    or return 1
    run_case claude-ponytail claude "$HOME/.claude-ponytail/projects" "$UAT_CLAUDE_PONYTAIL_SESSION" "$UAT_CLAUDE_PONYTAIL_QUERY"
    or return 1
    run_case codex-standard codex "$HOME/.codex/sessions" "$UAT_CODEX_STANDARD_SESSION" "$UAT_CODEX_STANDARD_QUERY"
    or return 1
    run_case codex-ponytail codex "$HOME/.codex-ponytail/sessions" "$UAT_CODEX_PONYTAIL_SESSION" "$UAT_CODEX_PONYTAIL_QUERY"
    or return 1

    cc-search-chats index --status --json >$uat_dir/final-status.json 2>$uat_dir/final-status.ndjson
    or return 1
    assert_progress "$uat_dir/final-status.ndjson"
    or return 1
    assert_reviewed_partial_coverage "$uat_dir/final-status.json"
    or return 1
    python -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); assert d["schema_version"]==3 and d["status"]=="complete"; assert d["selected"] is True and d["completed"]==d["total"]; assert d["semantic"]["fresh"] is True; assert d["coverage"]["configured_root_count"]==4 and d["coverage"]["resolved_root_count"]==4' "$uat_dir/final-status.json"
    or return 1

    test (systemctl --user is-enabled cc-search-chats-index.timer 2>/dev/null) = disabled
    or return 1

    printf 'UAT evidence: %s\n' "$uat_dir"
end

execute_uat
```

The final status total is semantic chunks, not logical messages. The evidence
directory is intentionally retained for review.

## Human acceptance

The human reviews:

- all four expected messages and their surrounding context;
- ranking usefulness and false positives;
- baseline age, bounded publication wait, coherent fallback, background
  incremental byte/vector work, model-load, and query latency;
- coverage/warnings and the exact roots associated with each control;
- stdout/stderr artifacts and final fresh semantic state.

Record an acceptance or rejection with the exact installed commit and evidence
directory. Do not infer acceptance from script exit status. Prune remains a
separate later authorization.

## Results

No production UAT has been run for this candidate.
