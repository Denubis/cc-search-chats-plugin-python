# cc-search-chats-plugin-python

Last verified: 2026-02-07

## Overview

Claude Code plugin for context recovery and cross-referencing of chat history. Searches JSONL session files stored by Claude Code in `~/.claude/projects/`.

**Status:** Forked from pcvelz/cc-search-chats-plugin. Currently a bash script with embedded Python — undergoing full rewrite to a proper Python package with plugin wrapper.

## Tech Stack

- Language: Python 3.12+ (target; currently bash + embedded Python)
- Package manager: uv
- Testing: pytest
- Linting: ruff
- Type checking: ty
- Plugin framework: Claude Code plugin system

## Commands

- `uvx cc-search-chats` - Primary CLI invocation (once package is built)
- `uv run pytest` - Run tests
- `uv run ruff check .` - Lint
- `uv run ruff format .` - Format

## Project Structure

- `.claude-plugin/` - Plugin manifest and marketplace config
- `commands/` - Claude Code slash command definitions (.md) and scripts (.sh)
- `docs/design-plans/` - Design documents (starting-a-design-plan workflow)
- `.ed3d/` - Project-specific design guidance

## Claude Code Chat Data Format

Sessions are stored as JSONL files at `~/.claude/projects/<encoded-path>/<session-uuid>.jsonl`. The encoded path replaces `/` with `-` in the project's absolute path. Each line is a JSON object with `sessionId`, `timestamp`, `cwd`, and `message` (containing `role` and `content`). Content can be a string or an array of text/tool_use objects.

## Conventions

- Functional Core / Imperative Shell: pure functions for parsing and searching, side effects at the edges
- Claude Code plugin conventions: commands in `commands/*.md`, agents in `agents/*.md`, skills in `skills/*/SKILL.md`
- Design-first: use starting-a-design-plan workflow before implementation

## Boundaries

- Safe to edit: everything except `.claude-plugin/marketplace.json` (marketplace structure)
- Never touch: `~/.claude/projects/` data files (read-only access to chat history)
