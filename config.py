"""CloudArc configuration loader."""

from __future__ import annotations

import copy
import json
from pathlib import Path


DEFAULT_CONFIG = {
    "version": 1,
    "default_disk": "local",
    "year": "2026",
    "smartbot_root": "SMARTBOT",
    "week": "01",
    "project": "CloudArc",
    "cloud_root": ".cloudarc/cloud",
    "max_package_bytes": 2 * 1024 * 1024 * 1024,
    "native": {
        "mode": "auto",
        "module_path": "",
        "module_name": "vibo_archive",
    },
    "stats_root": "stats",
}


def _merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: str | Path | None = None) -> tuple[dict, Path]:
    config_path = Path(path or "config.json").expanduser().resolve()
    if not config_path.exists():
        return copy.deepcopy(DEFAULT_CONFIG), config_path
    with config_path.open("r", encoding="utf-8") as file_obj:
        loaded = json.load(file_obj)
    if not isinstance(loaded, dict):
        raise ValueError("config.json must contain an object")
    config = _merge(DEFAULT_CONFIG, loaded)
    return config, config_path


def resolve_from_config(config_path: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (config_path.parent / path).resolve()
