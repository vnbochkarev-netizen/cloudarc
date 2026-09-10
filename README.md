# CloudArc

**Cloud layer for ViBo Archive `.vibo` knowledge packages.**

CloudArc treats a knowledge package as a cloud object: it can be packed and
unpacked with **bounded memory**, split across **multiple files** at high
cardinality, and searched **remotely** by reading only headers, manifests and
indexes through HTTP range requests - never the data section.

Author: **Viacheslav Bochkarev**

## Repository layout

| Ref | Contents |
|---|---|
| `main` | Repository card, license, CI workflow (this file). |
| `release/1.1.1` | Current published package: workflow skill, benchmark/telemetry helper, contract and release notes. |

## What the workflow enforces

The `release/*` branch ships `scripts/cloudarc_benchmark.py` - a stdlib-only
helper with three entry points:

| Command | Purpose |
|---|---|
| `smoke` | Run package tests and the small benchmarks inside a CloudArc checkout. |
| `manual` | Run the explicit large benchmark and the multi-file profile (requires `--yes`). |
| `check-telemetry` | Validate a remote-search telemetry response against the contract. |

Every push runs CI (`.github/workflows/ci.yml`): byte-compile, the CLI contract,
version consistency, a well-formed telemetry response, and **two fail-closed
checks** - a response that read the data section, and a range/sidecar mismatch,
must both be rejected.

## Documented limits (contract, not measurements)

| Limit | Value |
|---|---|
| Peak RSS (pack & unpack) | <= 256 MiB |
| 1/5/10 GiB peak-RSS spread | <= 64 MiB |
| Pack staging | <= 2.10 * payload + 64 MiB |
| Unpack staging | <= 1.10 * payload + 64 MiB |

> **Honest status:** these numbers are the **CloudArc SLO contract** the workflow
> enforces - documented target values, not measurements produced by this
> repository. Real numbers come from running `smoke` / `manual` inside a CloudArc
> checkout where `benchmarks/large_package.py` exists. Until then, treat the
> table as the contract, not as a measured result.

## Remote-search rule

Remote search must read **header, manifest and index only**. The data section is
never fetched for metadata queries; the telemetry validator rejects any response
that claims otherwise.
