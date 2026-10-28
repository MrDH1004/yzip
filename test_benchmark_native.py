import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

import benchmark_native as benchmark


class BenchmarkNativeTests(unittest.TestCase):
    def _case(self, **overrides):
        values = dict(
            dataset="large-text", tool="bandizip", format="zip", operation="compress",
            threads=4, level=5, source=Path("source"), archive=Path("archive.zip"),
        )
        values.update(overrides)
        return benchmark.BenchmarkCase(**values)

    def test_expand_cases_is_deterministic(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            datasets = {
                name: root / name
                for name in ("large-text", "incompressible", "small-files", "mixed-project", "large-file")
            }
            cases = benchmark.expand_cases(datasets, root / "archives")

        self.assertEqual(
            [(case.dataset, case.tool, case.format, case.operation, case.threads) for case in cases],
            [
                (dataset, tool, archive_format, operation, threads)
                for dataset in datasets
                for tool in ("bandizip", "yzip")
                for archive_format in ("zip", "7z")
                for operation in ("compress", "extract")
                for threads in (1, 4, 8, 16)
            ],
        )
        self.assertEqual(len(cases), 160)
        self.assertTrue(all(case.level == 5 for case in cases))

    def test_make_datasets_creates_all_required_independent_classes(self):
        with tempfile.TemporaryDirectory() as temp:
            datasets = benchmark.make_datasets(Path(temp), project_source=benchmark.ROOT)

            self.assertEqual(
                list(datasets),
                ["large-text", "incompressible", "small-files", "mixed-project", "large-file"],
            )
            self.assertGreaterEqual(sum(1 for path in datasets["small-files"].rglob("*") if path.is_file()), 1500)
            incompressible = (datasets["incompressible"] / "deterministic.bin").read_bytes()
            self.assertGreater(len(zlib.compress(incompressible, level=9)) / len(incompressible), 0.99)
            self.assertTrue((datasets["mixed-project"] / "yzip_cli.py").is_file())
            self.assertEqual(sum(path.stat().st_size for path in datasets["large-file"].rglob("*") if path.is_file()),
                             benchmark.LARGE_FILE_BYTES)

    def test_run_case_excludes_warmup_and_calculates_median_and_p95(self):
        case = self._case()
        samples = [
            benchmark.ProcessSample(99.0, 99.0, 99, 0, tree_sha256="a" * 64),
            benchmark.ProcessSample(1.0, 10.0, 100, 0, tree_sha256="a" * 64),
            benchmark.ProcessSample(2.0, 20.0, 200, 0, tree_sha256="a" * 64),
            benchmark.ProcessSample(3.0, 30.0, 300, 0, tree_sha256="a" * 64),
            benchmark.ProcessSample(4.0, 40.0, 400, 0, tree_sha256="a" * 64),
            benchmark.ProcessSample(100.0, 50.0, 500, 0, tree_sha256="a" * 64),
        ]
        with mock.patch.object(benchmark, "_run_iteration", side_effect=samples) as run:
            with mock.patch.object(benchmark, "tree_sha256", return_value="a" * 64):
                result = benchmark.run_case(case, runs=5)

        self.assertEqual(run.call_count, 6)
        self.assertEqual(result.wall_median_seconds, 3.0)
        self.assertEqual(result.wall_p95_seconds, 100.0)
        self.assertEqual(result.cpu_median_seconds, 30.0)
        self.assertEqual(result.peak_rss_bytes, 500)

    def test_commands_propagate_requested_threads(self):
        bandizip = benchmark.build_command(self._case(tool="bandizip", format="7z", threads=8), Path("out"))
        yzip = benchmark.build_command(self._case(tool="yzip", format="7z", threads=16), Path("out"))

        self.assertIn("-t:8", bandizip)
        self.assertIn("runtimeconfig.configure(16", yzip[2])

    def test_run_case_fails_when_extracted_tree_hash_mismatches_source(self):
        case = self._case(tool="yzip", operation="extract", threads=1)
        with mock.patch.object(
            benchmark, "_run_iteration",
            return_value=benchmark.ProcessSample(1.0, 1.0, 1, 0, tree_sha256="different-hash"),
        ):
            with mock.patch.object(benchmark, "tree_sha256", return_value="source-hash"):
                with self.assertRaisesRegex(RuntimeError, "SHA-256 tree mismatch"):
                    benchmark.run_case(case, runs=1)

    def test_compression_cross_extracts_with_opposite_tool_and_rejects_corrupt_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            (source / "payload.txt").write_text("expected", encoding="utf-8")
            case = self._case(tool="yzip", source=source)
            archive = root / "result.zip"
            archive.write_bytes(b"not-a-real-archive")

            def fake_extract(command):
                self.assertEqual(command[0], str(benchmark.BZ_EXE))
                extracted = root / "cross-extracted" / source.name
                extracted.mkdir(parents=True)
                (extracted / "payload.txt").write_text("corrupt", encoding="utf-8")

            with mock.patch.object(benchmark, "_run_unmeasured", side_effect=fake_extract):
                with self.assertRaisesRegex(RuntimeError, "cross-extraction SHA-256 mismatch"):
                    benchmark.verify_generated_archive(case, archive, root / "cross-extracted")

    def test_bandizip_compression_cross_extracts_with_yzip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            (source / "payload.txt").write_text("expected", encoding="utf-8")
            destination = root / "cross-extracted"
            case = self._case(tool="bandizip", source=source)

            def fake_extract(command):
                self.assertEqual(command[0], benchmark.sys.executable)
                extracted = destination / source.name
                extracted.mkdir(parents=True)
                (extracted / "payload.txt").write_text("expected", encoding="utf-8")

            with mock.patch.object(benchmark, "_run_unmeasured", side_effect=fake_extract):
                self.assertEqual(
                    benchmark.verify_generated_archive(case, root / "result.zip", destination),
                    benchmark.tree_sha256(source),
                )

    def test_json_includes_dataset_dimension(self):
        result = benchmark.BenchmarkResult(
            case=self._case(dataset="small-files"), measured_runs=7,
            wall_median_seconds=1.0, wall_p95_seconds=2.0, cpu_median_seconds=0.5,
            peak_rss_bytes=10, archive_size_bytes=20, requested_threads=4,
            command=("tool",), sha256_tree="hash",
        )
        self.assertEqual(result.to_json()["case"]["dataset"], "small-files")

    def test_7z_eight_thread_regression_gate_accepts_exactly_five_percent(self):
        current = {"large-text": 10.5}
        regression = benchmark.enforce_7z_regression_gate(current, {"large-text": 10.0})
        self.assertAlmostEqual(regression["large-text"], 0.05)

    def test_7z_eight_thread_regression_gate_rejects_over_five_percent(self):
        with self.assertRaisesRegex(RuntimeError, "large-text.*5.00%"):
            benchmark.enforce_7z_regression_gate({"large-text": 10.5001}, {"large-text": 10.0})


if __name__ == "__main__":
    unittest.main()
