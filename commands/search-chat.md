---
description: "Search previous sessions, chat history, last session, earlier conversation, before I made, what we discussed, find where we talked about, previous chat, old session, yesterday's session, recover context, lost context, compression"
allowed-tools: ["Bash(cc-search-chats:*)"]
---

# Search Chat History

Search, recover, and extract content from Claude Code chat sessions.

**Arguments provided:** $ARGUMENTS

## Subcommand Routing

Determine the user's intent and route to the appropriate subcommand. Always use the `--json` flag for structured output.

### "search X" / "find where we discussed X"

```bash
cc-search-chats search "X" --json
```

Optional flags: `--epoch N` (filter to epoch), `--days N` (limit recency), `--project PATH`.

### "what was in my last session" / "recover context"

```bash
cc-search-chats extract --json
```

With no session ID, auto-discovers the most recent substantial session. Optional: `--epoch 0` for pre-compression content.

### "show my recent sessions"

```bash
cc-search-chats list --json
```

Optional: `--days N` (default: all), `--project PATH`.

### "show context around message X"

```bash
cc-search-chats context UUID --json
```

Optional: `--depth N` (surrounding messages, default 5).

### "rebuild the index"

```bash
cc-search-chats index --json
```

Force a full reindex of the current project.

## Instructions

1. If no arguments provided, ask the user what they want to search for or recover.

2. Classify the user's request and run the appropriate command above with `--json`.

3. Parse the JSON output and present results in a readable format:
   - For **search results**: show snippets with session IDs, epoch info, and timestamps. Offer to extract specific sessions.
   - For **extract**: show the conversation with role labels. Mention epoch boundaries if present.
   - For **list**: show a table of sessions with dates, message counts, and epoch counts.
   - For **context**: show the target message with surrounding conversation.

4. Include session IDs in output so the user can drill down further (e.g. `extract <session-id>`).

5. If no results found, suggest broadening the search: increase `--days`, try alternative keywords, or use `list` to see what sessions exist.

## Examples

- `/search-chat "database migration"` -- find discussions about database migrations
- `/search-chat "auth" --epoch 0` -- find pre-compression auth discussions
- `/search-chat recover context` -- extract the most recent substantial session
- `/search-chat list recent sessions` -- show recent sessions
