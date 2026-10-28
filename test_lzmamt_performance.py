"""Real-code regressions for bounded small-file I/O and fast 7z decoding."""
import builtins
import io
import lzma
import os
from pathlib import Path
import tempfile
import tracemalloc
import unittest
from unittest import mock

import py7zr

import formats
import lzmamt


class LzmaPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yzip-lzmamt-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_packed_payload_slicing_does_not_allocate_another_payload(self):
        source = lzmamt._Buf(io.BytesIO().read)
        source.buf = b"data" * (1 << 20) + b"tail"
        tracemalloc.start()
        try:
            part = source.take(4 << 20)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(len(part), 4 << 20)
        self.assertEqual(part[:4], b"data")
        self.assertLess(peak, 128 << 10)
        self.assertEqual(source.take(4), b"tail")

    def test_independent_blocks_round_trip_at_low_and_high_presets(self):
        for preset in (0, 5, 9):
            for payload in (b"", os.urandom(8192), b"archive contents\n" * 2000):
                with self.subTest(preset=preset, size=len(payload)):
                    packed = lzmamt._raw_lzma2(preset)(payload) + b"\0"
                    decoded = lzma.decompress(packed, format=lzma.FORMAT_RAW,
                                             filters=[{"id": lzma.FILTER_LZMA2, "dict_size": 64 << 20}])
                    self.assertEqual(decoded, payload)

    def test_small_files_do_not_request_megabyte_read_buffers(self):
        source = self.root / "source"
        source.mkdir()
        expected = {}
        for i in range(40):
            payload = (f"file {i}: archive test\n".encode() * 400)[:8192]
            expected[f"f{i}.txt"] = payload
            (source / f"f{i}.txt").write_bytes(payload)
        requests = []

        class Reader:
            def __init__(self, file):
                self.file = file

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.file.__exit__(*args)

            def read(self, size=-1):
                requests.append(size)
                return self.file.read(size)

        def traced_open(path, mode="r", *args, **kwargs):
            file = builtins.open(path, mode, *args, **kwargs)
            return Reader(file) if mode == "rb" and Path(path).parent == source else file

        archive = self.root / "small.7z"
        with mock.patch.object(lzmamt, "open", traced_open, create=True):
            formats.create("7z", [str(source)], str(archive), level=5)
        self.assertTrue(requests)
        self.assertLessEqual(max(requests), 64 << 10)
        with formats.open_any(str(archive)) as reader:
            self.assertEqual(reader.test(), [])
            reader.extract(str(self.root / "out"))
        actual = {p.name: p.read_bytes() for p in (self.root / "out/source").iterdir()}
        self.assertEqual(actual, expected)

    def test_small_file_archive_has_independent_parallel_decode_pieces(self):
        source = self.root / "many"
        source.mkdir()
        for i in range(64):
            (source / f"{i}.txt").write_bytes(bytes([i]) * (32 << 10))
        archive = self.root / "parallel.7z"
        with mock.patch.object(lzmamt.runtimeconfig, "worker_count", return_value=4):
            formats.create("7z", [str(source)], str(archive), level=5)
        with py7zr.SevenZipFile(archive) as reader:
            folder, = lzmamt.fast_plan(reader)
        with archive.open("rb") as file:
            file.seek(folder["offset"])
            packed = file.read(folder["packsize"])
        pieces = list(lzmamt._segments(lzmamt._Buf(io.BytesIO(packed).read)))
        self.assertGreaterEqual(len(pieces), 2)
        self.assertTrue(all(fresh and complete for _, _, fresh, complete in pieces))
        decoded = b"".join(lzma.decompress(piece + b"\0", format=lzma.FORMAT_RAW,
                                        filters=[{"id": lzma.FILTER_LZMA2, "dict_size": 8 << 20}])
                           for piece, _, _, _ in pieces)
        self.assertEqual(len(decoded), 2 << 20)
        with formats.open_any(str(archive)) as reader:
            self.assertEqual(reader.test(), [])


if __name__ == "__main__":
    unittest.main()
