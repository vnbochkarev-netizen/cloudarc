# CloudArc Large-Package Benchmark

**Generated:** 2026-09-09T16:40:14.766191+00:00  
**Run status:** `complete`  
**Profile:** `compressible-text`  
**Workload:** v2 / `59ac9b284e1c4a7c`  
**Overall SLO:** PASS

## Environment

- Platform: `Windows-11-10.0.26200-SP0`
- Machine: `AMD64`
- Python: `3.14.0`
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
| 1.000 | pack | 33.496 | 30.6 | 26.0 | 3.0 | 1.002 | 1.002 | PASS |
| 1.000 | unpack | 1.421 | 720.6 | 29.7 | 6.8 | 1.000 | 1.000 | PASS |
| 5.000 | pack | 183.054 | 28.0 | 25.8 | 2.8 | 5.008 | 1.002 | PASS |
| 5.000 | unpack | 75.702 | 67.6 | 31.0 | 8.1 | 5.000 | 1.000 | PASS |
| 10.000 | pack | 488.938 | 20.9 | 25.9 | 2.8 | 10.015 | 1.002 | PASS |
| 10.000 | unpack | 494.140 | 20.7 | 31.2 | 8.2 | 10.000 | 1.000 | PASS |

## Scaling Evaluation

- Requested/completed sizes: 3/3 (PASS).
- Pack peak-RSS spread: 0.2 MiB (PASS).
- Unpack peak-RSS spread: 1.5 MiB (PASS).

## Method

- Each operation runs in a fresh child process.
- Peak RSS combines parent sampling with the worker's final OS high-water mark.
- Temporary disk counts logical bytes only inside `.cloudarc-pack-*` or `.cloudarc-unpack-*` staging directories.
- Sampling is combined with an operation-derived floor so a short atomic publication cannot be missed.
- Dataset generation and durable source/archive/output files are excluded.
- The SLO covers payload-size scaling with one file and bounded lexical vocabulary; manifest/index cardinality has a separate memory cost.
