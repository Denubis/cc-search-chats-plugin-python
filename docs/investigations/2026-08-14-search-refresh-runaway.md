# Search refresh misdiagnosis

Date: 2026-08-14 (Australia/Sydney)

The refresh was not runaway. A new Codex session correctly noticed that the
selected revision predated the requested conversation and ran the documented
idempotent `index` command. It then misread cumulative semantic progress as a
full rebuild and interrupted normal delta work.

PostgreSQL showed:

- corpus revision 9: 256,081 eligible passages, fully selected;
- corpus revision 10: 258,224 eligible passages;
- 256,081 vectors reused;
- 2,143 genuinely new passages across recent Claude and Codex sessions;
- interruption after 328 new vectors, leaving 1,815 resumable passages.

Scanning roughly 10,500 source files took about 80 seconds. Embedding the delta
was expected to take minutes. Both are reasonable for a casual explicit refresh.
The defect was ambiguous output such as `256381/258224`, which did not
distinguish reused vectors from new work or provide an ETA.

The corrected progress contract reports:

```text
Semantic refresh: 256081 reused, 2143 new passages
Semantic refresh: 256081 reused, 300 embedded, 1843 remaining, 4.8/s, ETA 6m24s
```

Explicit `index` reconciles every detected change; it does not defer new prose
behind a threshold. It remains resumable, bounded by its systemd user scope,
and keeps the last complete semantic revision selected until promotion.
