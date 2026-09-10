#!/usr/bin/env python3
"""Run CloudArc benchmark profiles and validate remote-search telemetry."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


DEFAULT_REPO = Path.cwd()


class HelperError(RuntimeError):
    """Raised when the helper cannot produce a trustworthy result."""


def _repo_path(value: str | None) -> Path:
    path = Path(value or DEFAULT_REPO).expanduser().resolve()
    if not path.is_dir():
        raise HelperError(f"repository directory does not exist: {path}")
    return path


def _python_executable(value: str | None) -> str:
    executable = value or sys.executable
    if not executable:
        raise HelperError("python executable is not available")
    return executable


def _run(command: list[str], *, cwd: Path) -> int:
    print("+ " + " ".join(_display_arg(item) for item in command))
    completed = subprocess.run(command, cwd=cwd, check=False)
    return completed.returncode


def _load_result(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HelperError(f"benchmark JSON artifact is missing: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HelperError(f"benchmark JSON artifact is invalid: {path}") from exc
    if not isinstance(payload, dict):
        raise HelperError(f"benchmark JSON artifact must be an object: {path}")
    return payload


def _validate_markdown(path: Path, *, heading: str) -> None:
    try:
        payload = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise HelperError(f"benchmark Markdown artifact is unreadable: {path}") from exc
    if not payload.startswith(heading):
        raise HelperError(f"benchmark Markdown heading is invalid: {path}")
    if "PASS" not in payload:
        raise HelperError(f"benchmark Markdown does not report PASS: {path}")


def validate_benchmark_artifacts(
    json_path: Path,
    markdown_path: Path,
    *,
    profile: str,
) -> dict:
    result = _load_result(json_path)
    evaluation = result.get("evaluation")
    if not isinstance(evaluation, dict) or evaluation.get("pass") is not True:
        raise HelperError(f"benchmark artifact does not pass its SLO: {json_path}")
    if profile == "multi-file-dedup":
        if result.get("status") != "ok" or result.get("profile") != profile:
            raise HelperError(f"multi-file benchmark identity is invalid: {json_path}")
        file_count = result.get("file_count")
        if (
            isinstance(file_count, bool)
            or not isinstance(file_count, int)
            or file_count <= 0
            or result.get("manifest_entry_count") != file_count
            or result.get("index_document_count") != file_count
        ):
            raise HelperError(
                f"multi-file benchmark cardinality is inconsistent: {json_path}"
            )
        heading = "# CloudArc Multi-File Benchmark"
    else:
        if (
            result.get("schema") != "cloudarc.large-package-benchmark"
            or result.get("completion_status") != "complete"
        ):
            raise HelperError(f"payload benchmark identity is invalid: {json_path}")
        runs = result.get("runs")
        if not isinstance(runs, list) or not runs or any(
            not isinstance(run, dict) or run.get("status") != "ok" for run in runs
        ):
            raise HelperError(f"payload benchmark runs are incomplete: {json_path}")
        heading = "# CloudArc Large-Package Benchmark"
    _validate_markdown(markdown_path, heading=heading)
    return {
        "profile": profile,
        "json": str(json_path),
        "markdown": str(markdown_path),
        "pass": True,
    }


def _display_arg(value: str) -> str:
    if any(char.isspace() for char in value):
        return repr(value)
    return value


def _benchmark_command(
    python: str,
    *,
    sizes: list[str] | None = None,
    sizes_mib: list[str] | None = None,
    multi_file: bool = False,
    file_count: int = 512,
    file_size_kib: int = 64,
    duplicate_groups: int = 16,
    json_output: Path,
    markdown_output: Path,
) -> list[str]:
    command = [python, "-B", "-m", "benchmarks.large_package"]
    if multi_file:
        command.extend(
            [
                "--multi-file",
                "--file-count",
                str(file_count),
                "--file-size-kib",
                str(file_size_kib),
                "--duplicate-groups",
                str(duplicate_groups),
            ]
        )
    elif sizes_mib:
        command.extend(["--sizes-mib", *sizes_mib])
    else:
        command.extend(["--sizes-gib", *(sizes or ["1", "5", "10"])])
    command.extend(
        [
            "--json-output",
            str(json_output),
            "--markdown-output",
            str(markdown_output),
            "--fail-on-slo",
        ]
    )
    return command


def run_smoke(args: argparse.Namespace) -> int:
    repo = _repo_path(args.repo)
    python = _python_executable(args.python)
    commands = [
        [
            python,
            "-B",
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-v",
        ],
        _benchmark_command(
            python,
            sizes_mib=[str(args.size_mib)],
            json_output=repo / args.json_output,
            markdown_output=repo / args.markdown_output,
        ),
        _benchmark_command(
            python,
            multi_file=True,
            file_count=args.file_count,
            file_size_kib=args.file_size_kib,
            duplicate_groups=args.duplicate_groups,
            json_output=repo / args.multi_json_output,
            markdown_output=repo / args.multi_markdown_output,
        ),
    ]
    for command in commands:
        if _run(command, cwd=repo) != 0:
            return 2
    artifacts = [
        validate_benchmark_artifacts(
            repo / args.json_output,
            repo / args.markdown_output,
            profile="compressible-text",
        ),
        validate_benchmark_artifacts(
            repo / args.multi_json_output,
            repo / args.multi_markdown_output,
            profile="multi-file-dedup",
        ),
    ]
    print(json.dumps({"artifacts": artifacts}, indent=2))
    return 0


def run_manual(args: argparse.Namespace) -> int:
    if not args.yes:
        raise HelperError(
            "manual 1/5/10 GiB execution requires --yes because it can "
            "consume hours and large temporary disk"
        )
    repo = _repo_path(args.repo)
    python = _python_executable(args.python)
    command = _benchmark_command(
        python,
        sizes=[str(value) for value in args.sizes_gib],
        json_output=repo / args.json_output,
        markdown_output=repo / args.markdown_output,
    )
    if _run(command, cwd=repo) != 0:
        return 2
    multi_command = _benchmark_command(
        python,
        multi_file=True,
        file_count=args.file_count,
        file_size_kib=args.file_size_kib,
        duplicate_groups=args.duplicate_groups,
        json_output=repo / args.multi_json_output,
        markdown_output=repo / args.multi_markdown_output,
    )
    if _run(multi_command, cwd=repo) != 0:
        return 2
    artifacts = [
        validate_benchmark_artifacts(
            repo / args.json_output,
            repo / args.markdown_output,
            profile="compressible-text",
        ),
        validate_benchmark_artifacts(
            repo / args.multi_json_output,
            repo / args.multi_markdown_output,
            profile="multi-file-dedup",
        ),
    ]
    print(json.dumps({"artifacts": artifacts}, indent=2))
    return 0


def _read_json(path: str) -> Any:
    payload = sys.stdin.read() if path == "-" else Path(path).read_text(
        encoding="utf-8"
    )
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HelperError(f"invalid JSON telemetry input: {path}") from exc


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HelperError(f"telemetry field {field} must be a non-negative integer")
    return value


def validate_telemetry(payload: Any, *, expected: str) -> dict:
    if not isinstance(payload, dict):
        raise HelperError("telemetry input must be a JSON object")
    telemetry = payload.get("telemetry", payload)
    if not isinstance(telemetry, dict):
        raise HelperError("telemetry object is missing")
    required = {
        "range_requests",
        "range_bytes",
        "sidecar_reads",
        "sidecar_bytes",
        "peak_rss_bytes",
        "rss_source",
        "data_section_read",
    }
    missing = sorted(required.difference(telemetry))
    if missing:
        raise HelperError("telemetry fields are missing: " + ", ".join(missing))
    values = {
        field: _nonnegative_int(telemetry[field], field)
        for field in (
            "range_requests",
            "range_bytes",
            "sidecar_reads",
            "sidecar_bytes",
            "peak_rss_bytes",
        )
    }
    if not isinstance(telemetry["rss_source"], str) or not telemetry["rss_source"]:
        raise HelperError("telemetry rss_source must be a non-empty string")
    if values["peak_rss_bytes"] <= 0:
        raise HelperError("telemetry peak_rss_bytes must be positive")
    if telemetry["data_section_read"] is not False:
        raise HelperError("data_section_read must be false")
    if values["range_requests"] == 0 and values["range_bytes"] != 0:
        raise HelperError("range_bytes must be zero when range_requests is zero")
    if values["range_requests"] > 0 and values["range_bytes"] <= 0:
        raise HelperError("range_bytes must be positive when ranges were read")
    if values["sidecar_reads"] == 0 and values["sidecar_bytes"] != 0:
        raise HelperError("sidecar_bytes must be zero when sidecar_reads is zero")
    if values["sidecar_reads"] > 0 and values["sidecar_bytes"] <= 0:
        raise HelperError("sidecar_bytes must be positive when sidecars were read")
    if expected == "range" and values["range_requests"] != 3:
        raise HelperError("embedded metadata must use exactly three range requests")
    if expected == "sidecar":
        if values["range_requests"] != 0 or values["sidecar_reads"] != 2:
            raise HelperError(
                "sidecar fallback must use zero ranges and two sidecar reads"
            )
    return {
        "valid": True,
        "expected": expected,
        **values,
        "rss_source": telemetry["rss_source"],
        "data_section_read": False,
    }


def check_telemetry(args: argparse.Namespace) -> int:
    for path in args.input:
        result = validate_telemetry(_read_json(path), expected=args.expected)
        print(json.dumps({"input": path, **result}, indent=2))
    return 0


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo",
        default=None,
        help="CloudArc repository root; defaults to the current directory",
    )
    parser.add_argument(
        "--python",
        default=None,
        help="Python executable; defaults to the current interpreter",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CloudArc benchmark and remote-search telemetry helper"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    smoke = subparsers.add_parser("smoke", help="run tests and small benchmarks")
    _add_common(smoke)
    smoke.add_argument("--size-mib", type=int, default=64)
    smoke.add_argument("--file-count", type=int, default=512)
    smoke.add_argument("--file-size-kib", type=int, default=16)
    smoke.add_argument("--duplicate-groups", type=int, default=32)
    smoke.add_argument(
        "--json-output",
        default="benchmarks/results/helper-smoke.json",
    )
    smoke.add_argument(
        "--markdown-output",
        default="docs/LARGE_PACKAGE_BENCHMARK_HELPER_SMOKE.md",
    )
    smoke.add_argument(
        "--multi-json-output",
        default="benchmarks/results/helper-multi-file.json",
    )
    smoke.add_argument(
        "--multi-markdown-output",
        default="docs/MULTI_FILE_BENCHMARK_HELPER.md",
    )
    smoke.set_defaults(handler=run_smoke)

    manual = subparsers.add_parser(
        "manual",
        help="run the explicit large benchmark and multi-file profile",
    )
    _add_common(manual)
    manual.add_argument("--yes", action="store_true")
    manual.add_argument("--sizes-gib", nargs="+", type=float, default=[1, 5, 10])
    manual.add_argument("--file-count", type=int, default=5000)
    manual.add_argument("--file-size-kib", type=int, default=64)
    manual.add_argument("--duplicate-groups", type=int, default=256)
    manual.add_argument(
        "--json-output",
        default="benchmarks/results/large-package-helper-manual.json",
    )
    manual.add_argument(
        "--markdown-output",
        default="docs/LARGE_PACKAGE_BENCHMARK_HELPER_MANUAL.md",
    )
    manual.add_argument(
        "--multi-json-output",
        default="benchmarks/results/multi-file-helper-manual.json",
    )
    manual.add_argument(
        "--multi-markdown-output",
        default="docs/MULTI_FILE_BENCHMARK_HELPER_MANUAL.md",
    )
    manual.set_defaults(handler=run_manual)

    telemetry = subparsers.add_parser(
        "check-telemetry",
        help="validate one or more remote-search JSON responses",
    )
    telemetry.add_argument(
        "--input",
        action="append",
        required=True,
        help="response JSON path, or '-' for stdin",
    )
    telemetry.add_argument(
        "--expected",
        choices=("any", "range", "sidecar"),
        default="any",
    )
    telemetry.set_defaults(handler=check_telemetry)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (HelperError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
