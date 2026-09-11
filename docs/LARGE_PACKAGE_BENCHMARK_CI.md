# CloudArc Large-Package Benchmark

**Generated:** 2026-09-11T04:48:42.492306+00:00  
**Run status:** `complete`  
**Profile:** `compressible-text`  
**Workload:** v2 / `59ac9b284e1c4a7c`  
**Overall SLO:** PASS

## Environment

- Platform: `Linux-6.8.0-137-generic-x86_64-with-glibc2.39`
- Machine: `x86_64`
- Python: `3.11.15`
- Sampling interval: `0.05` seconds

## Bounded-Memory SLO

| Requirement | Limit |
|---|---:|
| Peak RSS per pack/unpack process | 256.0 MiB |
| Peak RSS spread across tested sizes | 64.0 MiB |
| Pack staging disk | 2.10x payload + 64.0 MiB |
| Unpack staging disk | 1.10x payload + 64.0 MiB |

## Results

| Size GiB | Operation | Seconds | MiB/s | Peak RSS MiB | RSS delta MiB | Temp GiB | Temp/input | SLO |
|---:|---|---:|---:|---:|---:|---:|---:|---|
| 0.062 | pack | 2.933 | 21.8 | 27.1 | 3.1 | 0.063 | 1.002 | PASS |
| 0.062 | unpack | 0.319 | 200.5 | 26.9 | 3.3 | 0.062 | 1.000 | PASS |

## Scaling Evaluation

- Requested/completed sizes: 1/1 (PASS).
- Pack peak-RSS spread: 0.0 MiB (PASS).
- Unpack peak-RSS spread: 0.0 MiB (PASS).

## Method

- Each operation runs in a fresh child process.
- Peak RSS combines parent sampling with the worker's final OS high-water mark.
- Temporary disk counts logical bytes only inside `.cloudarc-pack-*` or `.cloudarc-unpack-*` staging directories.
- Sampling is combined with an operation-derived floor so a short atomic publication cannot be missed.
- Dataset generation and durable source/archive/output files are excluded.
- The SLO covers payload-size scaling with one file and bounded lexical vocabulary; manifest/index cardinality has a separate memory cost.
