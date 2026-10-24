import hashlib
import os
import subprocess
import tempfile
import unittest
import zipfile

import zipwriter
import ziplib

BZ = r"C:\Program Files\Bandizip\bz.exe"


def sha(b):
    return hashlib.sha256(b).hexdigest()


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.src = os.path.join(self.root, "src")
        os.makedirs(os.path.join(self.src, "빈 폴더"))
        os.makedirs(os.path.join(self.src, "sub"))
        C = zipwriter.CHUNK
        self.payloads = {
            "empty.txt": b"",
            "one.bin": b"x",
            "exact_chunk.bin": os.urandom(C),
            "chunk_plus1.bin": os.urandom(C + 1),
            "text.txt": (b"the quick brown fox jumps over the lazy dog\n" * 120000),
            os.path.join("sub", "한글 파일.bin"): os.urandom(2 * C + 12345),
        }
        for rel, data in self.payloads.items():
            with open(os.path.join(self.src, rel), "wb") as f:
                f.write(data)
        self.out = os.path.join(self.root, "o.zip")

    def tearDown(self):
        zipwriter.ZIP64_LIMIT = 0xFFFFFFFF
        self.tmp.cleanup()

    def build(self, **kw):
        ziplib.create_zip([self.src], self.out, **kw)

    def assert_matches(self, path):
        with zipfile.ZipFile(path) as z:
            self.assertIsNone(z.testzip())
            names = set(z.namelist())
            for rel, data in self.payloads.items():
                arc = "src/" + rel.replace("\\", "/")
                self.assertIn(arc, names)
                self.assertEqual(sha(z.read(arc)), sha(data), rel)
            self.assertIn("src/빈 폴더/", names)

    def test_deflate(self):
        self.build(level=6)
        self.assert_matches(self.out)

    def test_levels(self):
        for lv in (1, 9):
            self.build(level=lv)
            self.assert_matches(self.out)

    def test_store(self):
        self.build(store_only=True)
        self.assert_matches(self.out)
        with zipfile.ZipFile(self.out) as z:
            self.assertTrue(all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist()))

    def test_zip64_entries_forced(self):
        zipwriter.ZIP64_LIMIT = 1000  # every file > 1000 bytes takes the zip64 header/descriptor path
        self.build()
        self.assert_matches(self.out)

    def test_zip64_end_record_with_many_entries(self):
        many = os.path.join(self.root, "many")
        os.makedirs(many)
        for i in range(0xFFFF + 5):
            open(os.path.join(many, f"{i}"), "wb").close()
        out = os.path.join(self.root, "many.zip")
        ziplib.create_zip([many], out)
        with zipfile.ZipFile(out) as z:
            self.assertEqual(len(z.namelist()), 0xFFFF + 5 + 1)

    def test_mtime_preserved(self):
        p = os.path.join(self.src, "one.bin")
        os.utime(p, (1_600_000_000, 1_600_000_000))
        self.build()
        with zipfile.ZipFile(self.out) as z:
            y, mo, d = z.getinfo("src/one.bin").date_time[:3]
        import time
        lt = time.localtime(1_600_000_000)
        self.assertEqual((y, mo, d), (lt.tm_year, lt.tm_mon, lt.tm_mday))

    def test_ratio_close_to_stdlib(self):
        self.build(level=6)
        mine = os.path.getsize(self.out)
        ref = os.path.join(self.root, "ref.zip")
        with zipfile.ZipFile(ref, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for rel in self.payloads:
                z.write(os.path.join(self.src, rel), rel)
        self.assertLess(mine, os.path.getsize(ref) * 1.02 + 2000)

    def test_cancel_leaves_nothing(self):
        n = {"i": 0}

        def cancel():
            n["i"] += 1
            return n["i"] > 3
        with self.assertRaises(ziplib.Cancelled):
            ziplib.create_zip([self.src], self.out, cancel=cancel)
        self.assertEqual([f for f in os.listdir(self.root) if f.startswith("o.zip")], [])

    def test_progress_reaches_total(self):
        seen = []
        ziplib.create_zip([self.src], self.out, progress=lambda d, t, n: seen.append((d, t)))
        self.assertTrue(seen and seen[-1][0] == seen[-1][1] and all(a[0] <= b[0] for a, b in zip(seen, seen[1:])))

    def _aes_entries(self, path):
        with zipfile.ZipFile(path) as z:
            return [i for i in z.infolist() if not i.is_dir()]

    def test_aes_roundtrip_and_format(self):
        import pyzipper
        for kw in ({}, {"store_only": True}, {"level": 9}):
            self.build(password="암호Pw!", **kw)
            files = self._aes_entries(self.out)
            self.assertTrue(all(i.compress_type == 99 and i.flag_bits & 1 for i in files))
            self.assertTrue(all(b"\x01\x99\x07\x00\x02\x00AE\x03" in i.extra for i in files))  # AE-2, AES-256
            with pyzipper.AESZipFile(self.out) as z:
                z.setpassword("암호Pw!".encode("utf-8"))
                for rel, data in self.payloads.items():
                    self.assertEqual(sha(z.read("src/" + rel.replace("\\", "/"))), sha(data), (kw, rel))
            with ziplib.Archive(self.out, "틀린암호") as a:
                with self.assertRaises(ziplib.BadPassword):
                    a.test()

    def test_aes_every_entry_has_its_own_salt(self):
        self.build(password="pw")
        salts = []
        with open(self.out, "rb") as f:
            for i in self._aes_entries(self.out):
                f.seek(i.header_offset + 26)
                n, m = __import__("struct").unpack("<HH", f.read(4))
                f.seek(i.header_offset + 30 + n + m)
                salts.append(f.read(16))
        self.assertEqual(len(set(salts)), len(salts))

    def test_aes_zip64(self):
        zipwriter.ZIP64_LIMIT = 1000
        self.build(password="pw")
        with ziplib.Archive(self.out, "pw") as a:
            self.assertEqual(a.test(), [])

    def test_aes_bandizip_opens_it(self):
        if not os.path.exists(BZ):
            self.skipTest("Bandizip not installed")
        self.build(password="Pw1234")
        r = subprocess.run([BZ, "t", "-y", "-p:Pw1234", self.out], capture_output=True, timeout=120)
        self.assertIn(b"All OK", r.stdout, r.stdout[-300:])
        ex = os.path.join(self.root, "bz_out")
        subprocess.run([BZ, "x", "-y", "-p:Pw1234", f"-o:{ex}", self.out], capture_output=True, timeout=120)
        with open(os.path.join(ex, "src", "sub", "한글 파일.bin"), "rb") as f:
            self.assertEqual(sha(f.read()), sha(self.payloads[os.path.join("sub", "한글 파일.bin")]))
        r = subprocess.run([BZ, "t", "-y", "-p:wrong", self.out], capture_output=True, timeout=120)
        self.assertNotIn(b"All OK", r.stdout)

    def test_external_readers(self):
        zipwriter.ZIP64_LIMIT = 1_000_000  # mix of zip64 and plain entries
        self.build()
        ex = os.path.join(self.root, "tar_out")
        os.makedirs(ex)
        r = subprocess.run(["tar", "-xf", self.out, "-C", ex], capture_output=True)
        import ctypes
        if ctypes.windll.kernel32.GetACP() not in (949, 65001):
            # Windows tar (bsdtar) maps names through the ANSI code page: on an English system (CI runners)
            # it cannot name the Korean entries. That is the reader's limit; everything else must extract.
            errs = [ln for ln in r.stderr.decode(errors="replace").splitlines()
                    if ln and "unreadable filename" not in ln and "Error exit delayed" not in ln]
            self.assertEqual(errs, [], r.stderr)
        else:
            self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(ex, "src", "exact_chunk.bin"), "rb") as f:
            self.assertEqual(sha(f.read()), sha(self.payloads["exact_chunk.bin"]))
        if os.path.exists(BZ):
            r = subprocess.run([BZ, "t", "-y", self.out], capture_output=True, timeout=60)
            self.assertIn(b"All OK", r.stdout, r.stdout[-300:])


if __name__ == "__main__":
    unittest.main()
