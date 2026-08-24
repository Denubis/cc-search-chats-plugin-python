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
- A previous `index --literal-only` and `index --semantic-only` completed before
  the append actions below. Record that baseline's corpus/semantic IDs.

## Create four positive append controls

Through each native client—not by editing JSONL—send one benign, unique visible
message containing a newly generated sentinel phrase. Wait until the native
writer has completed a newline-terminated record. Record the provider, expected
session root, native session ID, and exact phrase for:

1. standard Claude;
2. Claude Ponytail;
3. standard Codex;
4. Codex Ponytail.

Do not run `index` after creating these controls. The first searches below must
discover the appended records on demand.

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
```

The explicit plural roots are part of the test. They include only session
directories; they do not point at either isolated home.

## Acceptance script

This script keeps stdout JSON and stderr NDJSON separate. It positively locates
the expected provider/session/message, verifies exact native resolution, checks
the result has a physical alias under the expected root through PostgreSQL, and
requires one terminal progress event. It then repeats the same control through
hybrid search.

```fish
set -g uat_dir (mktemp -d)

function assert_progress --argument-names path
    python -c 'import json,sys; e=[json.loads(x) for x in open(sys.argv[1],encoding="utf-8") if x.strip()]; assert e; assert [x["sequence"] for x in e]==list(range(1,len(e)+1)); assert sum(x["event"]=="terminal" for x in e)==1; assert e[-1]["event"]=="terminal"' "$path"
end

function run_case --argument-names label provider expected_root session query
    set -l literal "$uat_dir/$label.literal.json"
    set -l literal_progress "$uat_dir/$label.literal.ndjson"
    cc-search-chats search "$query" --literal --provider "$provider" --limit 200 --json >$literal 2>$literal_progress
    or return 1
    assert_progress "$literal_progress"
    or return 1

    set -l locator (python -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); assert d["schema_version"]==2 and d["command"]=="search" and d["status"]=="complete"; assert d["coverage"]["completeness"]=="complete"; m=[r for r in d["results"] if r["identity"]["provider"]==sys.argv[2] and r["identity"]["source_session_id"]==sys.argv[3] and sys.argv[4] in r["text"]]; assert m; print(m[0]["identity"]["canonical_locator"])' "$literal" "$provider" "$session" "$query")
    or return 1

    set -l resolved "$uat_dir/$label.resolve.json"
    set -l resolved_progress "$uat_dir/$label.resolve.ndjson"
    cc-search-chats resolve "$locator" --reference-only --json >$resolved 2>$resolved_progress
    or return 1
    assert_progress "$resolved_progress"
    or return 1
    python -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); assert d["status"]=="resolved" and d["messages"]; m=d["messages"][0]; assert "text" not in m; assert m["identity"]["canonical_locator"]==sys.argv[2]; assert m["identity"]["physical_aliases"]' "$resolved" "$locator"
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
    python -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); assert d["status"]=="complete" and d["semantic"]["fresh"] is True; m=[r for r in d["results"] if r["identity"]["canonical_locator"]==sys.argv[2]]; assert m; rank=m[0]["ranking"]; assert rank["method"]=="rrf" and rank["semantic_rank"] is not None and rank["semantic_chunk_ordinal"] is not None' "$hybrid" "$locator"
    or return 1

    printf '%s\t%s\t%s\t%s\n' "$label" "$provider" "$session" "$locator"
end

function execute_uat
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
    python -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); assert d["schema_version"]==2 and d["status"]=="complete"; assert d["selected"] is True and d["completed"]==d["total"]; assert d["semantic"]["fresh"] is True; assert d["coverage"]["configured_root_count"]==4 and d["coverage"]["resolved_root_count"]==4 and d["coverage"]["completeness"]=="complete"' "$uat_dir/final-status.json"
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
- baseline, on-demand refresh, model-load, and query latency;
- coverage/warnings and the exact roots associated with each control;
- stdout/stderr artifacts and final fresh semantic state.

Record an acceptance or rejection with the exact installed commit and evidence
directory. Do not infer acceptance from script exit status. Prune remains a
separate later authorization.

## Results

No production UAT has been run for this candidate.
