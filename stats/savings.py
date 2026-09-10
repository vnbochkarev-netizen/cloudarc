"""Redacted CloudArc savings and action logs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


SAFE_SAVINGS_FIELDS = {
    "schema_version",
    "timestamp",
    "operation",
    "archive",
    "disk",
    "project",
    "raw_bytes",
    "packed_bytes",
    "saved_bytes",
    "saved_pct",
    "methods",
    "remote_path",
}

SAFE_ACTION_FIELDS = {
    "schema_version",
    "timestamp",
    "operation",
    "disk",
    "project",
    "remote_path",
    "status",
    "detail",
}


def _append(path: Path, record: dict, allowed: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sanitized = {
        key: value
        for key, value in record.items()
        if key in allowed and value is not None
    }
    sanitized.setdefault("schema_version", 1)
    sanitized.setdefault(
        "timestamp", datetime.now(timezone.utc).isoformat()
    )
    with path.open("a", encoding="utf-8") as file_obj:
        file_obj.write(json.dumps(sanitized, ensure_ascii=False) + "\n")


def append_savings(stats_root: str | Path, record: dict) -> None:
    _append(Path(stats_root) / "savings.jsonl", record, SAFE_SAVINGS_FIELDS)


def append_action(stats_root: str | Path, record: dict) -> None:
    _append(Path(stats_root) / "action-log.jsonl", record, SAFE_ACTION_FIELDS)


def read_records(path: str | Path) -> list[dict]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    records: list[dict] = []
    for line in file_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def summarize(stats_root: str | Path) -> dict:
    records = read_records(Path(stats_root) / "savings.jsonl")
    raw = sum(int(item.get("raw_bytes", 0)) for item in records)
    packed = sum(int(item.get("packed_bytes", 0)) for item in records)
    saved = raw - packed
    return {
        "operations": len(records),
        "raw_bytes": raw,
        "packed_bytes": packed,
        "saved_bytes": saved,
        "saved_pct": round((saved / raw * 100) if raw else 0, 2),
    }
