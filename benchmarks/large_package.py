"""Measure CloudArc peak RSS and staging-disk use on large payloads."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from benchmarks.resource_monitor import (
    ProcessMemoryReader,
    ResourceMeasurementError,
    current_process_memory,
    matching_directory_size,
)


MIB = 1024**2
GIB = 1024**3
SCHEMA = "cloudarc.large-package-benchmark"
SCHEMA_VERSION = 1
DEFAULT_SIZES_GIB = (1, 5, 10)
DEFAULT_SAMPLE_INTERVAL_SECONDS = 0.05
DEFAULT_TIMEOUT_SECONDS = 2 * 60 * 60
DATASET_BUFFER_SIZE = MIB
WORKLOAD_SCHEMA_VERSION = 1
MULTI_FILE_SLO = {
    "peak_rss_bytes_max": 512 * MIB,
    "pack_temp_ratio_max": 2.50,
    "unpack_temp_ratio_max": 1.25,
    "max_manifest_entries": 10_000,
    "max_index_documents": 10_000,
}

SLO = {
    "peak_rss_bytes_max": 256 * MIB,
    "peak_rss_spread_bytes_max": 64 * MIB,
    "pack_temp_ratio_max": 2.10,
    "unpack_temp_ratio_max": 1.10,
    "temp_fixed_allowance_bytes": 64 * MIB,
}

_TEXT_MARKER = b"cloudarc benchmark bounded memory streaming payload\n"
_TEXT_SEED_SIZE = 64 * 1024
_TEXT_SEED = _TEXT_MARKER + b" " * (_TEXT_SEED_SIZE - len(_TEXT_MARKER))

PROFILES = {
    "compressible-text": {
        "suffix": ".txt",
        "block": _TEXT_SEED,
        "description": (
            "sparse-token, highly compressible UTF-8 text for payload-scaling "
            "and deflate expansion"
        ),
        "profile_version": 2,
        "free_space_factor": 2.10,
    },
    "stored-binary": {
        "suffix": ".bin",
        "block": bytes(range(256)),
        "description": "deterministic opaque bytes stored without compression",
        "profile_version": 1,
        "free_space_factor": 3.10,
    },
}


class BenchmarkError(RuntimeError):
    """Raised when a benchmark run cannot produce trustworthy measurements."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_size(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BenchmarkError("benchmark size must be a positive integer")
    return value


def _validate_positive_number(value: int | float, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise BenchmarkError(f"{name} must be a finite positive number")
    return float(value)


def _profile_metadata(profile: str) -> dict:
    definition = PROFILES[profile]
    seed = definition["block"]
    return {
        "schema_version": WORKLOAD_SCHEMA_VERSION,
        "profile_version": definition["profile_version"],
        "description": definition["description"],
        "suffix": definition["suffix"],
        "generator_buffer_bytes": DATASET_BUFFER_SIZE,
        "seed_bytes": len(seed),
        "seed_sha256": hashlib.sha256(seed).hexdigest(),
    }


def _environment_metadata() -> dict:
    return {
        "platform": sys.platform,
        "platform_detail": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "processor": platform.processor(),
    }


def generate_dataset(
    path: str | Path,
    size_bytes: int,
    *,
    profile: str,
    buffer_size: int = DATASET_BUFFER_SIZE,
) -> float:
    """Generate deterministic test data without materializing it in memory."""

    size_bytes = _validate_size(size_bytes)
    if profile not in PROFILES:
        raise BenchmarkError(f"unsupported benchmark profile: {profile}")
    if (
        isinstance(buffer_size, bool)
        or not isinstance(buffer_size, int)
        or buffer_size <= 0
    ):
        raise BenchmarkError("dataset buffer size must be positive")

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    seed = PROFILES[profile]["block"]
    repeats = max(1, (buffer_size + len(seed) - 1) // len(seed))
    block = (seed * repeats)[:buffer_size]
    remaining = size_bytes
    started = time.perf_counter()
    with target.open("wb") as file_obj:
        while remaining:
            payload = block if remaining >= len(block) else block[:remaining]
            file_obj.write(payload)
            remaining -= len(payload)
        file_obj.flush()
        os.fsync(file_obj.fileno())
    return time.perf_counter() - started


def _free_space_required(size_bytes: int, profile: str) -> int:
    factor = float(PROFILES[profile]["free_space_factor"])
    return int(size_bytes * factor) + GIB


def _preflight_disk(work_root: Path, size_bytes: int, profile: str) -> dict:
    usage = shutil.disk_usage(work_root)
    required = _free_space_required(size_bytes, profile)
    if usage.free < required:
        raise BenchmarkError(
            "insufficient free disk for benchmark: "
            f"available={usage.free} required={required}"
        )
    return {
        "free_bytes": usage.free,
        "required_bytes": required,
    }


def _worker_command(
    operation: str,
    *,
    source: Path,
    archive: Path,
    output: Path,
    dedup: bool = False,
) -> list[str]:
    command = [
        sys.executable,
        "-B",
        "-m",
        "benchmarks.large_package",
        "_worker",
        operation,
        "--source",
        str(source),
        "--archive",
        str(archive),
        "--output",
        str(output),
    ]
    if dedup:
        command.append("--dedup")
    return command


def _run_worker(
    operation: str,
    *,
    source: Path,
    archive: Path,
    output: Path,
    temp_parent: Path,
    temp_prefixes: tuple[str, ...],
    sample_interval_seconds: float,
    timeout_seconds: float,
    dedup: bool = False,
) -> dict:
    if operation not in {"pack", "unpack"}:
        raise BenchmarkError(f"unsupported operation: {operation}")
    sample_interval_seconds = _validate_positive_number(
        sample_interval_seconds,
        "sample interval",
    )
    timeout_seconds = _validate_positive_number(
        timeout_seconds,
        "operation timeout",
    )

    repo_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(repo_root)
        if not python_path
        else os.pathsep.join((str(repo_root), python_path))
    )
    started = time.perf_counter()
    process = subprocess.Popen(
        _worker_command(
            operation,
            source=source,
            archive=archive,
            output=output,
            dedup=dedup,
        ),
        cwd=repo_root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    peak_rss_bytes = 0
    peak_temp_bytes = 0
    sample_count = 0
    memory_source = ""
    deadline = started + timeout_seconds
    reader = None
    try:
        try:
            reader = ProcessMemoryReader(process.pid)
        except ResourceMeasurementError:
            if process.poll() is None:
                raise
            # A very short operation may finish before the parent can open its
            # process handle. The worker high-water mark remains authoritative.
            reader = None
        while True:
            if reader is None:
                memory = None
            else:
                try:
                    memory = reader.sample()
                except ResourceMeasurementError:
                    if process.poll() is None:
                        raise
                    memory = None
            if memory is not None:
                peak_rss_bytes = max(
                    peak_rss_bytes,
                    memory.rss_bytes,
                    memory.peak_rss_bytes,
                )
                memory_source = memory.source
            peak_temp_bytes = max(
                peak_temp_bytes,
                matching_directory_size(temp_parent, temp_prefixes),
            )
            sample_count += 1
            if process.poll() is not None:
                break
            if time.perf_counter() >= deadline:
                raise BenchmarkError(
                    f"{operation} exceeded timeout of "
                    f"{timeout_seconds} seconds"
                )
            time.sleep(sample_interval_seconds)
        if reader is None:
            memory = None
        else:
            try:
                memory = reader.sample()
            except ResourceMeasurementError:
                memory = None
        if memory is not None:
            peak_rss_bytes = max(
                peak_rss_bytes,
                memory.rss_bytes,
                memory.peak_rss_bytes,
            )
            memory_source = memory.source
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.communicate()
        raise
    finally:
        if reader is not None:
            reader.close()

    stdout, stderr = process.communicate()
    wall_seconds = time.perf_counter() - started
    if process.returncode != 0:
        raise BenchmarkError(
            f"{operation} worker failed with code {process.returncode}: "
            f"{stderr.strip() or stdout.strip()}"
        )
    try:
        worker = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise BenchmarkError(
            f"{operation} worker returned invalid JSON: {stdout[:500]!r}"
        ) from exc
    if not isinstance(worker, dict) or worker.get("status") != "ok":
        raise BenchmarkError(f"{operation} worker did not report success")
    if worker.get("operation") != operation:
        raise BenchmarkError(f"{operation} worker reported the wrong operation")
    baseline_rss = worker.get("baseline_rss_bytes")
    if (
        isinstance(baseline_rss, bool)
        or not isinstance(baseline_rss, int)
        or baseline_rss <= 0
    ):
        raise BenchmarkError(f"{operation} worker baseline RSS is invalid")
    worker_peak_rss = worker.get("peak_rss_bytes")
    if (
        isinstance(worker_peak_rss, bool)
        or not isinstance(worker_peak_rss, int)
        or worker_peak_rss < baseline_rss
    ):
        raise BenchmarkError(f"{operation} worker peak RSS is invalid")
    worker_memory_source = worker.get("rss_measurement_source")
    if not isinstance(worker_memory_source, str) or not worker_memory_source:
        raise BenchmarkError(f"{operation} worker RSS source is invalid")
    operation_seconds = worker.get("operation_seconds")
    if (
        isinstance(operation_seconds, bool)
        or not isinstance(operation_seconds, (int, float))
        or not math.isfinite(operation_seconds)
        or operation_seconds <= 0
    ):
        raise BenchmarkError(f"{operation} worker duration is invalid")
    peak_rss_bytes = max(peak_rss_bytes, baseline_rss, worker_peak_rss)
    memory_source = memory_source or worker_memory_source
    worker_result = worker.get("result", {})
    if not isinstance(worker_result, dict):
        raise BenchmarkError(f"{operation} worker result is invalid")
    temp_floor = worker_result.get("temp_peak_floor_bytes", 0)
    if (
        isinstance(temp_floor, bool)
        or not isinstance(temp_floor, int)
        or temp_floor <= 0
    ):
        raise BenchmarkError(f"{operation} temp disk floor is invalid")
    sampled_temp_bytes = peak_temp_bytes
    peak_temp_bytes = max(peak_temp_bytes, temp_floor)
    return {
        "operation": operation,
        "wall_seconds": round(wall_seconds, 6),
        "operation_seconds": operation_seconds,
        "baseline_rss_bytes": baseline_rss,
        "worker_peak_rss_bytes": worker_peak_rss,
        "peak_rss_bytes": peak_rss_bytes,
        "rss_delta_bytes": max(0, peak_rss_bytes - baseline_rss),
        "peak_temp_bytes": peak_temp_bytes,
        "sampled_peak_temp_bytes": sampled_temp_bytes,
        "operation_temp_floor_bytes": temp_floor,
        "temp_measurement_method": "sampled-with-operation-floor",
        "sample_count": sample_count,
        "rss_measurement_source": memory_source,
        "worker": worker_result,
    }


def _operation_slo(
    operation: str,
    measurement: dict,
    size_bytes: int,
) -> dict:
    ratio_limit = (
        SLO["pack_temp_ratio_max"]
        if operation == "pack"
        else SLO["unpack_temp_ratio_max"]
    )
    temp_limit = int(
        size_bytes * ratio_limit + SLO["temp_fixed_allowance_bytes"]
    )
    rss_pass = measurement["peak_rss_bytes"] <= SLO["peak_rss_bytes_max"]
    temp_pass = measurement["peak_temp_bytes"] <= temp_limit
    return {
        "peak_rss_pass": rss_pass,
        "peak_temp_pass": temp_pass,
        "peak_temp_limit_bytes": temp_limit,
        "pass": rss_pass and temp_pass,
    }


def run_case(
    size_bytes: int,
    *,
    profile: str = "compressible-text",
    work_root: str | Path | None = None,
    sample_interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Run isolated pack and unpack measurements for one payload size."""

    size_bytes = _validate_size(size_bytes)
    if profile not in PROFILES:
        raise BenchmarkError(f"unsupported benchmark profile: {profile}")
    sample_interval_seconds = _validate_positive_number(
        sample_interval_seconds,
        "sample interval",
    )
    timeout_seconds = _validate_positive_number(
        timeout_seconds,
        "operation timeout",
    )
    base = Path(work_root or tempfile.gettempdir()).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    disk_preflight = _preflight_disk(base, size_bytes, profile)
    case_started = time.perf_counter()
    with tempfile.TemporaryDirectory(
        prefix="cloudarc-large-benchmark-",
        dir=base,
    ) as temp:
        case_root = Path(temp)
        source = case_root / f"payload{PROFILES[profile]['suffix']}"
        archive_dir = case_root / "archive"
        archive_dir.mkdir()
        archive = archive_dir / "payload.vibo"
        restored = case_root / "restored"
        generation_seconds = generate_dataset(
            source,
            size_bytes,
            profile=profile,
        )
        pack_result = _run_worker(
            "pack",
            source=source,
            archive=archive,
            output=restored,
            temp_parent=archive_dir,
            temp_prefixes=(".cloudarc-pack-",),
            sample_interval_seconds=sample_interval_seconds,
            timeout_seconds=timeout_seconds,
        )
        if pack_result["worker"].get("raw_bytes") != size_bytes:
            raise BenchmarkError("pack worker raw size does not match test payload")
        if pack_result["worker"].get("packed_bytes") != archive.stat().st_size:
            raise BenchmarkError("pack worker archive size does not match output")
        pack_result["throughput_mib_per_second"] = round(
            size_bytes / MIB / pack_result["operation_seconds"],
            3,
        )
        pack_result["temp_to_input_ratio"] = round(
            pack_result["peak_temp_bytes"] / size_bytes,
            6,
        )
        pack_result["slo"] = _operation_slo(
            "pack",
            pack_result,
            size_bytes,
        )

        unpack_result = _run_worker(
            "unpack",
            source=source,
            archive=archive,
            output=restored,
            temp_parent=case_root,
            temp_prefixes=(".cloudarc-unpack-",),
            sample_interval_seconds=sample_interval_seconds,
            timeout_seconds=timeout_seconds,
        )
        if unpack_result["worker"].get("restored") != 1:
            raise BenchmarkError("unpack worker restored an unexpected entry count")
        unpack_result["throughput_mib_per_second"] = round(
            size_bytes / MIB / unpack_result["operation_seconds"],
            3,
        )
        unpack_result["temp_to_input_ratio"] = round(
            unpack_result["peak_temp_bytes"] / size_bytes,
            6,
        )
        unpack_result["slo"] = _operation_slo(
            "unpack",
            unpack_result,
            size_bytes,
        )

        restored_file = restored / source.name
        if not restored_file.is_file() or restored_file.stat().st_size != size_bytes:
            raise BenchmarkError("unpack output size does not match test payload")
        archive_size = archive.stat().st_size

    return {
        "status": "ok",
        "size_bytes": size_bytes,
        "size_gib": round(size_bytes / GIB, 6),
        "profile": profile,
        "generation_seconds": round(generation_seconds, 6),
        "archive_size_bytes": archive_size,
        "disk_preflight": disk_preflight,
        "pack": pack_result,
        "unpack": unpack_result,
        "case_seconds": round(time.perf_counter() - case_started, 6),
    }


def _generate_multi_file_dataset(
    root: Path,
    *,
    file_count: int,
    file_size_bytes: int,
    duplicate_groups: int,
) -> dict:
    if file_count <= 0 or file_size_bytes <= 0:
        raise BenchmarkError("multi-file dimensions must be positive")
    duplicate_groups = max(1, min(duplicate_groups, file_count))
    source = root / "multi-source"
    source.mkdir(parents=True, exist_ok=True)
    seed = _TEXT_SEED
    block = (seed * ((file_size_bytes + len(seed) - 1) // len(seed)))[:file_size_bytes]
    for position in range(file_count):
        group = position % duplicate_groups
        target = source / f"dir-{position // 100:04d}" / f"file-{position:06d}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = block if group == 0 else (
            f"cloudarc multi-file group {group} position {position}\n".encode()
            * (
                (
                    file_size_bytes
                    + len(
                        f"cloudarc multi-file group {group} position {position}\n".encode()
                    )
                    - 1
                )
                // len(
                    f"cloudarc multi-file group {group} position {position}\n".encode()
                )
            )
        )[:file_size_bytes]
        if group == 0:
            payload = block
        target.write_bytes(payload)
    return {
        "source": source,
        "file_count": file_count,
        "file_size_bytes": file_size_bytes,
        "duplicate_groups": duplicate_groups,
        "raw_bytes": file_count * file_size_bytes,
    }


def run_multi_file_case(
    *,
    file_count: int = 512,
    file_size_bytes: int = 64 * 1024,
    duplicate_groups: int = 16,
    work_root: str | Path | None = None,
    sample_interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Measure high-cardinality, deduplicated directory packaging."""
    base = Path(work_root or tempfile.gettempdir()).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cloudarc-multi-benchmark-", dir=base) as temp:
        root = Path(temp)
        dataset = _generate_multi_file_dataset(
            root,
            file_count=file_count,
            file_size_bytes=file_size_bytes,
            duplicate_groups=duplicate_groups,
        )
        archive_dir = root / "archive"
        archive_dir.mkdir()
        archive = archive_dir / "multi.vibo"
        restored = root / "restored"
        pack_result = _run_worker(
            "pack",
            source=dataset["source"],
            archive=archive,
            output=restored,
            temp_parent=archive_dir,
            temp_prefixes=(".cloudarc-pack-",),
            sample_interval_seconds=sample_interval_seconds,
            timeout_seconds=timeout_seconds,
            dedup=True,
        )
        unpack_result = _run_worker(
            "unpack",
            source=dataset["source"],
            archive=archive,
            output=restored,
            temp_parent=archive_dir,
            temp_prefixes=(".cloudarc-unpack-",),
            sample_interval_seconds=sample_interval_seconds,
            timeout_seconds=timeout_seconds,
        )
        pack_payload = pack_result["worker"]
        dedup_entries = int(pack_payload.get("dedup_entries", 0))
        unique_entries = dataset["file_count"] - dedup_entries
        return {
            "status": "ok",
            "profile": "multi-file-dedup",
            "file_count": dataset["file_count"],
            "file_size_bytes": dataset["file_size_bytes"],
            "raw_bytes": dataset["raw_bytes"],
            "duplicate_groups": dataset["duplicate_groups"],
            "dedup_entries": dedup_entries,
            "unique_entries": unique_entries,
            "dedup_ratio": round(
                dedup_entries / dataset["file_count"], 6
            ),
            "manifest_entry_count": pack_payload.get("manifest_entry_count"),
            "index_document_count": pack_payload.get("index_document_count"),
            "index_term_count": pack_payload.get("index_term_count"),
            "pack": pack_result,
            "unpack": unpack_result,
            "slo": {
                "peak_rss_pass": pack_result["peak_rss_bytes"] <= MULTI_FILE_SLO["peak_rss_bytes_max"]
                and unpack_result["peak_rss_bytes"] <= MULTI_FILE_SLO["peak_rss_bytes_max"],
                "manifest_cardinality_pass": pack_payload.get("manifest_entry_count", 0)
                <= MULTI_FILE_SLO["max_manifest_entries"],
                "index_cardinality_pass": pack_payload.get("index_document_count", 0)
                <= MULTI_FILE_SLO["max_index_documents"],
            },
        }


def run_multi_file_benchmark(**kwargs) -> dict:
    result = run_multi_file_case(**kwargs)
    result["evaluation"] = {
        "pass": all(result["slo"].values()),
        "slo": dict(MULTI_FILE_SLO),
    }
    return result


def evaluate_benchmark(
    runs: list[dict],
    *,
    expected_sizes_bytes: list[int] | None = None,
) -> dict:
    completed = [run for run in runs if run.get("status") == "ok"]
    completed_sizes = sorted(
        run["size_bytes"]
        for run in completed
        if isinstance(run.get("size_bytes"), int)
    )
    if expected_sizes_bytes is None:
        requested_runs = len(runs)
        missing_sizes: list[int] = []
        all_requested_completed = len(completed) == len(runs) and bool(runs)
    else:
        expected_sizes = sorted(
            {_validate_size(value) for value in expected_sizes_bytes}
        )
        requested_runs = len(expected_sizes)
        completed_set = set(completed_sizes)
        missing_sizes = [
            size for size in expected_sizes if size not in completed_set
        ]
        all_requested_completed = (
            not missing_sizes
            and len(completed_sizes) == len(expected_sizes)
            and all(run.get("status") == "ok" for run in runs)
        )
    operation_results: dict[str, dict] = {}
    for operation in ("pack", "unpack"):
        valid_measurements = []
        for run in completed:
            measurement = run.get(operation)
            if not isinstance(measurement, dict):
                continue
            peak = measurement.get("peak_rss_bytes")
            slo = measurement.get("slo")
            if (
                isinstance(peak, int)
                and not isinstance(peak, bool)
                and isinstance(slo, dict)
                and isinstance(slo.get("pass"), bool)
            ):
                valid_measurements.append((peak, slo["pass"]))
        peaks = [peak for peak, _slo_pass in valid_measurements]
        spread = max(peaks) - min(peaks) if peaks else None
        per_run_pass = (
            len(valid_measurements) == len(completed)
            and all(slo_pass for _peak, slo_pass in valid_measurements)
        )
        spread_pass = (
            spread is not None
            and spread <= SLO["peak_rss_spread_bytes_max"]
        )
        operation_results[operation] = {
            "completed_runs": len(peaks),
            "peak_rss_spread_bytes": spread,
            "peak_rss_spread_pass": spread_pass,
            "per_run_pass": per_run_pass,
            "pass": bool(peaks) and per_run_pass and spread_pass,
        }
    return {
        "requested_runs": requested_runs,
        "completed_runs": len(completed),
        "completed_sizes_bytes": completed_sizes,
        "missing_sizes_bytes": missing_sizes,
        "all_requested_completed": all_requested_completed,
        "pack": operation_results["pack"],
        "unpack": operation_results["unpack"],
        "pass": (
            all_requested_completed
            and operation_results["pack"]["pass"]
            and operation_results["unpack"]["pass"]
        ),
    }


def _benchmark_result(
    normalized_sizes: list[int],
    runs: list[dict],
    *,
    profile: str,
    sample_interval_seconds: float,
    timeout_seconds: float,
) -> dict:
    evaluation = evaluate_benchmark(
        runs,
        expected_sizes_bytes=normalized_sizes,
    )
    has_failure = any(
        run.get("status") in {"failed", "skipped"} for run in runs
    )
    if evaluation["all_requested_completed"]:
        completion_status = "complete"
    elif has_failure:
        completion_status = "failed"
    else:
        completion_status = "in_progress"
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "completion_status": completion_status,
        "environment": _environment_metadata(),
        "settings": {
            "profile": profile,
            "sizes_bytes": normalized_sizes,
            "sample_interval_seconds": sample_interval_seconds,
            "operation_timeout_seconds": timeout_seconds,
            "workload": _profile_metadata(profile),
        },
        "slo": dict(SLO),
        "runs": list(runs),
        "evaluation": evaluation,
    }


def run_benchmark(
    sizes_bytes: list[int],
    *,
    profile: str = "compressible-text",
    work_root: str | Path | None = None,
    sample_interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    checkpoint: Callable[[dict], None] | None = None,
    initial_runs: list[dict] | None = None,
) -> dict:
    if not sizes_bytes:
        raise BenchmarkError("at least one benchmark size is required")
    if profile not in PROFILES:
        raise BenchmarkError(f"unsupported benchmark profile: {profile}")
    normalized_sizes = sorted({_validate_size(value) for value in sizes_bytes})
    sample_interval_seconds = _validate_positive_number(
        sample_interval_seconds,
        "sample interval",
    )
    timeout_seconds = _validate_positive_number(
        timeout_seconds,
        "operation timeout",
    )
    runs: list[dict] = []
    completed_sizes: set[int] = set()
    for run in initial_runs or []:
        if not isinstance(run, dict) or run.get("status") != "ok":
            raise BenchmarkError("resume checkpoint contains a non-successful run")
        size_bytes = run.get("size_bytes")
        if (
            isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes not in normalized_sizes
            or size_bytes in completed_sizes
        ):
            raise BenchmarkError("resume checkpoint contains an invalid size")
        if run.get("profile") != profile:
            raise BenchmarkError("resume checkpoint profile does not match")
        if not isinstance(run.get("pack"), dict) or not isinstance(
            run.get("unpack"),
            dict,
        ):
            raise BenchmarkError("resume checkpoint measurement is incomplete")
        runs.append(run)
        completed_sizes.add(size_bytes)
    runs.sort(key=lambda run: run["size_bytes"])
    pending_sizes = [
        size for size in normalized_sizes if size not in completed_sizes
    ]
    for position, size_bytes in enumerate(pending_sizes):
        try:
            run = run_case(
                size_bytes,
                profile=profile,
                work_root=work_root,
                sample_interval_seconds=sample_interval_seconds,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            runs.append(
                {
                    "status": "failed",
                    "size_bytes": size_bytes,
                    "size_gib": round(size_bytes / GIB, 6),
                    "profile": profile,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            for skipped_size in pending_sizes[position + 1 :]:
                runs.append(
                    {
                        "status": "skipped",
                        "size_bytes": skipped_size,
                        "size_gib": round(skipped_size / GIB, 6),
                        "profile": profile,
                        "error": "skipped after an earlier benchmark failure",
                    }
                )
            if checkpoint is not None:
                checkpoint(
                    _benchmark_result(
                        normalized_sizes,
                        runs,
                        profile=profile,
                        sample_interval_seconds=sample_interval_seconds,
                        timeout_seconds=timeout_seconds,
                    )
                )
            break
        runs.append(run)
        if checkpoint is not None:
            checkpoint(
                _benchmark_result(
                    normalized_sizes,
                    runs,
                    profile=profile,
                    sample_interval_seconds=sample_interval_seconds,
                    timeout_seconds=timeout_seconds,
                )
            )
    return _benchmark_result(
        normalized_sizes,
        runs,
        profile=profile,
        sample_interval_seconds=sample_interval_seconds,
        timeout_seconds=timeout_seconds,
    )


def _format_mib(value: int | None) -> str:
    if value is None:
        return "n/a"
    return f"{value / MIB:.1f}"


def _format_gib(value: int | None) -> str:
    if value is None:
        return "n/a"
    return f"{value / GIB:.3f}"


def render_markdown(result: dict) -> str:
    evaluation = result["evaluation"]
    workload = result["settings"].get("workload", {})
    workload_version = workload.get("profile_version", "unknown")
    workload_fingerprint = workload.get("seed_sha256", "unknown")
    lines = [
        "# CloudArc Large-Package Benchmark",
        "",
        f"**Generated:** {result['generated_at']}  ",
        f"**Run status:** `{result.get('completion_status', 'complete')}`  ",
        f"**Profile:** `{result['settings']['profile']}`  ",
        (
            "**Workload:** "
            f"v{workload_version} / "
            f"`{workload_fingerprint[:16]}`  "
        ),
        f"**Overall SLO:** {'PASS' if evaluation['pass'] else 'FAIL'}",
        "",
        "## Environment",
        "",
        f"- Platform: `{result['environment']['platform_detail']}`",
        f"- Machine: `{result['environment']['machine']}`",
        f"- Python: `{result['environment']['python']}`",
        (
            "- Sampling interval: "
            f"`{result['settings']['sample_interval_seconds']}` seconds"
        ),
        "",
        "## Bounded-Memory SLO",
        "",
        "| Requirement | Limit |",
        "|---|---:|",
        (
            "| Peak RSS per pack/unpack process | "
            f"{_format_mib(result['slo']['peak_rss_bytes_max'])} MiB |"
        ),
        (
            "| Peak RSS spread across tested sizes | "
            f"{_format_mib(result['slo']['peak_rss_spread_bytes_max'])} MiB |"
        ),
        (
            "| Pack staging disk | "
            f"{result['slo']['pack_temp_ratio_max']:.2f}x payload + "
            f"{_format_mib(result['slo']['temp_fixed_allowance_bytes'])} MiB |"
        ),
        (
            "| Unpack staging disk | "
            f"{result['slo']['unpack_temp_ratio_max']:.2f}x payload + "
            f"{_format_mib(result['slo']['temp_fixed_allowance_bytes'])} MiB |"
        ),
        "",
        "## Results",
        "",
        (
            "| Size GiB | Operation | Seconds | MiB/s | Peak RSS MiB | "
            "RSS delta MiB | Temp GiB | Temp/input | SLO |"
        ),
        "|---:|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for run in result["runs"]:
        if run.get("status") != "ok":
            status = run.get("status", "failed")
            lines.append(
                f"| {run.get('size_gib', 'n/a')} | {status} |  |  |  |  |  |  | "
                f"FAIL: {run.get('error', 'unknown error')} |"
            )
            continue
        for operation in ("pack", "unpack"):
            measured = run[operation]
            lines.append(
                "| "
                f"{run['size_gib']:.3f} | {operation} | "
                f"{measured['operation_seconds']:.3f} | "
                f"{measured['throughput_mib_per_second']:.1f} | "
                f"{_format_mib(measured['peak_rss_bytes'])} | "
                f"{_format_mib(measured['rss_delta_bytes'])} | "
                f"{_format_gib(measured['peak_temp_bytes'])} | "
                f"{measured['temp_to_input_ratio']:.3f} | "
                f"{'PASS' if measured['slo']['pass'] else 'FAIL'} |"
            )
    lines.extend(
        [
            "",
            "## Scaling Evaluation",
            "",
            (
                "- Requested/completed sizes: "
                f"{evaluation['requested_runs']}/{evaluation['completed_runs']} "
                f"({'PASS' if evaluation['all_requested_completed'] else 'FAIL'})."
            ),
            (
                "- Pack peak-RSS spread: "
                f"{_format_mib(evaluation['pack']['peak_rss_spread_bytes'])} MiB "
                f"({'PASS' if evaluation['pack']['peak_rss_spread_pass'] else 'FAIL'})."
            ),
            (
                "- Unpack peak-RSS spread: "
                f"{_format_mib(evaluation['unpack']['peak_rss_spread_bytes'])} MiB "
                f"({'PASS' if evaluation['unpack']['peak_rss_spread_pass'] else 'FAIL'})."
            ),
            "",
            "## Method",
            "",
            "- Each operation runs in a fresh child process.",
            (
                "- Peak RSS combines parent sampling with the worker's final "
                "OS high-water mark."
            ),
            (
                "- Temporary disk counts logical bytes only inside "
                "`.cloudarc-pack-*` or `.cloudarc-unpack-*` staging directories."
            ),
            (
                "- Sampling is combined with an operation-derived floor so a "
                "short atomic publication cannot be missed."
            ),
            "- Dataset generation and durable source/archive/output files are excluded.",
            (
                "- The SLO covers payload-size scaling with one file and bounded "
                "lexical vocabulary; manifest/index cardinality has a separate "
                "memory cost."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def render_multi_file_markdown(result: dict) -> str:
    evaluation = result.get("evaluation", {})
    return "\n".join(
        [
            "# CloudArc Multi-File Benchmark",
            "",
            f"**Status:** `{result.get('status')}`",
            f"**Files:** {result.get('file_count')}",
            f"**Raw bytes:** {result.get('raw_bytes')}",
            f"**Duplicate ratio:** {result.get('dedup_ratio')}",
            f"**Manifest entries:** {result.get('manifest_entry_count')}",
            f"**Index documents:** {result.get('index_document_count')}",
            f"**Index terms:** {result.get('index_term_count')}",
            f"**SLO:** {'PASS' if evaluation.get('pass') else 'FAIL'}",
            "",
            "Resource telemetry is recorded per pack/unpack child process.",
            "",
        ]
    )


def _atomic_write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file_obj:
            file_obj.write(payload)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _write_json(path: Path, value: dict) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    _atomic_write_text(path, payload)


def _write_outputs(json_path: Path, markdown_path: Path, result: dict) -> None:
    _write_json(json_path, result)
    _atomic_write_text(markdown_path, render_markdown(result))


def _worker(
    operation: str,
    source: Path,
    archive: Path,
    output: Path,
    *,
    dedup: bool = False,
) -> int:
    from core.packer import pack, unpack

    baseline = current_process_memory()
    started = time.perf_counter()
    if operation == "pack":
        result = pack([source], archive, dedup=dedup, apply=True)
        raw_bytes = result["raw_bytes"]
        data_bytes = result["header"]["data_length"]
        packed_bytes = result["packed_bytes"]
        sidecar_bytes = sum(
            Path(result[field]).stat().st_size
            for field in ("manifest", "index")
        )
        manifest_payload = json.loads(
            Path(result["manifest"]).read_text(encoding="utf-8")
        )
        index_payload = json.loads(
            Path(result["index"]).read_text(encoding="utf-8")
        )
        compact_result = {
            "raw_bytes": raw_bytes,
            "packed_bytes": packed_bytes,
            "data_bytes": data_bytes,
            "sidecar_bytes": sidecar_bytes,
            "methods": result["methods"],
            "entry_count": result["entry_count"],
            "dedup_entries": sum(
                1 for entry in manifest_payload.get("entries", [])
                if entry.get("dedup_of")
            ),
            "manifest_entry_count": result["entry_count"],
            "index_document_count": len(
                index_payload.get("lexical", {}).get("documents", {})
            ),
            "index_term_count": len(
                index_payload.get("lexical", {}).get("postings", {})
            ),
            "temp_peak_floor_bytes": max(
                raw_bytes + data_bytes,
                data_bytes + packed_bytes + sidecar_bytes,
            ),
        }
    elif operation == "unpack":
        result = unpack(archive, output, apply=True)
        restored_bytes = sum(
            item.stat().st_size
            for item in output.rglob("*")
            if item.is_file() and not item.is_symlink()
        )
        compact_result = {
            "restored": result["restored"],
            "restored_files": (
                result["restored"]
                if isinstance(result["restored"], int)
                else len(result["restored"])
            ),
            "temp_peak_floor_bytes": restored_bytes,
        }
    else:
        raise BenchmarkError(f"unsupported worker operation: {operation}")
    final_memory = current_process_memory()
    payload = {
        "status": "ok",
        "operation": operation,
        "baseline_rss_bytes": baseline.rss_bytes,
        "peak_rss_bytes": max(
            baseline.peak_rss_bytes,
            final_memory.peak_rss_bytes,
            final_memory.rss_bytes,
        ),
        "rss_measurement_source": final_memory.source,
        "operation_seconds": round(time.perf_counter() - started, 6),
        "result": compact_result,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def _parse_positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be finite and positive")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure CloudArc large-package RSS and staging disk",
    )
    subparsers = parser.add_subparsers(dest="command")
    worker = subparsers.add_parser("_worker", help=argparse.SUPPRESS)
    worker.add_argument("operation", choices=["pack", "unpack"])
    worker.add_argument("--source", type=Path, required=True)
    worker.add_argument("--archive", type=Path, required=True)
    worker.add_argument("--output", type=Path, required=True)
    worker.add_argument("--dedup", action="store_true")

    size_group = parser.add_mutually_exclusive_group()
    size_group.add_argument(
        "--sizes-gib",
        nargs="+",
        type=_parse_positive_float,
        default=None,
        help="payload sizes in GiB; default: 1 5 10",
    )
    size_group.add_argument(
        "--sizes-mib",
        nargs="+",
        type=_parse_positive_float,
        default=None,
        help="smoke-test payload sizes in MiB",
    )
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILES),
        default="compressible-text",
    )
    parser.add_argument(
        "--multi-file",
        action="store_true",
        help="run the bounded high-cardinality multi-file dedup benchmark",
    )
    parser.add_argument("--file-count", type=int, default=512)
    parser.add_argument("--file-size-kib", type=int, default=64)
    parser.add_argument("--duplicate-groups", type=int, default=16)
    parser.add_argument("--work-root", type=Path, default=None)
    parser.add_argument(
        "--sample-interval",
        type=_parse_positive_float,
        default=DEFAULT_SAMPLE_INTERVAL_SECONDS,
    )
    parser.add_argument(
        "--timeout",
        type=_parse_positive_float,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=Path("benchmarks/results/large-package.json"),
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=Path("docs/LARGE_PACKAGE_BENCHMARK.md"),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="continue an exact matching in-progress JSON checkpoint",
    )
    parser.add_argument("--fail-on-slo", action="store_true")
    return parser


def _load_resume_runs(
    path: Path,
    *,
    sizes: list[int],
    profile: str,
    sample_interval_seconds: float,
    timeout_seconds: float,
) -> list[dict]:
    try:
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BenchmarkError(f"resume checkpoint does not exist: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"resume checkpoint is unreadable: {path}") from exc
    if not isinstance(checkpoint, dict):
        raise BenchmarkError("resume checkpoint must be a JSON object")
    if (
        checkpoint.get("schema") != SCHEMA
        or checkpoint.get("schema_version") != SCHEMA_VERSION
    ):
        raise BenchmarkError("resume checkpoint schema does not match")
    if checkpoint.get("completion_status") != "in_progress":
        raise BenchmarkError("resume checkpoint is not in progress")
    if checkpoint.get("environment") != _environment_metadata():
        raise BenchmarkError("resume checkpoint environment does not match")
    settings = checkpoint.get("settings")
    if not isinstance(settings, dict):
        raise BenchmarkError("resume checkpoint settings are missing")
    expected_sizes = sorted({_validate_size(value) for value in sizes})
    if settings.get("sizes_bytes") != expected_sizes:
        raise BenchmarkError("resume checkpoint sizes do not match")
    if settings.get("profile") != profile:
        raise BenchmarkError("resume checkpoint profile does not match")
    if settings.get("sample_interval_seconds") != sample_interval_seconds:
        raise BenchmarkError("resume checkpoint sample interval does not match")
    if settings.get("operation_timeout_seconds") != timeout_seconds:
        raise BenchmarkError("resume checkpoint timeout does not match")
    if settings.get("workload") != _profile_metadata(profile):
        raise BenchmarkError("resume checkpoint workload fingerprint does not match")
    if checkpoint.get("slo") != SLO:
        raise BenchmarkError("resume checkpoint SLO does not match")
    runs = checkpoint.get("runs")
    if not isinstance(runs, list):
        raise BenchmarkError("resume checkpoint runs are missing")
    for run in runs:
        if not isinstance(run, dict):
            raise BenchmarkError("resume checkpoint run is invalid")
        size_bytes = run.get("size_bytes")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
            raise BenchmarkError("resume checkpoint run size is invalid")
        for operation in ("pack", "unpack"):
            measurement = run.get(operation)
            if not isinstance(measurement, dict):
                raise BenchmarkError(
                    f"resume checkpoint {operation} measurement is missing"
                )
            peak_rss = measurement.get("peak_rss_bytes")
            peak_temp = measurement.get("peak_temp_bytes")
            if (
                isinstance(peak_rss, bool)
                or not isinstance(peak_rss, int)
                or peak_rss <= 0
                or isinstance(peak_temp, bool)
                or not isinstance(peak_temp, int)
                or peak_temp <= 0
            ):
                raise BenchmarkError(
                    f"resume checkpoint {operation} measurement is invalid"
                )
            if measurement.get("slo") != _operation_slo(
                operation,
                measurement,
                size_bytes,
            ):
                raise BenchmarkError(
                    f"resume checkpoint {operation} SLO result is invalid"
                )
    return runs


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "_worker":
        try:
            return _worker(
                args.operation,
                args.source,
                args.archive,
                args.output,
                dedup=args.dedup,
            )
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2

    try:
        if args.multi_file:
            result = run_multi_file_benchmark(
                file_count=args.file_count,
                file_size_bytes=args.file_size_kib * 1024,
                duplicate_groups=args.duplicate_groups,
                work_root=args.work_root,
                sample_interval_seconds=args.sample_interval,
                timeout_seconds=args.timeout,
            )
            _write_json(args.json_output, result)
            _atomic_write_text(
                args.markdown_output,
                render_multi_file_markdown(result),
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["evaluation"]["pass"] or not args.fail_on_slo else 2
        if args.sizes_mib:
            sizes = [int(value * MIB) for value in args.sizes_mib]
        else:
            values = args.sizes_gib or list(DEFAULT_SIZES_GIB)
            sizes = [int(value * GIB) for value in values]
        initial_runs = (
            _load_resume_runs(
                args.json_output,
                sizes=sizes,
                profile=args.profile,
                sample_interval_seconds=float(args.sample_interval),
                timeout_seconds=float(args.timeout),
            )
            if args.resume
            else None
        )
        result = run_benchmark(
            sizes,
            profile=args.profile,
            work_root=args.work_root,
            sample_interval_seconds=args.sample_interval,
            timeout_seconds=args.timeout,
            checkpoint=lambda partial: _write_outputs(
                args.json_output,
                args.markdown_output,
                partial,
            ),
            initial_runs=initial_runs,
        )
        _write_outputs(args.json_output, args.markdown_output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if any(run.get("status") != "ok" for run in result["runs"]):
            return 2
        if args.fail_on_slo and not result["evaluation"]["pass"]:
            return 2
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
