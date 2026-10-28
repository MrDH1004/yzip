import tempfile
import unittest
from pathlib import Path
from unittest import mock

import benchmark_native as benchmark


class BenchmarkNativeTests(unittest.TestCase):
    def test_expand_cases_is_deterministic(self):
        with tempfile.TemporaryDirectory() as temp:
            cases = benchmark.expand_cases(Path(temp) / "source", Path(temp) / "archives")

        self.assertEqual(
            [(case.tool, case.format, case.operation, case.threads) for case in cases],
            [
                (tool, archive_format, operation, threads)
                for tool in ("bandizip", "yzip")
                for archive_format in ("zip", "7z")
                for operation in ("compress", "extract")
                for threads in (1, 4, 8, 16)
            ],
        )
        self.assertTrue(all(case.level == 5 for case in cases))

    def test_run_case_excludes_warmup_and_calculates_median_and_p95(self):
        case = benchmark.BenchmarkCase(
            tool="bandizip", format="zip", operation="compress", threads=4,
            level=5, source=Path("source"), archive=Path("archive.zip"),
        )
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
        base = dict(format="7z", operation="compress", level=5,
                    source=Path("source"), archive=Path("archive.7z"))
        bandizip = benchmark.build_command(benchmark.BenchmarkCase(tool="bandizip", threads=8, **base), Path("out"))
        yzip = benchmark.build_command(benchmark.BenchmarkCase(tool="yzip", threads=16, **base), Path("out"))

        self.assertIn("-t:8", bandizip)
        self.assertIn("runtimeconfig.configure(16", yzip[2])

    def test_run_case_fails_when_extracted_tree_hash_mismatches_source(self):
        case = benchmark.BenchmarkCase(
            tool="yzip", format="zip", operation="extract", threads=1,
            level=5, source=Path("source"), archive=Path("archive.zip"),
        )
        with mock.patch.object(
            benchmark, "_run_iteration",
            return_value=benchmark.ProcessSample(1.0, 1.0, 1, 0, tree_sha256="different-hash"),
        ):
            with mock.patch.object(benchmark, "tree_sha256", return_value="source-hash"):
                with self.assertRaisesRegex(RuntimeError, "SHA-256 tree mismatch"):
                    benchmark.run_case(case, runs=1)


if __name__ == "__main__":
    unittest.main()
