import binascii
import hashlib
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

import formats
import nativearchive
from ziplib import BadPassword


HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "testdata")


def tree(root):
    out = {}
    for dp, _, files in os.walk(root):
        for name in files:
            path = os.path.join(dp, name)
            with open(path, "rb") as fh:
                out[os.path.relpath(path, root).replace("\\", "/")] = hashlib.sha256(fh.read()).hexdigest()
    return out


class NativeArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_libarchive_is_actionable(self):
        import nativearchive_libarchive
        import nativearchive_ffi

        with mock.patch.object(nativearchive_ffi, "libarchive_dll_path", return_value=os.path.join(
                self.tmp, "missing-libarchive.dll")):
            with self.assertRaises(nativearchive.NativeBackendUnavailable) as ctx:
                nativearchive_libarchive.LibArchive(os.path.join(self.tmp, "missing.rar"), "rar")
        self.assertIn("libarchive", str(ctx.exception).lower())

    def test_missing_legacy_module_is_actionable(self):
        import nativearchive_legacy

        with mock.patch.object(nativearchive_legacy, "native_dll_path", return_value=os.path.join(
                self.tmp, "missing-yzip-native.dll")):
            with self.assertRaises(nativearchive.NativeBackendUnavailable) as ctx:
                nativearchive_legacy.LegacyArchive(os.path.join(DATA, "normal.egg"), "egg")
        self.assertIn("yzip_native", str(ctx.exception))

    def test_rar_volume_names_are_passed_as_one_reader_set(self):
        import nativearchive_ffi

        first = os.path.join(self.tmp, "archive.part01.rar")
        second = os.path.join(self.tmp, "archive.part02.rar")
        with open(first, "wb"):
            pass
        with open(second, "wb"):
            pass
        self.assertEqual(nativearchive_ffi.volume_paths(first), [first, second])

    def test_egg_and_alz_use_native_module_without_a_child_process(self):
        if not nativearchive.available("egg") or not nativearchive.available("alz"):
            self.skipTest("native EGG/ALZ module is not built")
        with mock.patch("subprocess.Popen", side_effect=AssertionError("native archive spawned a process")):
            for name in ("normal.egg", "solid.egg", "split_store.vol2.egg", "normal.alz", "bzip2.alz",
                         "dir_tree.alz", "split_store.a01"):
                source = os.path.join(DATA, name)
                out = os.path.join(self.tmp, os.path.splitext(name)[0])
                with formats.open_any(source) as archive:
                    self.assertTrue(archive.entries())
                    self.assertEqual(archive.test(), [])
                    archive.extract(out)
                self.assertTrue(tree(out), name)

    def test_native_password_error_does_not_write_output(self):
        if not nativearchive.available("egg"):
            self.skipTest("native EGG/ALZ module is not built")
        source = os.path.join(DATA, "enc_aes256.egg")
        out = os.path.join(self.tmp, "out")
        with formats.open_any(source, "wrong") as archive:
            with self.assertRaises(BadPassword):
                archive.extract(out)
        self.assertEqual(tree(out) if os.path.exists(out) else {}, {})

    def test_corrupt_egg_does_not_write_a_final_file(self):
        if not nativearchive.available("egg"):
            self.skipTest("native EGG/ALZ module is not built")
        source = os.path.join(DATA, "normal.egg")
        damaged = os.path.join(self.tmp, "damaged.egg")
        with open(source, "rb") as fh:
            data = bytearray(fh.read())
        data[len(data) // 2] ^= 0xFF
        with open(damaged, "wb") as fh:
            fh.write(data)
        out = os.path.join(self.tmp, "out")
        with formats.open_any(damaged) as archive:
            self.assertTrue(archive.test())
            with self.assertRaises(ValueError):
                archive.extract(out)
        self.assertEqual(tree(out) if os.path.exists(out) else {}, {})

    def test_cab_is_read_in_process(self):
        if not nativearchive.available("cab"):
            self.skipTest("libarchive.dll is not bundled")
        source = os.path.join(self.tmp, "data.bin")
        with open(source, "wb") as fh:
            fh.write(b"cab payload" * 1000)
        cab = os.path.join(self.tmp, "data.cab")
        subprocess.run(["makecab", source, cab], check=True, capture_output=True)
        out = os.path.join(self.tmp, "out")
        with mock.patch("subprocess.Popen", side_effect=AssertionError("native archive spawned a process")):
            with formats.open_any(cab) as archive:
                self.assertEqual(formats.detect(cab), "cab")
                self.assertEqual([entry.name for entry in archive.entries()], ["data.bin"])
                archive.extract(out)
        self.assertEqual(tree(out), {"data.bin": hashlib.sha256(b"cab payload" * 1000).hexdigest()})

    def test_rar5_fixture_is_read_in_process(self):
        if not nativearchive.available("rar"):
            self.skipTest("libarchive.dll is not bundled")
        encoded = os.path.join(DATA, "libarchive_rar5_stored.rar.uu")
        rar = os.path.join(self.tmp, "fixture.rar")
        with open(encoded, "rb") as fh:
            lines = fh.read().splitlines()
        data = b"".join(binascii.a2b_uu(line) for line in lines
                          if line and not line.startswith((b"begin ", b"end")) and line != b"`")
        with open(rar, "wb") as fh:
            fh.write(data)
        out = os.path.join(self.tmp, "out")
        with mock.patch("subprocess.Popen", side_effect=AssertionError("native archive spawned a process")):
            with formats.open_any(rar) as archive:
                self.assertEqual(formats.detect(rar), "rar")
                self.assertEqual([entry.name for entry in archive.entries()], ["helloworld.txt"])
                self.assertEqual(archive.test(), [])
                archive.extract(out)
        with open(os.path.join(out, "helloworld.txt"), "rb") as fh:
            self.assertEqual(fh.read(), b"hello libarchive test suite!\n")

    def test_rar5_encrypted_fixture_reports_unsupported_decryption_without_writing(self):
        if not nativearchive.available("rar"):
            self.skipTest("libarchive.dll is not bundled")
        encoded = os.path.join(DATA, "libarchive_rar5_encrypted.rar.uu")
        rar = os.path.join(self.tmp, "encrypted.rar")
        with open(encoded, "rb") as fh:
            lines = fh.read().splitlines()
        data = b"".join(binascii.a2b_uu(line) for line in lines
                          if line and not line.startswith((b"begin ", b"end")) and line != b"`")
        with open(rar, "wb") as fh:
            fh.write(data)
        out = os.path.join(self.tmp, "out")
        with formats.open_any(rar, "password") as archive:
            self.assertTrue(archive.needs_password())
            with self.assertRaises(BadPassword):
                archive.test()
            with self.assertRaises(BadPassword):
                archive.extract(out)
        self.assertEqual(tree(out) if os.path.exists(out) else {}, {})

    def test_rar5_compressed_fixture_is_decompressed_in_process(self):
        if not nativearchive.available("rar"):
            self.skipTest("libarchive.dll is not bundled")
        encoded = os.path.join(DATA, "libarchive_rar5_compressed.rar.uu")
        rar = os.path.join(self.tmp, "compressed.rar")
        with open(encoded, "rb") as fh:
            lines = fh.read().splitlines()
        data = b"".join(binascii.a2b_uu(line) for line in lines
                          if line and not line.startswith((b"begin ", b"end")) and line != b"`")
        with open(rar, "wb") as fh:
            fh.write(data)
        expected = b"".join(struct.pack("<i", max(0, k * k - 3 * k + 1)) for k in range(1, 301))
        out = os.path.join(self.tmp, "out")
        with formats.open_any(rar) as archive:
            self.assertEqual([entry.name for entry in archive.entries()], ["test.bin"])
            self.assertEqual(archive.test(), [])
            archive.extract(out)
        with open(os.path.join(out, "test.bin"), "rb") as fh:
            self.assertEqual(fh.read(), expected)

    def test_iso9660_joliet_unicode_is_read_in_process(self):
        if not nativearchive.available("iso"):
            self.skipTest("libarchive.dll is not bundled")
        source = os.path.join(self.tmp, "src")
        os.makedirs(os.path.join(source, "한글"))
        with open(os.path.join(source, "한글", "a.txt"), "w", encoding="utf-8") as fh:
            fh.write("ISO unicode payload")
        iso = os.path.join(self.tmp, "disc.iso")
        script = os.path.join(self.tmp, "mkiso.ps1")
        with open(script, "w", encoding="utf-8-sig") as fh:
            fh.write(
                "param($src, $iso)\n"
                "Add-Type -TypeDefinition 'public static class IsoW { public static void Save(object o, string p) {"
                " var s = (System.Runtime.InteropServices.ComTypes.IStream)o; var b = new byte[65536];"
                " var n = System.Runtime.InteropServices.Marshal.AllocHGlobal(4);"
                " using (var f = System.IO.File.Create(p)) { while (true) { s.Read(b, b.Length, n);"
                " int k = System.Runtime.InteropServices.Marshal.ReadInt32(n); if (k <= 0) break; f.Write(b, 0, k); } }"
                " System.Runtime.InteropServices.Marshal.FreeHGlobal(n); } }'\n"
                "$fs = New-Object -ComObject IMAPI2FS.MsftFileSystemImage\n"
                "$fs.FileSystemsToCreate = 3\n$fs.VolumeName = 'T'\n$fs.Root.AddTree($src, $false)\n"
                "[IsoW]::Save($fs.CreateResultImage().ImageStream, $iso)\n"
            )
        result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, source, iso],
                                capture_output=True, timeout=120)
        if not os.path.exists(iso) or os.path.getsize(iso) == 0:
            self.skipTest("IMAPI2 not available: " + result.stderr.decode("utf-8", "replace")[-200:])
        out = os.path.join(self.tmp, "out")
        with mock.patch("subprocess.Popen", side_effect=AssertionError("native archive spawned a process")):
            with formats.open_any(iso) as archive:
                names = [entry.name for entry in archive.entries()]
                self.assertTrue(any(name.endswith("/a.txt") for name in names))
                self.assertEqual(archive.test(), [])
                archive.extract(out)
        with open(os.path.join(out, "한글", "a.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "ISO unicode payload")


if __name__ == "__main__":
    unittest.main()
