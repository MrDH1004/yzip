"""Small-file 7z I/O regressions; only isolated temporary archives are used."""
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import py7zr

import formats
import lzmamt
import ziplib


class SevenIOTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yzip-seven-io-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dest = self.root / "out"
        self.dest.mkdir()
        self.archive = self.root / "input.7z"

    def pack(self, entries, fallback=False):
        filters = None if fallback else [{"id": py7zr.FILTER_LZMA2}]
        with py7zr.SevenZipFile(self.archive, "w", filters=filters) as archive:
            for name, data in entries:
                archive.writestr(data, name)
        return formats.SevenZArchive(str(self.archive))

    def contents(self):
        return {p.relative_to(self.dest).as_posix(): p.read_bytes()
                for p in self.dest.rglob("*") if p.is_file()}

    def test_small_files_remain_in_memory_until_all_crc_checks_finish(self):
        archive = self.pack([("a", b"first"), ("b", b"second")])
        original = lzmamt.fast_extract

        def decode(*args):
            original(*args)
            self.assertEqual(self.contents(), {})

        with mock.patch.object(lzmamt, "fast_extract", side_effect=decode):
            archive.extract(str(self.dest))
        self.assertEqual(self.contents(), {"a": b"first", "b": b"second"})

    def test_memory_budget_spills_to_parts_without_publishing_targets(self):
        archive = self.pack([(str(i), bytes([i]) * 8) for i in range(4)])
        original = lzmamt.fast_extract

        def decode(*args):
            original(*args)
            files = self.contents()
            self.assertEqual(len(files), 2)
            self.assertTrue(all(name.endswith(".part") for name in files))

        with mock.patch.object(formats, "_SEVEN_MEMORY_LIMIT", 16), \
                mock.patch.object(lzmamt, "fast_extract", side_effect=decode):
            archive.extract(str(self.dest))
        self.assertEqual(self.contents(), {str(i): bytes([i]) * 8 for i in range(4)})

    def test_bad_final_crc_preserves_all_existing_targets_and_cleans_parts(self):
        archive = self.pack([("a", b"first"), ("b", b"second")])
        (self.dest / "a").write_bytes(b"precious")
        original = lzmamt.fast_plan

        def bad_crc(reader):
            folders = original(reader)
            fi, size, crc = folders[-1]["members"][-1]
            folders[-1]["members"][-1] = (fi, size, crc ^ 1)
            return folders

        with mock.patch.object(formats, "_SEVEN_MEMORY_LIMIT", 5), \
                mock.patch.object(lzmamt, "fast_plan", side_effect=bad_crc):
            with self.assertRaisesRegex(ValueError, "CRC mismatch"):
                archive.extract(str(self.dest))
        self.assertEqual(self.contents(), {"a": b"precious"})

    def test_cancel_during_decode_preserves_targets_and_cleans_buffers_and_parts(self):
        archive = self.pack([("a", b"first"), ("b", b"second")])
        (self.dest / "a").write_bytes(b"precious")
        with self.assertRaises(ziplib.Cancelled):
            archive.extract(str(self.dest), cancel=lambda: True)
        self.assertEqual(self.contents(), {"a": b"precious"})

    def test_cancel_after_decode_prevents_commit(self):
        archive = self.pack([("a", b"first")])
        (self.dest / "a").write_bytes(b"precious")
        stop = threading.Event()
        original = lzmamt.fast_extract

        def decode(*args):
            original(*args)
            stop.set()

        with mock.patch.object(lzmamt, "fast_extract", side_effect=decode):
            with self.assertRaises(ziplib.Cancelled):
                archive.extract(str(self.dest), cancel=stop.is_set)
        self.assertEqual(self.contents(), {"a": b"precious"})

    def test_repeated_targets_commit_in_archive_order_and_rename_keeps_both(self):
        archive = self.pack([("same", b"first"), ("same", b"last")])
        second_started = threading.Event()
        finish = ziplib.finish_part

        def commit(part, target):
            if Path(part).read_bytes() == b"first":
                self.assertFalse(second_started.wait(0.05), "same target committed concurrently")
            else:
                second_started.set()
            finish(part, target)

        with mock.patch.object(formats.runtimeconfig, "worker_count", return_value=2), \
                mock.patch.object(ziplib, "finish_part", side_effect=commit):
            archive.extract(str(self.dest))
        self.assertEqual(self.contents(), {"same": b"last"})
        archive.extract(str(self.dest), overwrite="rename")
        self.assertEqual(self.contents(), {"same": b"last", "same (2)": b"first", "same (3)": b"last"})

    def test_py7zr_fallback_keeps_random_access_and_empty_files(self):
        archive = self.pack([("a", b"content" * 100), ("empty", b"")], fallback=True)
        with mock.patch.object(lzmamt, "fast_plan", side_effect=lzmamt.Unsupported):
            archive.extract(str(self.dest))
        self.assertEqual(self.contents(), {"a": b"content" * 100, "empty": b""})

    def test_seek_read_and_oversized_write_spill_preserve_bytes(self):
        archive = self.pack([("a", b"1234")])

        def decode(path, folders, password, open_out, counter):
            output = open_out(SimpleNamespace(id=0))
            output.write(b"abcd")
            self.assertEqual(output.seek(1), 1)
            self.assertEqual(output.read(2), b"bc")
            output.seek(8)
            output.write(b"z")  # beyond the declared size: must spill before allocating a hole
            output.seek(0)
            self.assertEqual(output.read(), b"abcd\0\0\0\0z")
            output.flush()
            output.close()

        with mock.patch.object(lzmamt, "fast_extract", side_effect=decode):
            archive.extract(str(self.dest))
        self.assertEqual(self.contents(), {"a": b"abcd\0\0\0\0z"})

    def test_large_member_uses_disk_and_honors_seek_read(self):
        archive = self.pack([("a", b"12345")])

        def decode(path, folders, password, open_out, counter):
            output = open_out(SimpleNamespace(id=0))
            output.write(b"abcde")
            output.seek(1)
            output.write(b"Z")
            output.seek(0)
            self.assertEqual(output.read(), b"aZcde")
            output.close()
            self.assertEqual(len(list(self.dest.glob("*.part"))), 1)

        with mock.patch.object(formats, "_SEVEN_FILE_LIMIT", 4), \
                mock.patch.object(lzmamt, "fast_extract", side_effect=decode):
            archive.extract(str(self.dest))
        self.assertEqual(self.contents(), {"a": b"aZcde"})

    def test_independent_targets_commit_concurrently_and_wait_for_completion(self):
        archive = self.pack([("a", b"first"), ("b", b"second")])
        rendezvous = threading.Barrier(2)
        finish = ziplib.finish_part

        def commit(part, target):
            rendezvous.wait(timeout=3)
            finish(part, target)

        with mock.patch.object(formats.runtimeconfig, "worker_count", return_value=2), \
                mock.patch.object(ziplib, "finish_part", side_effect=commit):
            archive.extract(str(self.dest))
        self.assertEqual(self.contents(), {"a": b"first", "b": b"second"})

    def test_commit_failure_cleans_uncommitted_parts_and_preserves_failed_target(self):
        archive = self.pack([("a", b"new")])
        (self.dest / "a").write_bytes(b"precious")
        with mock.patch.object(ziplib, "finish_part", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(PermissionError, "denied"):
                archive.extract(str(self.dest))
        self.assertEqual(self.contents(), {"a": b"precious"})


if __name__ == "__main__":
    unittest.main()
