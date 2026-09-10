# CloudArc 1.1.0 Repository Handoff

**Prepared:** 2026-09-10  
**Target repository:** public GitHub repository named `cloudarc`

A ready-to-upload source bundle is available locally as
`release/cloudarc-1.1.0-source.zip`. The `release/` directory is ignored by
Git so the archive is kept separate from the repository working tree.

## Included

- Portable CloudArc `.vibo` reference backend.
- Bounded-memory pack/unpack benchmark and SLO documentation.
- Multi-file cardinality and deduplication benchmark.
- Remote-search range/sidecar telemetry.
- Pull-request CI smoke workflow.
- Manual 1/5/10 GiB workflow.
- CloudArc Bounded-Memory Benchmark skill `1.1.0`.
- Verified CI smoke JSON and Markdown reports.

## Deliberately excluded

- `config.json` and local `.cloudarc/` state.
- Credentials, provider tokens, and native ViBo binaries.
- Old unversioned skill archives and local benchmark scratch output.

## Local verification

- Python unit suite: `51/51` passed.
- CI smoke payload SLO: `PASS`.
- Multi-file smoke SLO: `PASS`.
- Multi-file smoke cardinality: `512` manifest entries and `512` index
  documents.
- Workflow YAML parsed successfully.
- JSON and Markdown reports are generated from matching result objects.

## Publish manually

Run these commands from the extracted repository directory after installing
Git:

```powershell
git init
git branch -M main
git add -A
git commit -m "CloudArc 1.1.0: bounded-memory CI and telemetry"
git remote add origin https://github.com/<your-account>/cloudarc.git
git push -u origin main
```

If the remote already exists, use `git remote set-url origin ...` instead of
`git remote add origin ...`.

After the first push:

1. Open the **Actions** tab and confirm `CloudArc CI` completes.
2. Download the `cloudarc-ci-smoke` artifact.
3. Use **CloudArc Large Package Benchmark** with `workflow_dispatch` only when
   the runner has sufficient disk and time for the requested sizes.

The CI workflow intentionally does not require cloud credentials and does not
activate the native ViBo backend.
