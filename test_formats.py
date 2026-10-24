import hashlib
import io
import os
import subprocess
import tarfile
import tempfile
import unittest

import formats
import lzmamt
import ziplib

BZ = r"C:\Program Files\Bandizip\bz.exe"


def sha(b):
    return hashlib.sha256(b).hexdigest()


def tree(root):
    return {os.path.relpath(os.path.join(dp, f), root).replace("\\", "/"): sha(open(os.path.join(dp, f), "rb").read())
            for dp, _, fs in os.walk(root) for f in fs}


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.src = os.path.join(self.root, "자료")
        os.makedirs(os.path.join(self.src, "하위 폴더"))
        os.makedirs(os.path.join(self.src, "빈 폴더"))
        for rel, data in (("a.txt", b"hello " * 50000), ("empty.bin", b""), ("r.bin", os.urandom(300_000)),
                          (os.path.join("하위 폴더", "한글.txt"), "안녕".encode() * 999)):
            with open(os.path.join(self.src, rel), "wb") as f:
                f.write(data)
        self.want = tree(self.src)

    def tearDown(self):
        self.tmp.cleanup()

    def roundtrip(self, fid, **kw):
        out = os.path.join(self.root, "o" + formats.BY_ID[fid].ext)
        formats.create(fid, [self.src], out, **kw)
        self.assertEqual(formats.detect(out), "tar" if fid.startswith("t") else fid)
        dest = os.path.join(self.root, "x_" + fid)
        with formats.open_any(out, kw.get("password")) as a:
            names = [e.name for e in a.entries()]
            self.assertIn("자료/하위 폴더/한글.txt", names)
            self.assertIn("자료/빈 폴더/", names)
            self.assertEqual(a.test(), [])
            a.extract(dest)
        self.assertEqual(tree(os.path.join(dest, "자료")), self.want, fid)
        self.assertTrue(os.path.isdir(os.path.join(dest, "자료", "빈 폴더")))
        return out

    def test_every_archive_format_roundtrips(self):
        for f in formats.FORMATS:
            if not f.single:
                with self.subTest(fmt=f.id):
                    self.roundtrip(f.id)

    def test_levels_and_store(self):
        for fid in ("7z", "tgz", "txz", "tzst"):
            for kw in ({"level": 1}, {"level": 9}, {"store_only": True}):
                with self.subTest(fmt=fid, **kw):
                    self.roundtrip(fid, **kw)

    def test_single_file_streams(self):
        p = os.path.join(self.src, "r.bin")
        for f in formats.FORMATS:
            if not f.single:
                continue
            with self.subTest(fmt=f.id):
                out = os.path.join(self.root, "r.bin" + f.ext)
                formats.create(f.id, [p], out)
                self.assertEqual(formats.detect(out), f.id)
                with formats.open_any(out) as a:
                    self.assertEqual([e.name for e in a.entries()], ["r.bin"])
                    self.assertEqual(a.test(), [])
                    a.extract(os.path.join(self.root, "s_" + f.id))
                with open(os.path.join(self.root, "s_" + f.id, "r.bin"), "rb") as g:
                    self.assertEqual(sha(g.read()), self.want["r.bin"])
                with self.assertRaises(ValueError):  # a folder cannot go into a single-file stream
                    formats.create(f.id, [self.src], out + "2")

    def test_gzip_header_keeps_real_name(self):
        out = os.path.join(self.root, "r.bin.gz")
        formats.create("gz", [os.path.join(self.src, "r.bin")], out)
        with open(out, "rb") as f:
            head = f.read(64)
        self.assertIn(b"r.bin\x00", head)
        self.assertNotIn(b"partial", head)

    def test_7z_password(self):
        out = self.roundtrip("7z", password="암호Pw1")
        with self.assertRaises(ziplib.BadPassword):  # names are encrypted too
            formats.open_any(out)
        with self.assertRaises(ziplib.BadPassword):
            formats.open_any(out, "wrong")
        with formats.open_any(out, "암호Pw1") as a:
            self.assertTrue(a.needs_password())

    def test_password_refused_where_unsupported(self):
        with self.assertRaises(ValueError):
            formats.create("tgz", [self.src], os.path.join(self.root, "p.tar.gz"), password="x")

    def test_tar_traversal_and_links_are_not_extracted(self):
        evil = os.path.join(self.root, "evil.tar")
        with tarfile.open(evil, "w") as tf:
            for name, data in (("../../escape.txt", b"x"), ("/abs.txt", b"y"), ("ok.txt", b"z")):
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
            link = tarfile.TarInfo("link")
            link.type, link.linkname = tarfile.SYMTYPE, "../../../Windows"
            tf.addfile(link)
        dest = os.path.join(self.root, "d", "e")
        with formats.open_any(evil) as a:
            a.extract(dest)
        self.assertFalse(os.path.exists(os.path.join(self.root, "escape.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "d", "escape.txt")))
        self.assertTrue(os.path.exists(os.path.join(dest, "ok.txt")))
        self.assertFalse(os.path.lexists(os.path.join(dest, "link")))

    def test_planner_keeps_7z_names_inside(self):
        plan = formats._Planner(os.path.join(self.root, "p"), True)
        for bad in ("../../x.txt", "..\\..\\x.txt", "/x.txt", "C:/x.txt"):
            t = plan.target(bad, False)
            self.assertTrue(t is None or t.startswith(plan.root), (bad, t))

    def test_rename_mode_and_cancel(self):
        out = self.roundtrip("tgz")
        dest = os.path.join(self.root, "x_tgz")
        with formats.open_any(out) as a:
            a.extract(dest, overwrite="rename")
        self.assertTrue(os.path.exists(os.path.join(dest, "자료", "a (2).txt")))
        with formats.open_any(out) as a:
            with self.assertRaises(ziplib.Cancelled):
                a.extract(os.path.join(self.root, "c"), cancel=lambda: True)
        left = [f for _, _, fs in os.walk(os.path.join(self.root, "c")) for f in fs]
        self.assertEqual(left, [], "cancel must not leave partial files")

    def test_stem_and_with_format(self):
        self.assertEqual(formats.stem(r"C:\a\b.tar.gz"), "b")
        self.assertEqual(formats.stem("b.zip.001"), "b")
        self.assertEqual(formats.with_format(r"C:\a\b.zip", "tgz"), r"C:\a\b.tar.gz")
        self.assertEqual(formats.with_format(r"C:\a\b.tar.gz", "7z"), r"C:\a\b.7z")

    def test_bandizip_interop(self):
        if not os.path.exists(BZ):
            self.skipTest("Bandizip not installed")
        for fid in ("7z", "tar", "tgz", "tbz", "txz"):
            with self.subTest(made_by="yzip", fmt=fid):
                out = self.roundtrip(fid)
                dest = os.path.join(self.root, "bz_" + fid)
                subprocess.run([BZ, "x", "-y", f"-o:{dest}", out], capture_output=True, timeout=120)
                inner = os.path.join(dest, "o.tar")
                if os.path.exists(inner):  # Bandizip's CLI unpacks .tar.bz2/.tar.xz one layer at a time
                    subprocess.run([BZ, "x", "-y", f"-o:{dest}", inner], capture_output=True, timeout=120)
                self.assertEqual(tree(os.path.join(dest, "자료")), self.want)
        for bzfmt, ext in (("7z", ".7z"), ("tgz", ".tgz"), ("tar", ".tar")):
            with self.subTest(made_by="Bandizip", fmt=bzfmt):
                out = os.path.join(self.root, "bz" + ext)
                subprocess.run([BZ, "c", f"-fmt:{bzfmt}", "-y", out, self.src], capture_output=True, timeout=120)
                dest = os.path.join(self.root, "yz_" + bzfmt)
                with formats.open_any(out) as a:
                    self.assertEqual(a.test(), [])
                    a.extract(dest)
                self.assertEqual(tree(os.path.join(dest, "자료")), self.want)
        out = os.path.join(self.root, "bzpw.7z")
        subprocess.run([BZ, "c", "-fmt:7z", "-p:Pw1234", "-y", out, self.src], capture_output=True, timeout=120)
        with formats.open_any(out, "Pw1234") as a:
            a.extract(os.path.join(self.root, "yzpw"))
        self.assertEqual(tree(os.path.join(self.root, "yzpw", "자료")), self.want)


class MultiBlockTests(unittest.TestCase):
    """The multi-threaded LZMA2 paths (lzmamt) with blocks small enough that several are in play."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.src = os.path.join(self.root, "data")
        os.makedirs(os.path.join(self.src, "sub"))
        words = [os.urandom(4).hex().encode() for _ in range(3000)]
        rnd = __import__("random").Random(7)
        for i in range(6):  # members straddle block borders; one is empty, one incompressible
            data = b"" if i == 2 else os.urandom(1_500_000) if i == 4 else \
                b" ".join(rnd.choice(words) for _ in range(400_000 + i * 50_000))
            with open(os.path.join(self.src, "sub" if i % 2 else "", f"f{i}.txt"), "wb") as f:
                f.write(data)
        self.want = tree(self.src)
        self.saved = lzmamt.plan, lzmamt._SEG_CAP
        lzmamt.plan = lambda preset, total: (1 << 20, 4)
        self.fast = []
        self.real_fast = lzmamt.fast_extract
        lzmamt.fast_extract = lambda *a, **k: (self.fast.append(1), self.real_fast(*a, **k))[1]

    def tearDown(self):
        lzmamt.plan, lzmamt._SEG_CAP = self.saved
        lzmamt.fast_extract = self.real_fast
        self.tmp.cleanup()

    def check(self, out, pw=None, names=None):
        dest = os.path.join(self.root, "x" + str(len(os.listdir(self.root))))
        with formats.open_any(out, pw) as a:
            self.assertEqual(a.test(), [])
            a.extract(dest, names)
        return tree(os.path.join(dest, "data"))

    def test_7z_parallel_write_and_read(self):
        for pw in (None, "암호 pw"):
            with self.subTest(pw=pw):
                out = os.path.join(self.root, f"a{bool(pw)}.7z")
                formats.create("7z", [self.src], out, level=5, password=pw)
                self.fast.clear()
                self.assertEqual(self.check(out, pw), self.want)
                self.assertTrue(self.fast, "fast 7z reader was not used")
                one = self.check(out, pw, ["data/sub/f5.txt"])  # solid: earlier members decoded past
                self.assertEqual(one, {"sub/f5.txt": self.want["sub/f5.txt"]})
                if os.path.exists(BZ):
                    r = subprocess.run([BZ, "t", "-y"] + ([f"-p:{pw}"] if pw else []) + [out], capture_output=True)
                    self.assertEqual(r.returncode, 0)

    def test_7z_oversized_segment_streams(self):
        lzmamt._SEG_CAP = 300_000  # smaller than a block: forces the in-order stream path
        out = os.path.join(self.root, "s.7z")
        formats.create("7z", [self.src], out)
        self.assertEqual(self.check(out), self.want)

    def test_7z_damage_is_reported(self):
        out = os.path.join(self.root, "d.7z")
        formats.create("7z", [self.src], out)
        with open(out, "r+b") as f:
            f.seek(os.path.getsize(out) // 2)
            b = f.read(1)
            f.seek(-1, 1)
            f.write(bytes([b[0] ^ 0xFF]))
        with formats.open_any(out) as a:
            self.assertNotEqual(a.test(), [])
            with self.assertRaises(Exception):
                a.extract(os.path.join(self.root, "dmg"))
        left = [f for _, _, fs in os.walk(os.path.join(self.root, "dmg")) for f in fs]
        self.assertNotIn("f5.txt", left)

    def test_7z_wrong_password(self):
        out = os.path.join(self.root, "w.7z")
        formats.create("7z", [self.src], out, password="right")
        with self.assertRaises(ziplib.BadPassword):
            formats.open_any(out, "wrong")

    def test_xz_parallel_write_and_read(self):
        out = os.path.join(self.root, "a.tar.xz")
        formats.create("txz", [self.src], out)
        self.assertIsInstance(lzmamt.open_xz(out), lzmamt.XZReader)
        self.assertEqual(self.check(out), self.want)
        self.assertEqual(self.check(out, names=["data/f4.txt"]), {"f4.txt": self.want["f4.txt"]})
        with tarfile.open(out) as tf:  # the standard library reads it as one ordinary stream
            self.assertEqual(len([m for m in tf if m.isreg()]), 6)
        if os.path.exists(BZ):
            self.assertEqual(subprocess.run([BZ, "t", "-y", out], capture_output=True).returncode, 0)
        single = os.path.join(self.root, "f1.txt.xz")
        formats.create("xz", [os.path.join(self.src, "sub", "f1.txt")], single)
        with formats.open_any(single) as a:
            a.extract(os.path.join(self.root, "sx"))
        with open(os.path.join(self.root, "sx", "f1.txt"), "rb") as g:
            self.assertEqual(sha(g.read()), self.want["sub/f1.txt"])

    def test_bandizip_made_7z_uses_fast_reader(self):
        if not os.path.exists(BZ):
            self.skipTest("Bandizip not installed")
        lzmamt.plan = self.saved[0]
        for pw in (None, "Pw1234"):
            with self.subTest(pw=pw):
                out = os.path.join(self.root, f"bz{bool(pw)}.7z")
                subprocess.run([BZ, "c", "-fmt:7z", "-y"] + ([f"-p:{pw}"] if pw else []) + [out, self.src],
                               capture_output=True, timeout=300)
                self.fast.clear()
                self.assertEqual(self.check(out, pw), self.want)
                self.assertTrue(self.fast, "Bandizip's 7z layout fell back to py7zr")


if __name__ == "__main__":
    unittest.main()
