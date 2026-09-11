#!/usr/bin/env python3
"""Vibo CloudArc MVP command line interface."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from cloud.google import GoogleCloud
from cloud.local import LocalCloud
from cloud.protocol import (
    build_request,
    execute_sidecar_request,
    sidecar_remote_paths,
    validate_remote_archive_path,
)
from cloud.telemetry import RemoteSearchTelemetry
from cloud.yandex import YandexCloud
from config import load_config, resolve_from_config
from core.errors import (
    CloudArcError,
    CloudError,
    CloudNotConfigured,
    FormatError,
    SafetyError,
)
from core.format import (
    HEADER_SIZE,
    MAGIC,
    parse_header_bytes,
    parse_metadata_sections,
    read_index,
    read_manifest,
    sidecar_paths,
    write_sidecars,
)
from core.index import validate_index
from core.manifest import (
    validate_manifest,
    validate_manifest_index_alignment,
)
from core.native import probe_native
from core.packer import (
    analyze,
    info_archive,
    list_archive,
    pack,
    search_archive_result,
    unpack,
)
from core.safety import weekly_folder
from stats.savings import append_action, append_savings, summarize


VERSION = "0.1.0-mvp"


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def _dump(value, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _warn_skipped(result) -> None:
    """Tell the user, out loud, about files that did not enter the archive.

    Files skipped by the safety policy used to disappear silently: a 556-file tree
    produced a 356-file archive with no signal at all.
    """

    if not isinstance(result, dict):
        return
    summary = result.get("skipped_summary") or {}
    count = summary.get("count") or result.get("skipped_count") or 0
    if not count:
        return
    reasons = summary.get("by_reason") or result.get("skipped_by_reason") or {}
    detail = ", ".join(f"{name}: {value}" for name, value in sorted(reasons.items()))
    print(
        f"warning: {count} file(s) were not packed ({detail}); run with --json to list them",
        file=sys.stderr,
    )


def _stats_writer(config: dict, config_path: Path):
    stats_root = resolve_from_config(config_path, config["stats_root"])

    def write(record: dict) -> None:
        append_savings(stats_root, record)

    return write


def _cloud(disk: str, config: dict, config_path: Path):
    if disk == "local":
        root = resolve_from_config(config_path, config["cloud_root"])
        return LocalCloud(root)
    if disk == "yd":
        return YandexCloud()
    if disk == "gd":
        return GoogleCloud()
    raise CloudNotConfigured(f"unsupported disk: {disk}")


def _weekly_remote_folder(args, config: dict) -> str:
    base = weekly_folder(
        year=str(config["year"]),
        smartbot_root=str(config["smartbot_root"]),
        week=str(args.week or config["week"]),
        project=str(args.project or config["project"]),
    )
    supplied = getattr(args, "folder", None)
    if not supplied:
        return base
    normalized = supplied.replace("\\", "/").strip("/")
    if normalized != base and not normalized.startswith(base + "/"):
        raise SafetyError(
            "remote folder must stay inside the configured weekly project folder"
        )
    return normalized


def _remote_sidecars(remote_archive: str) -> tuple[str, str]:
    return sidecar_remote_paths(validate_remote_archive_path(remote_archive))


def _read_remote_sidecars(
    cloud,
    remote_archive: str,
    *,
    telemetry: RemoteSearchTelemetry | None = None,
):
    manifest_path, index_path = _remote_sidecars(remote_archive)
    try:
        manifest_payload = cloud.read_bytes(manifest_path)
        index_payload = cloud.read_bytes(index_path)
        if telemetry is not None:
            telemetry.record_sidecar(manifest_payload)
            telemetry.record_sidecar(index_payload)
        manifest = json.loads(manifest_payload.decode("utf-8"))
        index = json.loads(index_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FormatError("remote sidecar is not valid UTF-8 JSON") from exc
    validate_manifest(manifest)
    validate_index(index, expected_archive_id=manifest.get("archive_id"))
    validate_manifest_index_alignment(manifest, index)
    return manifest, index


def _read_remote_ranges(
    cloud,
    remote_archive: str,
    *,
    archive_meta=None,
    telemetry: RemoteSearchTelemetry | None = None,
):
    """Read only the fixed header, manifest and index from a remote archive."""

    remote_archive = validate_remote_archive_path(remote_archive)
    if archive_meta is None:
        archive_meta = cloud.get_meta(remote_archive)
    if archive_meta.file_type != "file":
        raise CloudError(f"remote archive is not a file: {remote_archive}")
    if (
        isinstance(archive_meta.size, bool)
        or not isinstance(archive_meta.size, int)
        or archive_meta.size < 0
    ):
        raise CloudError(f"remote archive has invalid size: {remote_archive}")

    read_range = getattr(cloud, "read_range", None)
    if not callable(read_range):
        raise NotImplementedError("provider does not support range reads")

    header_length = len(MAGIC) + HEADER_SIZE
    header_payload = read_range(remote_archive, 0, header_length)
    if telemetry is not None:
        telemetry.record_range(header_payload)
    if not isinstance(header_payload, (bytes, bytearray)) or len(
        header_payload
    ) != header_length:
        raise FormatError("remote header range has an unexpected length")
    header = parse_header_bytes(
        header_payload,
        file_size=archive_meta.size,
    )
    manifest_payload = read_range(
        remote_archive,
        header["manifest_offset"],
        header["manifest_length"],
    )
    if telemetry is not None:
        telemetry.record_range(manifest_payload)
    index_payload = read_range(
        remote_archive,
        header["index_offset"],
        header["index_length"],
    )
    if telemetry is not None:
        telemetry.record_range(index_payload)
    return parse_metadata_sections(
        header,
        manifest_payload,
        index_payload,
    )


def _read_remote_metadata(
    cloud,
    remote_archive: str,
    *,
    archive_meta=None,
    telemetry: RemoteSearchTelemetry | None = None,
):
    """Prefer range reads and fall back to validated sidecars if unavailable."""

    try:
        return _read_remote_ranges(
            cloud,
            remote_archive,
            archive_meta=archive_meta,
            telemetry=telemetry,
        )
    except NotImplementedError:
        return _read_remote_sidecars(
            cloud,
            remote_archive,
            telemetry=telemetry,
        )


def _validate_downloaded_archive(
    archive_path: Path,
    expected_manifest: dict,
    expected_index: dict,
) -> None:
    """Ensure the downloaded body is the same archive described by sidecars."""

    actual_manifest = read_manifest(archive_path)
    actual_index = read_index(archive_path)
    if actual_manifest != expected_manifest:
        raise FormatError(
            "downloaded archive manifest does not match the remote sidecar"
        )
    if actual_index != expected_index:
        raise FormatError(
            "downloaded archive index does not match the remote sidecar"
        )


def _remote_archive_in_weekly_folder(
    remote_archive: str,
    args,
    config: dict,
) -> str:
    normalized = validate_remote_archive_path(remote_archive)
    base = _weekly_remote_folder(args, config)
    if normalized != base and not normalized.startswith(base + "/"):
        raise SafetyError(
            "remote archive must stay inside the configured weekly project folder"
        )
    return normalized


def _record_action(
    config_path: Path,
    config: dict,
    record: dict,
) -> None:
    """Action logging must never turn a completed operation into a crash."""

    try:
        stats_root = resolve_from_config(config_path, config["stats_root"])
        append_action(stats_root, record)
    except Exception:
        pass


def _push(args, config: dict, config_path: Path) -> dict:
    source_input = Path(args.source).expanduser()
    if source_input.is_symlink():
        raise SafetyError(f"symlink source is not allowed: {source_input}")
    source = source_input.resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    folder = _weekly_remote_folder(args, config)
    archive_name = (
        source.name if source.suffix.lower() == ".vibo" else f"{source.name}.vibo"
    )
    remote_archive = f"{folder}/{archive_name}"
    cloud = _cloud(args.disk or config["default_disk"], config, config_path)

    effective_dry_run = args.dry_run or not args.apply
    if effective_dry_run:
        if source.suffix.lower() == ".vibo":
            # Validate an existing archive during preview as well; a dry-run
            # must not advertise an object that cannot be read.
            preview_manifest = read_manifest(source)
            preview_index = read_index(source)
            validate_manifest_index_alignment(
                preview_manifest,
                preview_index,
            )
            plan = {
                "source": str(source),
                "remote_archive": remote_archive,
                "disk": args.disk or config["default_disk"],
                "folder": folder,
                "mode": "upload-existing-vibo",
                "dry_run": True,
                "requires_apply": not args.apply,
            }
        else:
            plan = analyze([source])
            plan.update(
                {
                    "source": str(source),
                    "remote_archive": remote_archive,
                    "disk": args.disk or config["default_disk"],
                    "folder": folder,
                    "mode": "pack-then-upload",
                    "dry_run": True,
                    "requires_apply": not args.apply,
                }
            )
        return plan

    uploaded_paths: list[str] = []
    existing: dict[str, object | None] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="cloudarc-push-") as temp_dir:
            if source.suffix.lower() == ".vibo":
                # Never materialize derived sidecars next to a user-supplied
                # archive.  Staging avoids mutating the source on a failed
                # upload and prevents stale sidecars from being reused.
                archive_path = source
                manifest = read_manifest(archive_path)
                index = read_index(archive_path)
                staged_sidecar_base = Path(temp_dir) / archive_name
                local_manifest, local_index = write_sidecars(
                    staged_sidecar_base,
                    manifest,
                    index,
                )
            else:
                archive_path = Path(temp_dir) / archive_name
                packed = pack(
                    [source],
                    archive_path,
                    dedup=args.dedup,
                    apply=True,
                    index=not getattr(args, "no_index", False),
                    stats_writer=_stats_writer(config, config_path),
                    stats_context={
                        "disk": args.disk or config["default_disk"],
                        "project": args.project or config["project"],
                    },
                    max_package_bytes=config.get("max_package_bytes"),
                )
                _warn_skipped(packed)
                local_manifest, local_index = sidecar_paths(archive_path)

            remote_paths = [
                remote_archive,
                f"{remote_archive}.manifest.json",
                f"{remote_archive}.index.json",
            ]
            for remote_path in remote_paths:
                try:
                    existing[remote_path] = cloud.get_meta(remote_path)
                except CloudError:
                    existing[remote_path] = None

            target_folder = cloud.ensure_folder(folder, apply=True)
            for local_path, remote_path in (
                (archive_path, remote_archive),
                (local_manifest, remote_paths[1]),
                (local_index, remote_paths[2]),
            ):
                cloud.upload(local_path, remote_path, apply=True)
                uploaded_paths.append(remote_path)

            # A push is not considered applied until all three objects are
            # visible and their sidecars validate as one archive.
            for remote_path in remote_paths:
                cloud.get_meta(remote_path)
            _read_remote_sidecars(cloud, remote_archive)
    except Exception as exc:
        for remote_path in uploaded_paths:
            if existing.get(remote_path) is None:
                try:
                    cloud.delete(remote_path, apply=True)
                except Exception:
                    pass
        _record_action(
            config_path,
            config,
            {
                "operation": "push",
                "disk": args.disk or config["default_disk"],
                "project": args.project or config["project"],
                "remote_path": remote_archive,
                "status": "failed",
                "detail": f"{type(exc).__name__}: push not committed",
            },
        )
        raise

    _record_action(
        config_path,
        config,
        {
            "operation": "push",
            "disk": args.disk or config["default_disk"],
            "project": args.project or config["project"],
            "remote_path": remote_archive,
            "status": "applied",
            "detail": "archive and sidecars uploaded and verified",
        },
    )
    return {
        "source": str(source),
        "remote_archive": remote_archive,
        "disk": args.disk or config["default_disk"],
        "folder": target_folder,
        "status": "applied",
    }


def _pull(args, config: dict, config_path: Path) -> dict:
    cloud = _cloud(args.disk or config["default_disk"], config, config_path)
    remote_archive = _remote_archive_in_weekly_folder(
        args.remote,
        args,
        config,
    )
    args.remote = remote_archive
    output = Path(args.output).expanduser().resolve()
    effective_dry_run = args.dry_run or not args.apply
    if effective_dry_run:
        return {
            "dry_run": True,
            "remote_archive": remote_archive,
            "disk": args.disk or config["default_disk"],
            "output": str(output),
            "mode": "download-then-unpack",
            "requires_apply": not args.apply,
        }

    try:
        # Validate metadata before changing the local output directory.
        remote_manifest, remote_index = _read_remote_sidecars(
            cloud,
            remote_archive,
        )
        with tempfile.TemporaryDirectory(prefix="cloudarc-pull-") as temp_dir:
            local_archive = Path(temp_dir) / Path(remote_archive).name
            cloud.download(remote_archive, local_archive, apply=True)
            _validate_downloaded_archive(
                local_archive,
                remote_manifest,
                remote_index,
            )
            result = unpack(local_archive, output, apply=True)
    except Exception as exc:
        _record_action(
            config_path,
            config,
            {
                "operation": "pull",
                "disk": args.disk or config["default_disk"],
                "project": args.project or config["project"],
                "remote_path": remote_archive,
                "status": "failed",
                "detail": f"{type(exc).__name__}: pull not committed",
            },
        )
        raise

    _record_action(
        config_path,
        config,
        {
            "operation": "pull",
            "disk": args.disk or config["default_disk"],
            "project": args.project or config["project"],
            "remote_path": remote_archive,
            "status": "applied",
            "detail": f"restored to {output.name}",
        },
    )
    return result


def _open_remote(args, config: dict, config_path: Path) -> dict:
    remote_archive = validate_remote_archive_path(args.remote)
    request = build_request(
        args.operation,
        remote_archive,
        query=args.query,
        limit=args.limit,
        mode=getattr(args, "mode", "lexical"),
    )
    cloud = _cloud(args.disk or config["default_disk"], config, config_path)
    archive_meta = cloud.get_meta(remote_archive)
    if archive_meta.file_type != "file":
        raise CloudError(f"remote archive is not a file: {remote_archive}")
    telemetry = RemoteSearchTelemetry()
    telemetry.sample_memory()
    manifest, index = _read_remote_metadata(
        cloud,
        remote_archive,
        archive_meta=archive_meta,
        telemetry=telemetry,
    )
    response = execute_sidecar_request(request, manifest, index)
    telemetry.mode_used = response.get("mode_used", "metadata")
    response["telemetry"] = telemetry.as_dict()
    if args.operation == "info":
        try:
            response["packed_bytes"] = cloud.get_meta(remote_archive).size
        except Exception:
            response["packed_bytes"] = None
    return response


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cloudarc.py",
        description="Vibo CloudArc MVP",
    )
    parser.add_argument("--config", default=None, help="path to config.json")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("analyze", help="dry-run compression analysis")
    p.add_argument("inputs", nargs="+")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("pack", help="pack files into a .vibo archive")
    p.add_argument("inputs", nargs="+")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--dedup", action="store_true")
    p.add_argument(
        "--no-index",
        action="store_true",
        help="skip the lexical search index (much lower memory on large text trees; search returns nothing)",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", "--yes", dest="apply", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("unpack", help="restore a .vibo archive")
    p.add_argument("archive")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", "--yes", dest="apply", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("info", help="show local archive metadata")
    p.add_argument("archive")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("list", help="list local archive entries")
    p.add_argument("archive")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("search", help="search a local archive")
    p.add_argument("archive")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--mode", choices=["lexical", "semantic"], default="lexical")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("push", help="pack if needed and upload archive")
    p.add_argument("source")
    p.add_argument("--disk", choices=["local", "yd", "gd"])
    p.add_argument("--folder")
    p.add_argument("--week")
    p.add_argument("--project")
    p.add_argument("--dedup", action="store_true")
    p.add_argument(
        "--no-index",
        action="store_true",
        help="skip the lexical search index (much lower memory on large text trees)",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", "--yes", dest="apply", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("pull", help="download an archive and unpack it")
    p.add_argument("remote")
    p.add_argument("--disk", choices=["local", "yd", "gd"])
    p.add_argument("--week")
    p.add_argument("--project")
    p.add_argument("-o", "--out", dest="output", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", "--yes", dest="apply", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("ls-cloud", help="list a cloud folder")
    p.add_argument("--disk", choices=["local", "yd", "gd"])
    p.add_argument("--folder")
    p.add_argument("--week")
    p.add_argument("--project")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("open", help="read a remote archive via sidecars")
    p.add_argument("remote")
    p.add_argument("operation", choices=["list", "info", "search"])
    p.add_argument("query", nargs="?")
    p.add_argument("--disk", choices=["local", "yd", "gd"])
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--mode", choices=["lexical", "semantic"], default="lexical")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("fetch", help="remote search via the index sidecar")
    p.add_argument("remote")
    p.add_argument("query")
    p.add_argument("--disk", choices=["local", "yd", "gd"])
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--mode", choices=["lexical", "semantic"], default="lexical")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("stats", help="summarize CloudArc byte savings")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("runtime", help="probe optional native ViBo runtime")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("version", help="show CloudArc version")
    p.add_argument("--json", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config, config_path = load_config(args.config)
        command = args.command

        if command == "analyze":
            result = analyze(args.inputs)
        elif command == "pack":
            effective_dry_run = args.dry_run or not args.apply
            result = pack(
                args.inputs,
                args.output,
                dedup=args.dedup,
                dry_run=effective_dry_run,
                apply=args.apply,
                index=not args.no_index,
                stats_writer=_stats_writer(config, config_path),
                stats_context={
                    "disk": config["default_disk"],
                    "project": config["project"],
                },
                max_package_bytes=config.get("max_package_bytes"),
            )
            _warn_skipped(result)
        elif command == "unpack":
            effective_dry_run = args.dry_run or not args.apply
            result = unpack(
                args.archive,
                args.out,
                dry_run=effective_dry_run,
                apply=args.apply,
            )
        elif command == "info":
            result = info_archive(args.archive)
        elif command == "list":
            result = list_archive(args.archive)
        elif command == "search":
            native = config.get("native", {})
            result = search_archive_result(
                args.archive,
                args.query,
                limit=args.limit,
                mode=args.mode,
                native_module_path=native.get("module_path") or None,
                native_module_name=native.get("module_name", "vibo_archive"),
            )
        elif command == "push":
            result = _push(args, config, config_path)
        elif command == "pull":
            result = _pull(args, config, config_path)
        elif command == "ls-cloud":
            cloud = _cloud(args.disk or config["default_disk"], config, config_path)
            folder = _weekly_remote_folder(args, config)
            result = [
                item.__dict__ for item in cloud.list_folder(folder)
            ]
        elif command == "open":
            if args.operation == "search" and not args.query:
                raise SafetyError("open search requires a query")
            result = _open_remote(args, config, config_path)
        elif command == "fetch":
            args.operation = "search"
            args.query = args.query
            result = _open_remote(args, config, config_path)
        elif command == "stats":
            stats_root = resolve_from_config(config_path, config["stats_root"])
            result = summarize(stats_root)
        elif command == "runtime":
            native = config.get("native", {})
            result = probe_native(
                native.get("module_path") or None,
                module_name=native.get("module_name", "vibo_archive"),
            )
        elif command == "version":
            result = {
                "cloudarc_version": VERSION,
                "format_version": 1,
                "manifest_schema_version": 2,
                "index_schema_version": 2,
                "remote_protocol_version": "1.1",
            }
        else:
            parser.error(f"unsupported command: {command}")
            return 2

        _dump(result, getattr(args, "json", False))
        return 0
    except (CloudArcError, FileNotFoundError, PermissionError, ValueError) as exc:
        payload = {"error": type(exc).__name__, "message": str(exc)}
        if getattr(args, "json", False):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"CloudArc error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
