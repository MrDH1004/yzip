"""ZIP pipeline regressions. Optional benchmarks run variants sequentially.

python test_zipwriter_performance.py --benchmark DATA_ROOT --runs 2
DATA_ROOT contains the existing small/text/random benchmark directories.
The reference comes from HEAD:zipwriter.py; outputs live in a temporary folder.
"""
import hashlib
import json
import os
from pathlib import Path
import random
import struct
import tempfile
import threading
import unittest
from unittest import mock
import zipfile
import zlib

import pyzipper

import ziplib
import zipwriter


class ZipPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="yzip-zip-pipeline-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "result.zip"

    def files(self, payloads):
        items = []
        for index, (name, content) in enumerate(payloads.items()):
            path = self.root / f"source{index}.bin"
            path.write_bytes(content)
            items.append((str(path), name))
        return items

    def test_small_file_compressed_bytes_are_identical_to_existing_deflate(self):
        payloads = {"empty": b"", "one": b"x", "text": b"alpha beta gamma\n" * 500,
                    "random": random.Random(42).randbytes(8192)}
        items = self.files(payloads)
        for level in (1, 5, 9):
            with self.subTest(level=level):
                zipwriter.write_zip(items, str(self.output), level=level, workers=2)
                archive = self.output.read_bytes()
                with zipfile.ZipFile(self.output) as z:
                    self.assertIsNone(z.testzip())
                    for name, data in payloads.items():
                        info = z.getinfo(name)
                        name_len, extra_len = struct.unpack_from("<HH", archive, info.header_offset + 26)
                        start = info.header_offset + 30 + name_len + extra_len
                        actual = archive[start:start + info.compress_size]
                        expected = (zipwriter._deflate(data, level) if data else b"") + zipwriter._FINAL_BLOCK
                        self.assertEqual(actual, expected)
                        self.assertEqual(z.read(name), data)

    def test_metadata_is_collected_once_per_item(self):
        items = self.files({"a": b"a", "b": b"b"})
        directory = self.root / "empty-directory"
        directory.mkdir()
        items.append((str(directory), "empty/"))
        real_stat = os.stat
        with mock.patch.object(zipwriter.os, "stat", wraps=real_stat) as stat:
            zipwriter.write_zip(iter(items), str(self.output), workers=2)
        self.assertEqual(stat.call_count, len(items))
        with zipfile.ZipFile(self.output) as z:
            self.assertIn("empty/", z.namelist())

    def test_small_file_reads_run_concurrently_on_the_limited_pool(self):
        items = self.files({f"file{i}": b"sample" * 100 for i in range(4)})
        original = zipwriter._small_file
        rendezvous = threading.Barrier(2)
        threads = set()
        lock = threading.Lock()
        def read(*args):
            with lock:
                threads.add(threading.get_ident())
            rendezvous.wait(timeout=5)
            return original(*args)
        with mock.patch.object(zipwriter.runtimeconfig, "worker_count", return_value=2), \
                mock.patch.object(zipwriter, "_small_file", side_effect=read):
            zipwriter.write_zip(items, str(self.output), workers=8)
        self.assertEqual(len(threads), 2)
        with zipfile.ZipFile(self.output) as z:
            self.assertIsNone(z.testzip())

    def test_mixed_pipeline_preserves_order_crc_progress_and_callback_thread(self):
        payloads = {"small": b"s" * 3000, "large": b"large payload\n" * 180000,
                    "empty": b"", "last": b"last"}
        items = self.files(payloads)
        events = []
        caller = threading.get_ident()
        def progress(done, total, name):
            self.assertEqual(threading.get_ident(), caller)
            events.append((done, total, name))
        zipwriter.write_zip(items, str(self.output), workers=2, progress=progress)
        self.assertTrue(events)
        self.assertEqual(events[-1][0], sum(map(len, payloads.values())))
        self.assertTrue(all(a[0] <= b[0] for a, b in zip(events, events[1:])))
        with zipfile.ZipFile(self.output) as z:
            self.assertEqual(z.namelist(), list(payloads))
            for name, data in payloads.items():
                self.assertEqual(z.read(name), data)
                self.assertEqual(z.getinfo(name).CRC, zlib.crc32(data))

    def test_small_file_boundary_and_store_roundtrip(self):
        payloads = {"exact": b"x" * zipwriter.CHUNK, "over": b"y" * (zipwriter.CHUNK + 1), "zero": b""}
        items = self.files(payloads)
        for store in (False, True):
            with self.subTest(store=store):
                zipwriter.write_zip(items, str(self.output), store_only=store, workers=2)
                with zipfile.ZipFile(self.output) as z:
                    for name, data in payloads.items():
                        self.assertEqual(z.read(name), data)
                        self.assertEqual(z.getinfo(name).compress_type, 0 if store else 8)

    def test_aes_small_files_zip64_and_store_preserve_authentication(self):
        payloads = {"unicode-한글": b"private text\n" * 200, "empty": b""}
        items = self.files(payloads)
        for store in (False, True):
            with self.subTest(store=store), mock.patch.object(zipwriter, "ZIP64_LIMIT", 1000):
                zipwriter.write_zip(items, str(self.output), store_only=store, password="secret", workers=2)
                with pyzipper.AESZipFile(self.output) as z:
                    z.setpassword(b"secret")
                    for name, data in payloads.items():
                        self.assertEqual(z.read(name), data)
                    z.setpassword(b"wrong")
                    with self.assertRaises(RuntimeError):
                        z.read("unicode-한글")

    def test_cancel_from_small_file_progress_discards_transaction(self):
        source = self.root / "input"
        source.mkdir()
        for index in range(12):
            (source / f"{index}.txt").write_bytes(b"input" * 1000)
        stopped = False
        def progress(*args):
            nonlocal stopped
            stopped = True
        with self.assertRaises(ziplib.Cancelled):
            ziplib.create_zip([str(source)], str(self.output), progress=progress, cancel=lambda: stopped)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob("result.zip*")), [])

    def test_growing_small_source_fails_instead_of_truncating(self):
        source = self.root / "input.txt"
        source.write_bytes(b"initial")
        original = zipwriter._small_file
        def grow(path, *args):
            Path(path).write_bytes(b"grown" * (zipwriter.CHUNK // 5 + 1))
            return original(path, *args)
        with mock.patch.object(zipwriter, "_small_file", side_effect=grow):
            with self.assertRaises(OSError):
                ziplib.create_zip([str(source)], str(self.output))
        self.assertFalse(self.output.exists())

    def test_copy_entries_remain_unchanged_before_parallel_new_files(self):
        original = self.root / "original.zip"
        with zipfile.ZipFile(original, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("old", b"old data" * 100)
        with zipfile.ZipFile(original) as z:
            copied = z.infolist()
        zipwriter.write_zip(self.files({"new": b"new data"}), str(self.output),
                            copy=(str(original), [(copied[0], "renamed")]), workers=2)
        with zipfile.ZipFile(self.output) as z:
            self.assertEqual(z.namelist(), ["renamed", "new"])
            self.assertEqual(z.read("renamed"), b"old data" * 100)
            self.assertEqual(z.read("new"), b"new data")


def benchmark(data_root, runs):
    import statistics
    import subprocess
    import time
    import types

    source = subprocess.check_output(["git", "show", "HEAD:zipwriter.py"], cwd=Path(__file__).parent)
    before = types.ModuleType("baseline_zipwriter")
    exec(compile(source.decode("utf-8"), "baseline_zipwriter.py", "exec"), before.__dict__)
    print(json.dumps({"reference": "HEAD:zipwriter.py", "reference_sha256": hashlib.sha256(source).hexdigest(),
                      "candidate_sha256": hashlib.sha256(Path(zipwriter.__file__).read_bytes()).hexdigest(),
                      "level": 5, "workers": zipwriter.runtimeconfig.worker_count(16), "runs": runs,
                      "scope": "write_zip only; sequential alternating variants; CRC/hash validation excluded"}),
          flush=True)
    results = []
    with tempfile.TemporaryDirectory(prefix="yzip-zip-benchmark-") as temp:
        for dataset in ("small", "text", "random"):
            directory = Path(data_root) / dataset
            items = ziplib.collect_items([str(directory)])
            if not items:
                raise ValueError(f"No benchmark inputs: {directory}")
            timings = {"before": [], "after": []}
            archive_hashes = {}
            for repeat in range(runs):
                variants = [("before", before), ("after", zipwriter)]
                if repeat % 2:
                    variants.reverse()
                for name, module in variants:
                    output = Path(temp) / f"{dataset}-{name}.zip"
                    start = time.perf_counter()
                    module.write_zip(items, str(output), level=5)
                    timings[name].append(time.perf_counter() - start)
                    archive_hashes[name] = hashlib.sha256(output.read_bytes()).hexdigest()
                    with zipfile.ZipFile(output) as z:
                        if z.testzip() is not None:
                            raise AssertionError(f"CRC failure: {name}/{dataset}")
            if len(set(archive_hashes.values())) != 1:
                raise AssertionError(f"Archive bytes changed: {dataset}")
            result = {"dataset": dataset, "seconds": timings,
                      "median": {name: statistics.median(values) for name, values in timings.items()},
                      "archive_sha256_identical": True, "sha256": archive_hashes["after"]}
            results.append(result)
            print(json.dumps(result), flush=True)
    return results


if __name__ == "__main__":
    import sys
    if "--benchmark" in sys.argv:
        import argparse
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--benchmark", type=Path, required=True)
        parser.add_argument("--runs", type=int, default=2)
        args = parser.parse_args()
        if args.runs < 1:
            parser.error("--runs must be positive")
        benchmark(args.benchmark, args.runs)
    else:
        unittest.main()
