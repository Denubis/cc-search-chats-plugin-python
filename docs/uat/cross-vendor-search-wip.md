# Cross-vendor search WIP UAT

## 2026-08-12T07:48:44+10:00 — unbounded literal result stream

- **Query/intended task:** Recover the original objective of the agent-submission-receipts thread using literal terms including `agent-to-agent`, `provenance`, `submitted_by`, `receipt`, `spike`, `postgres`, and `opaque`.
- **Claude/Codex scope:** Both complete native roots via `discover_claude_sources` and `discover_codex_sources`; retained visible prose parsed with the WIP provider adapters.
- **Observed result:** The adapters read both corpora, but the temporary consumer produced hundreds of thousands of broad matches. Output exceeded capture limits repeatedly, relevant August results were buried among unrelated historical uses, and the full scan was still running after roughly two minutes. It had to be interrupted, so the intended thread could not be recovered reliably.
- **Expected result:** A practical exploratory search should support narrowing before or during scanning (at least timestamp/project/role filters), bounded/ranked result collection, and enough progress/coverage information to distinguish “still scanning” from “complete.”
- **Reproduction details:** Stream every discovered source through `read_bounded_jsonl`, parse with `parse_claude_session`/`parse_codex_session`, and emit every retained prose message matching the case-insensitive union of the terms above. The process generated more output than the tool could retain and was interrupted with SIGINT. No native chat or implementation file was modified.
- **Severity:** painful
