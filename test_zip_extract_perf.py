"""ZIP extraction cost regressions and integrity checks; no timing thresholds."""
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import zipfile

import ziplib


class ZipExtractPerfTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="yzip-extract-perf-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "input.zip"
        self.dest = self.root / "out"

    def write_zip(self, members, method=zipfile.ZIP_DEFLATED):
        with zipfile.ZipFile(self.archive, "w", compression=method) as archive:
            for name, data in members.items():
                archive.writestr(name, data)

    def assert_no_parts(self):
        self.assertEqual(list(self.dest.rglob("*.part")), [])

    def test_plain_read_extract_and_test_do_not_import_crypto(self):
        self.write_zip({"hello.txt": b"hello" * 1024})
        script = """
import sys
import ziplib
with ziplib.Archive(sys.argv[1], 'unused password') as archive:
    assert archive.test() == []
    archive.extract(sys.argv[2])
assert 'pyzipper' not in sys.modules
assert not any(name.startswith('Cryptodome') for name in sys.modules)
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.archive), str(self.dest)],
                                cwd=Path(ziplib.__file__).resolve().parent,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.dest / "hello.txt").read_bytes(), b"hello" * 1024)

    def test_each_shared_output_directory_is_prepared_once(self):
        members = {f"group{i % 3}/file{i}.txt": str(i).encode() * 500 for i in range(48)}
        self.write_zip(members)
        with mock.patch.object(ziplib.os, "makedirs", wraps=os.makedirs) as mkdir:
            with ziplib.Archive(str(self.archive)) as archive:
                archive.extract(str(self.dest))
        for index in range(3):
            directory = os.path.realpath(self.dest / f"group{index}")
            self.assertEqual(sum(os.fspath(call.args[0]) == directory for call in mkdir.call_args_list), 1)
        for name, expected in members.items():
            self.assertEqual((self.dest / name).read_bytes(), expected)
        self.assert_no_parts()

    def test_crc_failure_preserves_existing_file_and_cleans_part(self):
        self.write_zip({"data.bin": b"good data" * 100}, zipfile.ZIP_STORED)
        with zipfile.ZipFile(self.archive) as archive:
            info = archive.getinfo("data.bin")
        data = bytearray(self.archive.read_bytes())
        name_len, extra_len = struct.unpack_from("<HH", data, info.header_offset + 26)
        data[info.header_offset + 30 + name_len + extra_len] ^= 1
        self.archive.write_bytes(data)
        self.dest.mkdir()
        target = self.dest / "data.bin"
        target.write_bytes(b"existing")
        with ziplib.Archive(str(self.archive)) as archive:
            with self.assertRaises(zipfile.BadZipFile):
                archive.extract(str(self.dest))
        self.assertEqual(target.read_bytes(), b"existing")
        self.assert_no_parts()

    def test_cancel_during_write_preserves_existing_file_and_cleans_part(self):
        self.write_zip({"data.bin": b"a" * (10 << 20)})
        self.dest.mkdir()
        target = self.dest / "data.bin"
        target.write_bytes(b"existing")
        cancelled = threading.Event()
        with ziplib.Archive(str(self.archive)) as archive:
            with self.assertRaises(ziplib.Cancelled):
                archive.extract(str(self.dest), progress=lambda *_: cancelled.set(), cancel=cancelled.is_set)
        self.assertEqual(target.read_bytes(), b"existing")
        self.assert_no_parts()

    def test_selection_and_all_overwrite_modes(self):
        self.write_zip({"a.txt": b"new", "excluded.txt": b"excluded"})
        self.dest.mkdir()
        target = self.dest / "a.txt"
        for mode in (False, "rename", True):
            with self.subTest(mode=mode):
                target.write_bytes(b"old")
                with ziplib.Archive(str(self.archive)) as archive:
                    archive.extract(str(self.dest), names=["a.txt"], overwrite=mode)
                self.assertEqual(target.read_bytes(), b"new" if mode is True else b"old")
                self.assertFalse((self.dest / "excluded.txt").exists())
        self.assertEqual((self.dest / "a (2).txt").read_bytes(), b"new")
        self.assert_no_parts()

    def test_bzip2_and_lzma_still_use_validating_reader(self):
        for method in (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
            with self.subTest(method=method):
                data = b"other method" * 10000
                self.write_zip({"data.bin": data}, method)
                with ziplib.Archive(str(self.archive)) as archive:
                    archive.extract(str(self.dest))
                    self.assertEqual(archive.test(), [])
                self.assertEqual((self.dest / "data.bin").read_bytes(), data)

    def test_shared_directory_preparation_does_not_follow_outside_symlink(self):
        self.write_zip({"escape/data.bin": b"must not escape", "safe/a.txt": b"a", "safe/b.txt": b"b"})
        self.dest.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        protected = outside / "data.bin"
        protected.write_bytes(b"existing outside")
        os.symlink(outside, self.dest / "escape", target_is_directory=True)
        with ziplib.Archive(str(self.archive)) as archive:
            archive.extract(str(self.dest))
        self.assertEqual(protected.read_bytes(), b"existing outside")
        self.assertEqual((self.dest / "safe/a.txt").read_bytes(), b"a")
        self.assertEqual((self.dest / "safe/b.txt").read_bytes(), b"b")
        self.assert_no_parts()

    def test_mixed_plain_aes_members_keep_password_validation(self):
        import pyzipper

        with pyzipper.AESZipFile(self.archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("plain.txt", b"plain")
            archive.setpassword(b"right")
            archive.setencryption(pyzipper.WZ_AES, nbits=256)
            archive.writestr("secret.txt", b"secret")
        with ziplib.Archive(str(self.archive), "right") as archive:
            archive.extract(str(self.dest))
            self.assertEqual(archive.test(), [])
        self.assertEqual((self.dest / "plain.txt").read_bytes(), b"plain")
        self.assertEqual((self.dest / "secret.txt").read_bytes(), b"secret")
        (self.dest / "secret.txt").write_bytes(b"old secret")
        with ziplib.Archive(str(self.archive), "wrong") as archive:
            with self.assertRaises(ziplib.BadPassword):
                archive.extract(str(self.dest))
        self.assertEqual((self.dest / "secret.txt").read_bytes(), b"old secret")
        self.assert_no_parts()


if __name__ == "__main__":
    unittest.main()
