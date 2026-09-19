# 0006. Offload semantic model weights to CPU RAM automatically when VRAM is short

Status: Accepted
Date: 2026-09-20

## Context

The pinned `nvidia/Nemotron-3-Embed-8B-BF16` checkpoint declares 15,905,366,016
bytes of weights. The loader placed all of it on the GPU. On a 16 GB RTX 5080
Laptop GPU that left tens of megabytes free: the 2026-09-19 timer run completed
through repeated allocation failures, and the 2026-09-20 06:07 run failed in
`model_load` with 111,869,952 bytes free and exit 8. No other compute process
held the GPU.

Measurements on that GPU (2026-09-20, offloaded load, worst-case batch of four
1024-token passages): the batch peaks 520,691,712 bytes above resident weights
at every reserve tested; reserves of 1.0, 1.5, and 2.0 GiB all embed that batch
in about 2 s. Sixteen stored passages re-embedded under offload match build 14
at cosine 0.99977 or better, the same figure obtained by changing only batch
composition under one loader; different passages peak at 0.834. Offload's own
numeric contribution was not isolated from library-version differences, because
the all-GPU path no longer fits on this card.

The governing records are:

- Brian, 2026-09-20: *"can we have this offload some layers to normal ram?"*
  (native record `attachment`/`queued_command`, uuid
  `ccbe559d-331b-4dfa-90d9-2b63a317db6a`, session
  `9d65e5ae-8c1f-4135-883c-c1530e296182`; sent mid-turn, so it has no indexed
  locator).
- Brian, 2026-09-20: *"yes, do the thing, research properly, but yes, offload
  should be automatic"*
  (`ccchat:v1:claude:9d65e5ae-8c1f-4135-883c-c1530e296182:uuid:c29d7706-85f7-4d32-b9b5-01deeca44dd3`).
- Brian, 2026-09-20, on the reserve: *"um, 10%? seems fine?"* (native record
  `attachment`/`queued_command`, uuid `3a20f01a-d447-4288-a9e1-0b5f93f3a32a`,
  same session; sent mid-turn) followed by *"min 2gb 10%?"*
  (`ccchat:v1:claude:9d65e5ae-8c1f-4135-883c-c1530e296182:uuid:b7fd0971-fcc8-44f7-b404-b58a089f6ee8`).

## Decision

Model placement is automatic and has no operator switch. The loader reads the
checkpoint's declared weight size and the GPU's free and total memory. The
reserve is the larger of 2 GiB and one tenth of total VRAM. Reading "10%" as
total rather than free VRAM is the implementing agent's interpretation, stated
to Brian before implementation.

When the weights fit in free VRAM less the reserve, the model loads wholly onto
the GPU exactly as before. Otherwise it loads with `device_map="auto"`, capping
GPU weights at free VRAM less the reserve and holding the remainder in CPU RAM.
Offloaded layers are copied to the GPU for their forward pass, so computation
stays on the GPU in bf16 and the embedding profile is unchanged. The CPU cap is
the model's own weight size and no offload folder is supplied, so weights never
spill to disk; insufficient RAM fails the load loudly. When the reserve alone
exceeds free VRAM, the load fails as `vram_unavailable`.

## Consequences

The `semantic` extra requires `accelerate`, which Transformers needs for any
device map. Index and query-time embedding share the loader, so both offload.
Offloaded layers cross PCIe on every batch; the measured cost on the 16 GB card
is about 2 s per worst-case batch and a 9.7 s query-helper model load.

The reserve is a policy number owned by Brian. The 2 GiB floor exists because
the batch peak does not shrink with the card: one tenth of a 6 GB GPU would
leave almost no margin above the measured peak.
