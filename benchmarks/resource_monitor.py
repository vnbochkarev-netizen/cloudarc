"""Cross-platform process RSS and temporary-disk measurement helpers."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class ResourceMeasurementError(RuntimeError):
    """Raised when the platform cannot provide a reliable measurement."""


@dataclass(frozen=True)
class MemorySample:
    rss_bytes: int
    peak_rss_bytes: int
    source: str


class ProcessMemoryReader:
    """Read current and peak RSS for one process without third-party packages."""

    def __init__(self, pid: int):
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError("pid must be a positive integer")
        self.pid = pid
        self._handle = None
        self._source = ""
        if sys.platform == "win32":
            self._open_windows_handle()
        elif sys.platform.startswith("linux"):
            self._source = "linux-proc-status"
        elif sys.platform == "darwin":
            self._source = "macos-ps-rss"
        else:
            raise ResourceMeasurementError(
                f"RSS measurement is unsupported on {sys.platform}"
            )

    def _open_windows_handle(self) -> None:
        from ctypes import wintypes

        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._psapi = ctypes.WinDLL("psapi", use_last_error=True)
        self._kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

        process_query_information = 0x0400
        process_vm_read = 0x0010
        handle = self._kernel32.OpenProcess(
            process_query_information | process_vm_read,
            False,
            self.pid,
        )
        if not handle:
            error = ctypes.get_last_error()
            raise ResourceMeasurementError(
                f"cannot open process {self.pid} for RSS measurement: {error}"
            )
        self._handle = handle
        self._source = "windows-psapi-working-set"

    def _sample_windows(self) -> MemorySample:
        from ctypes import wintypes

        class ProcessMemoryCountersEx(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCountersEx()
        counters.cb = ctypes.sizeof(counters)
        self._psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCountersEx),
            wintypes.DWORD,
        ]
        self._psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        ok = self._psapi.GetProcessMemoryInfo(
            self._handle,
            ctypes.byref(counters),
            counters.cb,
        )
        if not ok:
            error = ctypes.get_last_error()
            raise ResourceMeasurementError(
                f"cannot read process {self.pid} RSS: {error}"
            )
        return MemorySample(
            rss_bytes=int(counters.WorkingSetSize),
            peak_rss_bytes=int(counters.PeakWorkingSetSize),
            source=self._source,
        )

    def _sample_linux(self) -> MemorySample:
        status_path = Path(f"/proc/{self.pid}/status")
        try:
            lines = status_path.read_text(encoding="ascii").splitlines()
        except OSError as exc:
            raise ResourceMeasurementError(
                f"cannot read {status_path}"
            ) from exc
        values: dict[str, int] = {}
        for line in lines:
            if ":" not in line:
                continue
            name, raw = line.split(":", 1)
            fields = raw.strip().split()
            if not fields:
                continue
            if name in {"VmRSS", "VmHWM"}:
                try:
                    values[name] = int(fields[0]) * 1024
                except ValueError as exc:
                    raise ResourceMeasurementError(
                        f"invalid {name} value for process {self.pid}"
                    ) from exc
        rss = values.get("VmRSS")
        if rss is None:
            raise ResourceMeasurementError(
                f"process {self.pid} has no VmRSS measurement"
            )
        return MemorySample(
            rss_bytes=rss,
            peak_rss_bytes=values.get("VmHWM", rss),
            source=self._source,
        )

    def _sample_macos(self) -> MemorySample:
        completed = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(self.pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            raise ResourceMeasurementError(
                f"cannot read process {self.pid} RSS with ps"
            )
        try:
            rss = int(completed.stdout.strip()) * 1024
        except ValueError as exc:
            raise ResourceMeasurementError(
                f"invalid ps RSS for process {self.pid}"
            ) from exc
        # macOS ps exposes the current resident set. The benchmark parent
        # samples it frequently and retains the maximum.
        return MemorySample(
            rss_bytes=rss,
            peak_rss_bytes=rss,
            source=self._source,
        )

    def sample(self) -> MemorySample:
        if sys.platform == "win32":
            return self._sample_windows()
        if sys.platform.startswith("linux"):
            return self._sample_linux()
        return self._sample_macos()

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> "ProcessMemoryReader":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


def current_process_memory() -> MemorySample:
    with ProcessMemoryReader(os.getpid()) as reader:
        return reader.sample()


def logical_tree_size(path: str | Path) -> int:
    """Return logical bytes below path while ignoring symlinks and races."""

    root = Path(path)
    try:
        if root.is_symlink() or not root.exists():
            return 0
        if root.is_file():
            return root.stat().st_size
    except OSError:
        return 0

    total = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except (FileNotFoundError, NotADirectoryError, OSError):
                        continue
        except (FileNotFoundError, NotADirectoryError, OSError):
            continue
    return total


def matching_directory_size(
    root: str | Path,
    prefixes: tuple[str, ...],
) -> int:
    """Sum logical bytes in immediate child directories with known prefixes."""

    root_path = Path(root)
    if not prefixes:
        return 0
    try:
        children = list(root_path.iterdir())
    except OSError:
        return 0
    total = 0
    for child in children:
        try:
            if (
                not child.is_symlink()
                and child.is_dir()
                and child.name.startswith(prefixes)
            ):
                total += logical_tree_size(child)
        except OSError:
            continue
    return total
