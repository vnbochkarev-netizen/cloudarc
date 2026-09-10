"""Observability helpers for remote metadata/search operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic

from benchmarks.resource_monitor import ResourceMeasurementError, current_process_memory


@dataclass
class RemoteSearchTelemetry:
    """Bounded counters for metadata-only remote search."""

    started_at: float = field(default_factory=monotonic)
    range_requests: int = 0
    range_bytes: int = 0
    sidecar_reads: int = 0
    sidecar_bytes: int = 0
    peak_rss_bytes: int = 0
    rss_source: str = "unavailable"
    mode_used: str = "unknown"
    data_section_read: bool = False

    def sample_memory(self) -> None:
        try:
            sample = current_process_memory()
        except (ResourceMeasurementError, OSError):
            return
        self.peak_rss_bytes = max(self.peak_rss_bytes, sample.rss_bytes)
        self.rss_source = sample.source

    def record_range(self, payload: bytes | bytearray) -> None:
        self.range_requests += 1
        self.range_bytes += len(payload)
        self.sample_memory()

    def record_sidecar(self, payload: bytes | bytearray) -> None:
        self.sidecar_reads += 1
        self.sidecar_bytes += len(payload)
        self.sample_memory()

    def as_dict(self) -> dict:
        self.sample_memory()
        return {
            "range_requests": self.range_requests,
            "range_bytes": self.range_bytes,
            "sidecar_reads": self.sidecar_reads,
            "sidecar_bytes": self.sidecar_bytes,
            "peak_rss_bytes": self.peak_rss_bytes,
            "rss_source": self.rss_source,
            "mode_used": self.mode_used,
            "data_section_read": self.data_section_read,
        }
