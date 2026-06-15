---
name: search-chat
description: "Search and recover context from Claude Code chat history — use when asked about previous conversations, lost context, cross-referencing sessions, what we discussed, earlier today, yesterday's session, find where we talked about, recover from compression"
user-invocable: true
allowed-tools: ["Bash(cc-search-chats:*)"]
---

# Progressive Search Workflow

This skill guides you through finding and recovering content from Claude Code chat history. Follow the steps in order.

## Step 1: Classify the Query

Determine what the user is asking for:

| Intent | Signal phrases | Action |
|--------|---------------|--------|
| **Temporal** | "what did we discuss yesterday", "my last session", "earlier today" | `extract` with no args, or `list --days N` |
| **Topic** | "find the auth discussion", "where did we talk about database" | `search "keywords"` |
| **Recovery** | "I lost context", "the session crashed", "what was I working on" | `extract` with no args (auto-discovers recent substantial session) |
| **Hybrid** | "what did we discuss about auth yesterday" | `search "auth" --days 1` |

## Step 2: Execute Search

Run the appropriate command with `--json` for structured output:

```bash
# Topic search
cc-search-chats search "query" --json

# Recovery / temporal (auto-discovers most recent session)
cc-search-chats extract --json

# List recent sessions
cc-search-chats list --days 7 --json

# Pre-compression content specifically
cc-search-chats extract --epoch 0 --json

# Hybrid: topic + recency
cc-search-chats search "query" --days 3 --json

# Search every indexed project up front (instead of waiting for a local miss)
cc-search-chats search "query" --all --json

# Include thinking + tool calls (live full-content scan, not persisted)
cc-search-chats search "query" --everything --json
```

## Step 3: Interpret Results

Parse the JSON output and present to the user:

- **Search results**: the payload is an object `{scope, searched_project, project_count, results}` — read the `results` array. When `scope` is `widened` or `all`, tell the user the match came from another project and show each result's `project_path`. Show matching snippets with session IDs, timestamps, and epoch numbers. Explain that epoch 0 is pre-compression content.
- **Extract output**: Show the conversation with role labels. Note epoch boundaries if compression occurred.
- **Session list**: Show dates, message counts, and epoch counts for each session.

Always include session IDs so the user can drill down further.

## Step 4: Broaden if No Results

Search already broadens to **every indexed project** automatically when the current project has no hits (the result `scope` will be `widened`). If it still finds nothing:

1. **Remove epoch filter** if one was set (search across all epochs)
2. **Increase `--days` range** (try 30, then 90)
3. **Run `index --all`** if the global index may be incomplete — a project you never opened is not indexed until then
4. **Add `--everything`** to also search thinking and tool calls
5. **Try alternative keywords** (synonyms, related terms)
6. **Fall back to `list`** to show what sessions exist

```bash
# Broader search across all epochs and days
cc-search-chats search "query" --days 90 --json

# Make sure every project is indexed, then search everything
cc-search-chats index --all
cc-search-chats search "query" --all --json

# Include reasoning + tool calls
cc-search-chats search "query" --everything --json

# See what exists
cc-search-chats list --json
```

## Step 5: Drill Down

When the user wants more detail on a specific result:

```bash
# Full conversation from a session
cc-search-chats extract SESSION_ID --json

# Pre-compression content only
cc-search-chats extract SESSION_ID --epoch 0 --json

# Context around a specific message
cc-search-chats context MESSAGE_UUID --json

# More surrounding context
cc-search-chats context MESSAGE_UUID --depth 10 --json
```

## Key Concepts

- **Epoch 0**: Content from before Claude Code's compression. This is the "lost context" that users most commonly want to recover.
- **Compression boundary**: When Claude Code compresses a session, it creates a new epoch. The boundary marks where context was summarised.
- **JIT indexing**: The tool indexes the current project's sessions on first access. `index --all` builds a global index across every project (incremental, cron-friendly).
- **Project scope**: Search looks at the current project first and broadens to all indexed projects on a miss (`scope`: local / widened / all). `--all` forces machine-wide; `--project PATH` pins one project and never broadens.
- **Full-content scan**: `--everything` searches the full text — thinking blocks and tool inputs/outputs — via a throwaway in-memory index. The persistent index keeps only the clean conversation.
