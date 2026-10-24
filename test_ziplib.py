import os
import tempfile
import unittest
import zipfile

import ziplib


def make_tree(root):
    os.makedirs(os.path.join(root, "src", "한글폴더"))
    os.makedirs(os.path.join(root, "src", "empty"))
    with open(os.path.join(root, "src", "a.txt"), "w", encoding="utf-8") as f:
        f.write("hello" * 1000)
    with open(os.path.join(root, "src", "한글폴더", "파일.txt"), "w", encoding="utf-8") as f:
        f.write("안녕")
    with open(os.path.join(root, "src", "big.bin"), "wb") as f:
        f.write(os.urandom(300_000))
    return os.path.join(root, "src")


class ZipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.src = make_tree(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _roundtrip(self, **kw):
        out = os.path.join(self.root, "o.zip")
        pw = kw.get("password")
        parts = ziplib.create_zip([self.src], out, **kw)
        dest = os.path.join(self.root, "x")
        with ziplib.Archive(parts[0], pw) as a:
            self.assertEqual(a.test(), [])
            a.extract(dest)
        for rel in ("a.txt", os.path.join("한글폴더", "파일.txt"), "big.bin"):
            with open(os.path.join(self.src, rel), "rb") as f1, open(os.path.join(dest, "src", rel), "rb") as f2:
                self.assertEqual(f1.read(), f2.read(), rel)
        self.assertTrue(os.path.isdir(os.path.join(dest, "src", "empty")))
        return parts

    def test_plain(self):
        self._roundtrip()

    def test_store(self):
        self._roundtrip(store_only=True)

    def test_aes(self):
        self._roundtrip(password="pw한글1")

    def test_cp949_password_from_bandizip(self):
        import pyzipper
        out = os.path.join(self.root, "bz.zip")
        with pyzipper.AESZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as zf:
            zf.setpassword("암호1".encode("cp949"))
            zf.writestr("f.txt", b"data")
        with ziplib.Archive(out, "암호1") as a:
            self.assertEqual(a.test(), [])
        with ziplib.Archive(out, "틀림") as a:
            with self.assertRaises(ziplib.BadPassword):
                a.test()

    def test_wrong_password(self):
        out = os.path.join(self.root, "o.zip")
        ziplib.create_zip([self.src], out, password="right")
        with ziplib.Archive(out, "wrong") as a:
            with self.assertRaises(ziplib.BadPassword):
                a.extract(os.path.join(self.root, "y"))

    def test_split(self):
        parts = self._roundtrip(split_size=100_000)
        self.assertGreater(len(parts), 1)

    def test_zipstd_python_can_read(self):
        out = os.path.join(self.root, "o.zip")
        ziplib.create_zip([self.src], out)
        with zipfile.ZipFile(out) as z:
            self.assertIsNone(z.testzip())
            self.assertIn("src/한글폴더/파일.txt", z.namelist())

    def test_cp949_names(self):
        out = os.path.join(self.root, "k.zip")
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("ABCD.txt", b"x")
        with open(out, "rb") as f:
            data = f.read().replace(b"ABCD", "한글".encode("cp949"))  # no UTF-8 flag
        with open(out, "wb") as f:
            f.write(data)
        with ziplib.Archive(out) as a:
            self.assertEqual([e.name for e in a.entries()], ["한글.txt"])

    def test_zip_slip(self):
        out = os.path.join(self.root, "evil.zip")
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("../../evil.txt", b"x")
            z.writestr("ok.txt", b"y")
        dest = os.path.join(self.root, "d", "e")
        with ziplib.Archive(out) as a:
            a.extract(dest)
        self.assertFalse(os.path.exists(os.path.join(self.root, "evil.txt")))
        self.assertTrue(os.path.exists(os.path.join(dest, "evil.txt")) or os.path.exists(os.path.join(dest, "ok.txt")))

    def test_corrupt_detected(self):
        out = os.path.join(self.root, "o.zip")
        ziplib.create_zip([self.src], out, store_only=True)
        with open(out, "rb") as f:
            data = bytearray(f.read())
        data[200] ^= 0xFF
        with open(out, "wb") as f:
            f.write(data)
        with ziplib.Archive(out) as a:
            self.assertTrue(a.test())

    def test_parallel_extract_many_files_and_cancel(self):
        many = os.path.join(self.root, "many")
        os.makedirs(many)
        for i in range(60):
            with open(os.path.join(many, f"f{i}.bin"), "wb") as f:
                f.write(os.urandom(300_000))
        z = os.path.join(self.root, "many.zip")
        ziplib.create_zip([many], z)
        out = os.path.join(self.root, "m_out")
        with ziplib.Archive(z) as a:
            a.extract(out)
        for i in range(60):
            with open(os.path.join(many, f"f{i}.bin"), "rb") as f1, open(os.path.join(out, "many", f"f{i}.bin"), "rb") as f2:
                self.assertEqual(f1.read(), f2.read())
        out2 = os.path.join(self.root, "m_out2")
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] > 20
        with ziplib.Archive(z) as a:
            with self.assertRaises(ziplib.Cancelled):
                a.extract(out2, cancel=cancel)
        sizes = [os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(out2) for f in fs]
        self.assertTrue(all(s == 300_000 for s in sizes), "cancel must not leave partial files")

    def test_extract_corrupt_member_is_not_a_password_error(self):
        out = os.path.join(self.root, "c.zip")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
            z.writestr("BadApple.txt", b"A" * 5000)
        with open(out, "rb") as f:
            data = bytearray(f.read())
        data[100] ^= 0xFF  # inside the stored data
        with open(out, "wb") as f:
            f.write(data)
        dest = os.path.join(self.root, "cx")
        with ziplib.Archive(out) as a:
            with self.assertRaises(zipfile.BadZipFile):
                a.extract(dest)
        self.assertFalse(os.path.exists(os.path.join(dest, "BadApple.txt")), "partial file left behind")

    def test_extract_rename_mode(self):
        out = os.path.join(self.root, "r.zip")
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("a.txt", b"new a")
            z.writestr("a (2).txt", b"new a2")  # would collide with the renamed a.txt
            z.writestr("d/b.txt", b"new b")
        dest = os.path.join(self.root, "rd")
        os.makedirs(os.path.join(dest, "d"))
        for rel, data in (("a.txt", b"old a"), (os.path.join("d", "b.txt"), b"old b")):
            with open(os.path.join(dest, rel), "wb") as f:
                f.write(data)
        with ziplib.Archive(out) as a:
            a.extract(dest, overwrite="rename")
        got = {os.path.relpath(os.path.join(dp, f), dest).replace("\\", "/"): open(os.path.join(dp, f), "rb").read()
               for dp, _, fs in os.walk(dest) for f in fs}
        self.assertEqual(got["a.txt"], b"old a")
        self.assertEqual(got["d/b.txt"], b"old b")
        self.assertEqual(got["d/b (2).txt"], b"new b")
        self.assertEqual(sorted(v for k, v in got.items() if k.startswith("a")), [b"new a", b"new a2", b"old a"])
        self.assertEqual(len(got), 5)

    def test_cancel(self):
        out = os.path.join(self.root, "c.zip")
        with self.assertRaises(ziplib.Cancelled):
            ziplib.create_zip([self.src], out, cancel=lambda: True)
        self.assertFalse(os.path.exists(out))
        self.assertFalse(os.path.exists(out + ".partial"))


if __name__ == "__main__":
    unittest.main()
