# Agent instructions

## Environment evidence

- Treat observations about GPUs, mounts, services, the host environment, and
  system caches made inside a sandbox as sandbox-scoped evidence, not host
  facts.
- When host capability matters, distinguish sandbox evidence from host
  evidence. Use an available read-only host probe, such as `nvidia-smi` or
  `pg_isready`, or request a narrowly scoped read-only check. State which
  environment produced the evidence.
- An environment variable absent from an agent process is "not visible to this
  process," not "unconfigured."
