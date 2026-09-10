from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from benchmarks.large_package import (
    BenchmarkError,
    MIB,
    SLO,
    _load_resume_runs,
    evaluate_benchmark,
    generate_dataset,
    render_markdown,
    run_benchmark,
    run_case,
    run_multi_file_case,
)
from benchmarks.resource_monitor import (
    current_process_memory,
    logical_tree_size,
    matching_directory_size,
)


class ResourceMonitorTests(unittest.TestCase):
    def test_current_process_rss_is_available(self):
        sample = current_process_memory()
        self.assertGreater(sample.rss_bytes, 0)
        self.assertGreaterEqual(sample.peak_rss_bytes, sample.rss_bytes)
        self.assertTrue(sample.source)

    def test_logical_disk_measurement_is_scoped_to_matching_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            staging = root / ".cloudarc-pack-test"
            staging.mkdir()
            (staging / "payload.bin").write_bytes(b"x" * 4096)
            durable = root / "archive.vibo"
            durable.write_bytes(b"y" * 8192)
            self.assertEqual(logical_tree_size(staging), 4096)
            self.assertEqual(
                matching_directory_size(root, (".cloudarc-pack-",)),
                4096,
            )

    def test_symlink_is_not_counted_as_temporary_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target.bin"
            target.write_bytes(b"x" * 4096)
            staging = root / ".cloudarc-pack-test"
            staging.mkdir()
            link = staging / "link.bin"
            try:
                os.symlink(target, link)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable")
            self.assertEqual(logical_tree_size(staging), 0)


class LargePackageBenchmarkTests(unittest.TestCase):
    def test_dataset_generator_writes_exact_streamed_size(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "payload.txt"
            generate_dataset(
                target,
                3 * MIB + 17,
                profile="compressible-text",
                buffer_size=MIB,
            )
            self.assertEqual(target.stat().st_size, 3 * MIB + 17)
            with target.open("rb") as file_obj:
                self.assertEqual(
                    file_obj.read(8),
                    b"cloudarc",
                )

    def test_smoke_case_measures_pack_and_unpack_resources(self):
        with tempfile.TemporaryDirectory() as temp:
            result = run_case(
                32 * MIB,
                work_root=temp,
                sample_interval_seconds=0.01,
                timeout_seconds=120,
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["size_bytes"], 32 * MIB)
        for operation in ("pack", "unpack"):
            measurement = result[operation]
            self.assertGreater(measurement["peak_rss_bytes"], 0)
            self.assertGreaterEqual(
                measurement["worker_peak_rss_bytes"],
                measurement["baseline_rss_bytes"],
            )
            self.assertGreater(measurement["sample_count"], 0)
            self.assertGreater(measurement["peak_temp_bytes"], 0)
            self.assertTrue(measurement["rss_measurement_source"])
            self.assertTrue(measurement["slo"]["pass"])
        self.assertGreater(result["pack"]["worker"]["sidecar_bytes"], 0)
        self.assertGreaterEqual(
            result["pack"]["operation_temp_floor_bytes"],
            result["pack"]["worker"]["data_bytes"]
            + result["pack"]["worker"]["packed_bytes"]
            + result["pack"]["worker"]["sidecar_bytes"],
        )

    def test_multi_file_dedup_reports_cardinality_and_resources(self):
        with tempfile.TemporaryDirectory() as temp:
            result = run_multi_file_case(
                file_count=24,
                file_size_bytes=8 * 1024,
                duplicate_groups=4,
                work_root=temp,
                sample_interval_seconds=0.01,
                timeout_seconds=120,
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["file_count"], 24)
        self.assertGreater(result["manifest_entry_count"], 0)
        self.assertGreaterEqual(result["dedup_entries"], 1)
        self.assertGreater(result["pack"]["peak_rss_bytes"], 0)
        self.assertGreater(result["unpack"]["peak_rss_bytes"], 0)

    def test_evaluation_checks_absolute_and_scaling_limits(self):
        base = {
            "status": "ok",
            "size_bytes": MIB,
            "pack": {
                "peak_rss_bytes": 100 * MIB,
                "slo": {"pass": True},
            },
            "unpack": {
                "peak_rss_bytes": 90 * MIB,
                "slo": {"pass": True},
            },
        }
        second = {
            "status": "ok",
            "size_bytes": 2 * MIB,
            "pack": {
                "peak_rss_bytes": 110 * MIB,
                "slo": {"pass": True},
            },
            "unpack": {
                "peak_rss_bytes": 100 * MIB,
                "slo": {"pass": True},
            },
        }
        evaluation = evaluate_benchmark([base, second])
        self.assertTrue(evaluation["pass"])
        self.assertLessEqual(
            evaluation["pack"]["peak_rss_spread_bytes"],
            SLO["peak_rss_spread_bytes_max"],
        )

        second["pack"]["peak_rss_bytes"] = 200 * MIB
        evaluation = evaluate_benchmark([base, second])
        self.assertFalse(evaluation["pass"])
        self.assertFalse(evaluation["pack"]["peak_rss_spread_pass"])

    def test_evaluation_fails_when_a_requested_size_is_missing(self):
        run = {
            "status": "ok",
            "size_bytes": MIB,
            "pack": {
                "peak_rss_bytes": 32 * MIB,
                "slo": {"pass": True},
            },
            "unpack": {
                "peak_rss_bytes": 32 * MIB,
                "slo": {"pass": True},
            },
        }
        evaluation = evaluate_benchmark(
            [run],
            expected_sizes_bytes=[MIB, 2 * MIB],
        )
        self.assertFalse(evaluation["pass"])
        self.assertFalse(evaluation["all_requested_completed"])
        self.assertEqual(evaluation["missing_sizes_bytes"], [2 * MIB])

    def test_non_finite_sampling_settings_are_rejected(self):
        with self.assertRaises(BenchmarkError):
            run_benchmark(
                [MIB],
                sample_interval_seconds=float("nan"),
            )
        with self.assertRaises(BenchmarkError):
            run_benchmark(
                [MIB],
                timeout_seconds=float("inf"),
            )

    def test_checkpoint_preserves_each_completed_size(self):
        checkpoints = []
        with tempfile.TemporaryDirectory() as temp:
            result = run_benchmark(
                [MIB, 2 * MIB],
                work_root=temp,
                sample_interval_seconds=0.01,
                timeout_seconds=120,
                checkpoint=checkpoints.append,
            )
        self.assertEqual(len(checkpoints), 2)
        self.assertEqual(checkpoints[0]["completion_status"], "in_progress")
        self.assertEqual(checkpoints[0]["evaluation"]["completed_runs"], 1)
        self.assertEqual(checkpoints[1]["completion_status"], "complete")
        self.assertTrue(result["evaluation"]["pass"])

    def test_resume_runs_only_missing_exact_matching_size(self):
        checkpoints = []
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_benchmark(
                [MIB, 2 * MIB],
                work_root=root,
                sample_interval_seconds=0.01,
                timeout_seconds=120,
                checkpoint=checkpoints.append,
            )
            checkpoint_path = root / "checkpoint.json"
            checkpoint_path.write_text(
                json.dumps(checkpoints[0]),
                encoding="utf-8",
            )
            initial_runs = _load_resume_runs(
                checkpoint_path,
                sizes=[MIB, 2 * MIB],
                profile="compressible-text",
                sample_interval_seconds=0.01,
                timeout_seconds=120.0,
            )
            tampered = json.loads(json.dumps(checkpoints[0]))
            tampered["runs"][0]["pack"]["slo"]["pass"] = False
            checkpoint_path.write_text(
                json.dumps(tampered),
                encoding="utf-8",
            )
            with self.assertRaises(BenchmarkError):
                _load_resume_runs(
                    checkpoint_path,
                    sizes=[MIB, 2 * MIB],
                    profile="compressible-text",
                    sample_interval_seconds=0.01,
                    timeout_seconds=120.0,
                )
            resumed_checkpoints = []
            result = run_benchmark(
                [MIB, 2 * MIB],
                work_root=root,
                sample_interval_seconds=0.01,
                timeout_seconds=120,
                initial_runs=initial_runs,
                checkpoint=resumed_checkpoints.append,
            )
        self.assertEqual(len(initial_runs), 1)
        self.assertEqual(len(resumed_checkpoints), 1)
        self.assertEqual(result["completion_status"], "complete")
        self.assertEqual(result["evaluation"]["completed_runs"], 2)

    def test_markdown_report_contains_resource_columns(self):
        run = {
            "status": "ok",
            "size_bytes": MIB,
            "size_gib": 1 / 1024,
            "profile": "compressible-text",
            "pack": {
                "operation_seconds": 1.0,
                "throughput_mib_per_second": 1.0,
                "peak_rss_bytes": 32 * MIB,
                "rss_delta_bytes": 8 * MIB,
                "peak_temp_bytes": MIB,
                "temp_to_input_ratio": 1.0,
                "slo": {"pass": True},
            },
            "unpack": {
                "operation_seconds": 1.0,
                "throughput_mib_per_second": 1.0,
                "peak_rss_bytes": 32 * MIB,
                "rss_delta_bytes": 8 * MIB,
                "peak_temp_bytes": MIB,
                "temp_to_input_ratio": 1.0,
                "slo": {"pass": True},
            },
        }
        result = {
            "generated_at": "2026-09-08T00:00:00+00:00",
            "environment": {
                "platform_detail": "test",
                "machine": "test",
                "python": "3.14",
            },
            "settings": {
                "profile": "compressible-text",
                "sample_interval_seconds": 0.05,
                "workload": {
                    "profile_version": 2,
                    "seed_sha256": "0123456789abcdef" * 4,
                },
            },
            "slo": dict(SLO),
            "runs": [run],
        }
        result["evaluation"] = evaluate_benchmark(result["runs"])
        report = render_markdown(result)
        self.assertIn("Peak RSS MiB", report)
        self.assertIn("Temp/input", report)
        self.assertIn("Overall SLO", report)
