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


def _raw_names(path):
    with zipfile.ZipFile(path) as z:
        return sorted(ziplib.fix_name(i) for i in z.infolist())


class EditTests(unittest.TestCase):
    """edit_zip copies kept entries byte for byte: names Windows cannot hold, cp949 names, encryption survive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.zip = os.path.join(self.root, "e.zip")
        with zipfile.ZipFile(self.zip, "w", zipfile.ZIP_DEFLATED) as z:
            for n in ("README", "readme", "notes.", "data.txt:meta", "dir/a.txt", "dir/sub/b.txt", "keep.txt"):
                z.writestr(n, f"<{n}>" * 50)
            z.writestr("XXXX.txt", b"korean")
        with open(self.zip, "rb") as f:  # legacy cp949 name without the UTF-8 flag, as old Korean tools write
            data = f.read().replace(b"XXXX.txt", "한글.txt".encode("cp949"))
        with open(self.zip, "wb") as f:
            f.write(data)

    def tearDown(self):
        self.tmp.cleanup()

    def test_delete_keeps_everything_else(self):
        ziplib.edit_zip(self.zip, drop=["dir"])
        self.assertEqual(_raw_names(self.zip), sorted(["README", "readme", "notes.", "data.txt:meta", "keep.txt",
                                                       "한글.txt"]))
        with ziplib.Archive(self.zip) as a:
            self.assertEqual(a.test(), [])
            self.assertEqual(a.zf.read(a._names["한글.txt"]), b"korean")

    def test_rename_file_and_folder(self):
        ziplib.edit_zip(self.zip, renames={"keep.txt": "kept.txt", "dir/": "folder"})
        names = _raw_names(self.zip)
        self.assertIn("kept.txt", names)
        self.assertIn("folder/sub/b.txt", names)
        self.assertNotIn("dir/a.txt", names)
        with ziplib.Archive(self.zip) as a:
            self.assertEqual(a.test(), [])

    def test_add_replaces_same_name_and_goes_into_folder(self):
        f = os.path.join(self.root, "keep.txt")
        with open(f, "wb") as fh:
            fh.write(b"new keep")
        ziplib.edit_zip(self.zip, add=[f])
        ziplib.edit_zip(self.zip, add=[f], into="dir/sub/")
        names = _raw_names(self.zip)
        self.assertEqual(names.count("keep.txt"), 1)
        self.assertIn("dir/sub/keep.txt", names)
        self.assertIn("data.txt:meta", names)
        with ziplib.Archive(self.zip) as a:
            self.assertEqual(a.zf.read(a._names["keep.txt"]), b"new keep")
            self.assertEqual(a.test(), [])

    def test_encrypted_entries_survive_an_edit(self):
        src = os.path.join(self.root, "s")
        os.makedirs(src)
        for n in ("x.bin", "y.bin"):
            with open(os.path.join(src, n), "wb") as fh:
                fh.write(os.urandom(200_000))
        z = os.path.join(self.root, "aes.zip")
        ziplib.create_zip([src], z, password="pw")
        ziplib.edit_zip(z, drop=["s/x.bin"])
        with ziplib.Archive(z, "pw") as a:
            self.assertEqual(a.test(), [])
            self.assertEqual(sorted(a._names), ["s/", "s/y.bin"])
        with ziplib.Archive(z, "wrong") as a:
            with self.assertRaises(ziplib.BadPassword):
                a.extract(os.path.join(self.root, "w"))

    def test_adding_to_an_encrypted_zip_needs_its_password(self):
        src = os.path.join(self.root, "s.txt")
        with open(src, "wb") as fh:
            fh.write(b"s" * 5000)
        z = os.path.join(self.root, "aes.zip")
        ziplib.create_zip([src], z, password="pw")
        new = os.path.join(self.root, "n.txt")
        with open(new, "wb") as fh:
            fh.write(b"n")
        for pw in (None, "typo"):
            with self.assertRaises(ziplib.BadPassword):
                ziplib.edit_zip(z, add=[new], password=pw)
        ziplib.edit_zip(z, add=[new], password="pw")
        with zipfile.ZipFile(z) as zf:
            self.assertTrue(all(i.flag_bits & 1 for i in zf.infolist()))

    def test_rename_onto_an_existing_name_is_refused(self):
        with self.assertRaises(ValueError):
            ziplib.edit_zip(self.zip, renames={"keep.txt": "README"})
        with self.assertRaises(ValueError):
            ziplib.edit_zip(self.zip, renames={"keep.txt": "dir"})
        self.assertIn("keep.txt", _raw_names(self.zip))

    def test_split_archive_is_refused(self):
        with self.assertRaises(ValueError):
            ziplib.edit_zip(self.zip + ".001", drop=["x"])


class ExtractSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_hostile_names_stay_inside_and_do_not_abort(self):
        z = os.path.join(self.root, "h.zip")
        names = ["../up.txt", "C:/abs.txt", "D:/evil.txt", "q:x.txt", "a.txt:ads", "CON", "nul.txt", "trail.",
                 "sp ", "...", "a<b>.txt", "x?.txt", "ok.txt"]
        with zipfile.ZipFile(z, "w") as zf:
            for n in names:
                zf.writestr(n, n.encode())
        dest = os.path.join(self.root, "d")
        os.makedirs(dest)
        with open(os.path.join(dest, "a.txt"), "wb") as fh:
            fh.write(b"mine")
        with ziplib.Archive(z) as a:
            a.extract(dest)
        written = {os.path.relpath(os.path.join(dp, f), dest) for dp, _, fs in os.walk(dest) for f in fs}
        self.assertFalse(os.path.exists(os.path.join(self.root, "up.txt")))
        self.assertIn("ok.txt", written)
        self.assertIn("a.txt_ads", written)
        self.assertIn("_CON", written)
        with open(os.path.join(dest, "a.txt"), "rb") as fh:
            self.assertEqual(fh.read(), b"mine")
        with self.assertRaises(FileNotFoundError):  # no alternate data stream written onto the user's file
            open(os.path.join(dest, "a.txt") + ":ads", "rb").close()

    def test_wrong_password_keeps_existing_file(self):
        src = os.path.join(self.root, "report.txt")
        with open(src, "wb") as fh:
            fh.write(b"secret v2")
        z = os.path.join(self.root, "p.zip")
        ziplib.create_zip([src], z, password="right")
        dest = os.path.join(self.root, "out")
        os.makedirs(dest)
        with open(os.path.join(dest, "report.txt"), "wb") as fh:
            fh.write(b"my v1")
        with ziplib.Archive(z, "wrong") as a:
            with self.assertRaises(ziplib.BadPassword):
                a.extract(dest)
        with open(os.path.join(dest, "report.txt"), "rb") as fh:
            self.assertEqual(fh.read(), b"my v1")
        self.assertEqual(os.listdir(dest), ["report.txt"])

    def test_declared_size_is_enforced(self):
        z = os.path.join(self.root, "bomb.zip")
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("big.bin", b"\0" * (20 << 20))
        with zipfile.ZipFile(z) as zf:
            info = zf.getinfo("big.bin")
        with open(z, "r+b") as fh:  # central directory: uncompressed size -> 10
            data = fh.read()
            cd = data.rfind(b"PK\x01\x02")
            fh.seek(cd + 24)
            fh.write((10).to_bytes(4, "little"))
        dest = os.path.join(self.root, "b")
        with ziplib.Archive(z) as a:
            with self.assertRaises(zipfile.BadZipFile):
                a.extract(dest)
        self.assertFalse(os.path.exists(os.path.join(dest, "big.bin")))
        self.assertEqual(info.file_size, 20 << 20)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        import contextlib, io, yzip_cli
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return yzip_cli.main(list(args))

    def test_failed_create_keeps_the_old_archive(self):
        z = os.path.join(self.root, "backup.zip")
        with open(z, "wb") as fh:
            fh.write(b"old archive")
        self.assertNotEqual(self.cli("c", z, os.path.join(self.root, "typo")), 0)
        with open(z, "rb") as fh:
            self.assertEqual(fh.read(), b"old archive")

    def test_flat_extract_never_moves_outside_files(self):
        victim = os.path.join(self.root, "victim.txt")
        with open(victim, "wb") as fh:
            fh.write(b"keep me")
        rel = os.path.relpath(victim, tempfile.gettempdir()).replace("\\", "/")
        z = os.path.join(self.root, "evil.zip")
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("../" + rel, b"payload")  # from the temp folder 'e' extracts into, this is the victim
            zf.writestr("a/b/ok.txt", b"ok")
        out = os.path.join(self.root, "o")
        self.assertEqual(self.cli("e", z, "-o", out), 0)
        with open(victim, "rb") as fh:
            self.assertEqual(fh.read(), b"keep me")
        with open(os.path.join(out, "ok.txt"), "rb") as fh:
            self.assertEqual(fh.read(), b"ok")

    def test_delete_and_add_on_an_encrypted_zip(self):
        src = os.path.join(self.root, "s")
        os.makedirs(src)
        for n in ("a.txt", "b.txt"):
            with open(os.path.join(src, n), "wb") as fh:
                fh.write(n.encode() * 1000)
        z = os.path.join(self.root, "p.zip")
        self.assertEqual(self.cli("c", z, src, "-p", "pw"), 0)
        self.assertEqual(self.cli("d", z, "s/a.txt"), 0)
        extra = os.path.join(self.root, "c.txt")
        with open(extra, "wb") as fh:
            fh.write(b"c")
        self.assertEqual(self.cli("a", z, extra, "-p", "pw"), 0)
        with ziplib.Archive(z, "pw") as a:
            self.assertEqual(sorted(a._names), ["c.txt", "s/", "s/b.txt"])
            self.assertEqual(a.test(), [])


class SplitTests(unittest.TestCase):
    def test_resplit_removes_stale_volumes(self):
        with tempfile.TemporaryDirectory() as root:
            f = os.path.join(root, "rnd.bin")
            with open(f, "wb") as fh:
                fh.write(os.urandom(400_000))
            out = os.path.join(root, "s.zip")
            self.assertEqual(len(ziplib.create_zip([f], out, store_only=True, split_size=100_000)), 5)
            with open(f, "wb") as fh:
                fh.write(os.urandom(150_000))
            self.assertEqual(len(ziplib.create_zip([f], out, store_only=True, split_size=100_000)), 2)
            self.assertFalse(os.path.exists(out + ".003"))
            with ziplib.Archive(out + ".001") as a:
                self.assertEqual(a.test(), [])


if __name__ == "__main__":
    unittest.main()
