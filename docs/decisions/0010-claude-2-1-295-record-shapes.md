# 2026-10-10 — Admit Claude Code 2.1.295 record shapes immediately

> Moved from the notes directory on 2026-10-26 under ADR 0043 of the denubis-plugins
> register; links rewritten, with no other change.

**Question answered:** should the Claude parser admit the 2.1.295 `last-prompt`
(`explicit`) and `attachment` (`agentId` + `renderedRole`) shapes now, at the
cost of one more full Claude reparse, or batch it with a later parser change?

**Answer (human, verbatim):** "ummmmmmmmmmmmm, so we're making sure current
chats index, right?"

**Resolution:** fixed immediately as cc-search-chats 2.4.1
(`74f7c9276c4b474c979b937052a3de466beb5344`); Claude parser-state version 7;
production index run after install reported `needs_attention 0`,
`completeness complete`, generation 107, semantic build 92.

**Resolver:** ccchat:v1:claude:42102a00-89eb-4f30-ad4e-e12c7a65e697 (this
session, 2026-10-09/10 AEDT).
