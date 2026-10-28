"""Formats read through the bundled native/compatibility readers and the Python decoders (minifmt).

EGG / ALZ use the CC0 samples in testdata\\; CAB and ISO are made with Windows' own makecab / IMAPI2.
The Python formats are built here by small encoders and round-tripped."""
import base64
import binascii
import hashlib
import hmac
import json
import lzma
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import formats  # noqa: E402
from ziplib import BadPassword, Cancelled  # noqa: E402

DATA = os.path.join(HERE, "testdata")


def tree(d):
    out = {}
    for dp, _, fs in os.walk(d):
        for f in fs:
            p = os.path.join(dp, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, d).replace("\\", "/")] = hashlib.sha256(fh.read()).hexdigest()
    return out


class Tmp(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)


class NativeFormatTests(Tmp):
    def roundtrip(self, name, pw=None):
        path = os.path.join(DATA, name)
        with formats.open_any(path, pw) as a:
            self.assertTrue(a.entries())
            seen = []
            a.extract(self.t, progress=lambda d, t, n: seen.append((d, t)))
            self.assertEqual(a.test(), [])
        got = tree(self.t)
        self.assertTrue(got)
        self.assertEqual(seen[-1][0], seen[-1][1], "progress ends at 100%")
        self.assertFalse([f for f in os.listdir(self.t) if f.startswith("~yzip")], "staging folder removed")

    def test_egg_samples(self):
        for n in ("normal.egg", "azo.egg", "solid.egg", "lzma5.egg"):
            with self.subTest(n):
                self.roundtrip(n)
                shutil.rmtree(self.t)
                os.makedirs(self.t)

    def test_alz_samples(self):
        for n in ("normal.alz", "bzip2.alz", "dir_tree.alz"):
            with self.subTest(n):
                self.roundtrip(n)
                shutil.rmtree(self.t)
                os.makedirs(self.t)

    def test_split_sets_open_from_any_volume(self):
        self.roundtrip("split_store.vol2.egg")
        shutil.rmtree(self.t)
        os.makedirs(self.t)
        self.roundtrip("split_store.a01")

    def test_encrypted(self):
        for n in ("enc_aes256.egg", "enc_lea_bzip2.egg", "enc_deflate.alz", "zip20.alz"):
            with self.subTest(n):
                p = os.path.join(DATA, n)
                with formats.open_any(p) as a:
                    self.assertTrue(a.needs_password())
                    with self.assertRaises(BadPassword):  # no password: an error, never a console prompt (hang)
                        a.extract(self.t)
                with formats.open_any(p, "wrong") as a:
                    with self.assertRaises(BadPassword):
                        a.extract(self.t)
                    with self.assertRaises(BadPassword):
                        a.test()
                self.assertEqual(tree(self.t), {}, "nothing written by a failed run")
                shutil.rmtree(self.t)
                os.makedirs(self.t)
                self.roundtrip(n, "test1234")
                shutil.rmtree(self.t)
                os.makedirs(self.t)

    def test_selected_names_only(self):
        with formats.open_any(os.path.join(DATA, "dir_tree.alz")) as a:
            files = [e.name for e in a.entries() if not e.is_dir]
            a.extract(self.t, names=[files[0]])
        self.assertEqual(list(tree(self.t)), [files[0]])

    def test_damaged_egg_is_reported_and_writes_nothing(self):
        src = os.path.join(DATA, "normal.egg")
        bad = os.path.join(self.t, "bad.egg")
        data = bytearray(open(src, "rb").read())
        data[len(data) // 2] ^= 0xFF
        open(bad, "wb").write(bytes(data))
        out = os.path.join(self.t, "out")
        try:
            with formats.open_any(bad) as a:
                self.assertTrue(a.test())
                with self.assertRaises(ValueError):
                    a.extract(out)
        except ValueError:
            pass  # or already refused when listing
        self.assertEqual(tree(out) if os.path.exists(out) else {}, {})

    def make_cab(self):
        src = os.path.join(self.t, "data.bin")
        with open(src, "wb") as f:
            f.write(os.urandom(200_000) + b"x" * 300_000)
        subprocess.run(["makecab", src, os.path.join(self.t, "x.cab")], capture_output=True, check=True)
        return src, os.path.join(self.t, "x.cab")

    def test_cab_overwrite_modes_and_cancel(self):
        src, cab = self.make_cab()
        self.assertEqual(formats.detect(cab), "cab")
        out = os.path.join(self.t, "out")
        with formats.open_any(cab) as a:
            self.assertEqual([e.name for e in a.entries()], ["data.bin"])
            a.extract(out)
            a.extract(out, overwrite="rename")
            a.extract(out, overwrite=False)
            with self.assertRaises(Cancelled):
                a.extract(os.path.join(self.t, "c"), progress=lambda *x: None, cancel=lambda: True)
        self.assertEqual(sorted(os.listdir(out)), ["data (2).bin", "data.bin"])
        self.assertEqual(open(os.path.join(out, "data.bin"), "rb").read(), open(src, "rb").read())
        self.assertEqual(tree(os.path.join(self.t, "c")) if os.path.exists(os.path.join(self.t, "c")) else {}, {})

    def test_iso_made_by_windows(self):
        src = os.path.join(self.t, "src")
        os.makedirs(os.path.join(src, "하위"))
        with open(os.path.join(src, "하위", "a.txt"), "w", encoding="utf-8") as f:
            f.write("ISO 안의 파일 " * 100)
        iso = os.path.join(self.t, "disc.iso")
        script = os.path.join(self.t, "mkiso.ps1")
        with open(script, "w", encoding="utf-8-sig") as f:  # IMAPI2 image stream copied through IStream
            f.write("param($src, $iso)\n"
                    "Add-Type -TypeDefinition 'public static class IsoW { public static void Save(object o, string p) {"
                    " var s = (System.Runtime.InteropServices.ComTypes.IStream)o; var b = new byte[65536];"
                    " var n = System.Runtime.InteropServices.Marshal.AllocHGlobal(4);"
                    " using (var f = System.IO.File.Create(p)) { while (true) { s.Read(b, b.Length, n);"
                    " int k = System.Runtime.InteropServices.Marshal.ReadInt32(n); if (k <= 0) break; f.Write(b, 0, k); } }"
                    " System.Runtime.InteropServices.Marshal.FreeHGlobal(n); } }'\n"
                    "$fs = New-Object -ComObject IMAPI2FS.MsftFileSystemImage\n"
                    "$fs.FileSystemsToCreate = 3\n$fs.VolumeName = 'T'\n$fs.Root.AddTree($src, $false)\n"
                    "[IsoW]::Save($fs.CreateResultImage().ImageStream, $iso)\n")
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, src, iso],
                           capture_output=True, timeout=120)
        if not os.path.exists(iso) or os.path.getsize(iso) == 0:
            self.skipTest("IMAPI2 not available: " + r.stderr.decode("utf-8", "replace")[-200:])
        with formats.open_any(iso) as a:
            names = [e.name for e in a.entries()]
            a.extract(os.path.join(self.t, "out"))
        self.assertIn("하위/a.txt", names)
        self.assertEqual(open(os.path.join(self.t, "out", "하위", "a.txt"), encoding="utf-8").read(), "ISO 안의 파일 " * 100)

    def test_read_only_member_leaves_no_staging_folder(self):
        import zipfile
        p = os.path.join(self.t, "ro.zipx")  # .zipx is read through 7-Zip, which restores the R attribute
        with zipfile.ZipFile(p, "w") as z:
            info = zipfile.ZipInfo("locked.txt")
            info.create_system, info.external_attr = 0, 0x01  # MS-DOS read-only
            z.writestr(info, b"read only")
        out = os.path.join(self.t, "out")
        with formats.open_any(p) as a:
            a.extract(out)
            a.extract(out, overwrite=False)  # skipped: the staged read-only copy has to be deleted
            a.extract(out, overwrite=True)
        self.assertEqual(os.listdir(out), ["locked.txt"])

    def test_member_names_cannot_become_tool_options(self):
        with mock.patch("subprocess.Popen", side_effect=AssertionError("native archive spawned a process")):
            with formats.open_any(os.path.join(DATA, "normal.alz")) as archive:
                out = os.path.join(self.t, "out")
                archive.extract(out, names=["--output-dir=elsewhere", "-p"])
        self.assertEqual(tree(out), {})

    def test_exe_and_docx_are_not_archives(self):
        p = os.path.join(self.t, "a.exe")
        shutil.copy(sys.executable, p)
        with self.assertRaises(ValueError):
            formats.detect(p)

    def test_zpaq_roundtrip_through_yzip_backend(self):
        tool = os.path.join(HERE, "tools", "zpaq.exe")
        if not os.path.exists(tool):
            self.skipTest("bundled zpaq.exe not present")
        payload = os.path.join(self.t, "payload.txt")
        with open(payload, "wb") as f:
            f.write(b"zpaq payload\n")
        archive = os.path.join(self.t, "sample.zpaq")
        subprocess.run([tool, "add", archive, "payload.txt"], cwd=self.t, check=True,
                       capture_output=True, text=True)
        with formats.open_any(archive) as a:
            self.assertEqual([e.name for e in a.entries()], ["payload.txt"])
            self.assertEqual(a.test(), [])
            out = os.path.join(self.t, "out")
            a.extract(out)
        with open(os.path.join(out, "payload.txt"), "rb") as f:
            self.assertEqual(f.read(), b"zpaq payload\n")

    def test_7z_sfx_exe_is_openable_but_plain_exe_is_not(self):
        tool = os.path.join(HERE, "tools", "7z.exe")
        if not os.path.exists(tool):
            self.skipTest("bundled 7z.exe not present")
        payload = os.path.join(self.t, "payload.txt")
        with open(payload, "wb") as f:
            f.write(b"sfx payload")
        base = os.path.join(self.t, "base.7z")
        subprocess.run([tool, "a", base, "payload.txt"], cwd=self.t, check=True,
                       capture_output=True)
        exe = os.path.join(self.t, "archive.exe")
        with open(base, "rb") as source, open(exe, "wb") as f:
            f.write(b"MZ\0\0" + source.read())
        self.assertEqual(formats.detect(exe), "7zcli")
        with formats.open_any(exe) as a:
            self.assertEqual([e.name for e in a.entries()], ["payload.txt"])


# ---------- Python-decoded formats ----------

def lz4_compress(data: bytes) -> bytes:
    """A small greedy LZ4 block compressor, enough to produce real matches (including overlapping ones)."""
    out, i, anchor, table, n = bytearray(), 0, 0, {}, len(data)

    def emit(lit: bytes, off=None, ml=0):
        tok_l = min(len(lit), 15)
        tok_m = min(ml - 4, 15) if off else 0
        out.append(tok_l << 4 | tok_m)
        if len(lit) >= 15:
            r = len(lit) - 15
            while r >= 255:
                out.append(255)
                r -= 255
            out.append(r)
        out.extend(lit)
        if off:
            out.extend(struct.pack("<H", off))
            if ml - 4 >= 15:
                r = ml - 4 - 15
                while r >= 255:
                    out.append(255)
                    r -= 255
                out.append(r)
    while i + 12 < n:
        key = data[i:i + 4]
        j = table.get(key)
        table[key] = i
        if j is not None and i - j < 65536:
            ml = 4
            while i + ml < n - 5 and data[j + ml] == data[i + ml]:
                ml += 1
            emit(data[anchor:i], i - j, ml)
            i += ml
            anchor = i
        else:
            i += 1
    emit(data[anchor:])
    return bytes(out)


def lz4_frame(data: bytes) -> bytes:
    blocks = b""
    for k in range(0, len(data), 1 << 16):
        c = lz4_compress(data[k:k + (1 << 16)])
        blocks += struct.pack("<I", len(c)) + c
    raw_tail = b"raw block at the end"
    blocks += struct.pack("<I", len(raw_tail) | 0x80000000) + raw_tail
    return struct.pack("<I", 0x184D2204) + bytes([0x60, 0x40, 0x82]) + blocks + struct.pack("<I", 0), data + raw_tail


def lzip(data: bytes) -> bytes:
    alone = lzma.compress(data, format=lzma.FORMAT_ALONE, preset=6)  # lc3 lp0 pb2, end marker, 8 MiB dict
    member = b"LZIP\x01\x17" + alone[13:]  # 0x17: 8 MiB
    return member + struct.pack("<IQQ", zlib.crc32(data), len(data), len(member) + 20)


def aescrypt(data: bytes, pw: str) -> bytes:
    from Cryptodome.Cipher import AES
    iv1, iv2, key2 = os.urandom(16), os.urandom(16), os.urandom(32)
    key = iv1 + bytes(16)
    for _ in range(8192):
        key = hashlib.sha256(key + pw.encode("utf-16-le")).digest()
    enc = AES.new(key, AES.MODE_CBC, iv1).encrypt(iv2 + key2)
    tail = len(data) % 16
    padded = data + bytes([16 - tail]) * (16 - tail) if tail else data
    ct = AES.new(key2, AES.MODE_CBC, iv2).encrypt(padded)
    return (b"AES\x02\x00" + struct.pack(">H", 0) + iv1 + enc + hmac.new(key, enc, hashlib.sha256).digest() + ct
            + bytes([tail]) + hmac.new(key2, ct, hashlib.sha256).digest())


class PythonFormatTests(Tmp):
    payload = os.urandom(50_000) + b"abcabcabc" * 20_000 + "한글".encode() * 3000

    def one(self, name, blob, want, pw=None):
        p = os.path.join(self.t, name)
        with open(p, "wb") as f:
            f.write(blob)
        out = os.path.join(self.t, "out")
        with formats.open_any(p, pw) as a:
            self.assertEqual(a.test(), [])
            a.extract(out)
        stem = formats.stem(p)
        self.assertEqual(open(os.path.join(out, stem), "rb").read(), want)
        return p

    def test_brotli(self):
        import brotli
        self.one("a.bin.br", brotli.compress(self.payload), self.payload)

    def test_lzip_two_members_and_crc_check(self):
        p = self.one("a.bin.lz", lzip(self.payload) + lzip(b"second member"), self.payload + b"second member")
        data = bytearray(open(p, "rb").read())
        data[-25] ^= 1  # inside the first member's trailer area of the second: CRC / size no longer match
        with open(p, "wb") as f:
            f.write(bytes(data))
        with formats.open_any(p) as a:
            self.assertTrue(a.test())

    def test_lz4_frame_with_matches_and_raw_block(self):
        blob, want = lz4_frame(self.payload)
        self.one("a.bin.lz4", blob, want)

    def test_aescrypt(self):
        blob = aescrypt(self.payload, "비밀번호")
        p = self.one("a.bin.aes", blob, self.payload, "비밀번호")
        with formats.open_any(p) as a:
            self.assertTrue(a.needs_password())
        with formats.open_any(p, "wrong") as a:
            with self.assertRaises(BadPassword):
                a.extract(os.path.join(self.t, "x"))
        for n in (0, 16, 17):  # sizes that are / are not a multiple of 16
            self.one(f"s{n}.aes", aescrypt(b"z" * n, "pw"), b"z" * n, "pw")

    def test_uu_xx_base64_several_files(self):
        a, b, c = os.urandom(1000), b"hello\n" * 50, os.urandom(77)

        def uu(name, d):
            return f"begin 644 {name}\n" + "".join(binascii.b2a_uu(d[k:k + 45]).decode() for k in range(0, len(d), 45)) + "`\nend\n"
        xx_alpha = "+-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        xx = uu("x.bin", c).replace("begin 644 x.bin\n", "").replace("`\nend\n", "")
        xx = "".join(xx_alpha[(ord(ch) - 32) & 63] if ch != "\n" else "\n" for ch in xx)
        text = ("header text\n" + uu("a.bin", a) + "\n" + uu("b.txt", b)
                + "begin-base64 644 c.bin\n" + base64.encodebytes(c).decode() + "====\n")
        p = os.path.join(self.t, "m.uue")
        with open(p, "w", newline="\n") as f:
            f.write(text)
        with formats.open_any(p) as arc:
            self.assertEqual([e.name for e in arc.entries()], ["a.bin", "b.txt", "c.bin"])
            arc.extract(os.path.join(self.t, "out"))
        for n, d in (("a.bin", a), ("b.txt", b), ("c.bin", c)):
            self.assertEqual(open(os.path.join(self.t, "out", n), "rb").read(), d)
        px = os.path.join(self.t, "x.xxe")
        with open(px, "w", newline="\n") as f:
            f.write("begin 644 x.bin\n" + xx + "+\nend\n")
        with formats.open_any(px) as arc:
            arc.extract(os.path.join(self.t, "outx"))
        self.assertEqual(open(os.path.join(self.t, "outx", "x.bin"), "rb").read(), c)

    def test_asar(self):
        files = {"main.js": b"console.log(1)", "lib/util.js": b"x" * 5000, "lib/deep/d.txt": "한글".encode()}
        tree_ = {"files": {}}
        blob, off = b"", 0
        for name, d in files.items():
            node = tree_
            parts = name.split("/")
            for part in parts[:-1]:
                node = node["files"].setdefault(part, {"files": {}})
            node["files"][parts[-1]] = {"size": len(d), "offset": str(off)}
            blob += d
            off += len(d)
        node = tree_["files"].setdefault("../evil.txt", {"size": 1, "offset": "0"})  # hostile name
        js = json.dumps(tree_).encode()
        pad = (4 - len(js) % 4) % 4
        pickle = struct.pack("<II", len(js) + pad + 4, len(js)) + js + b"\0" * pad
        p = os.path.join(self.t, "app.asar")
        with open(p, "wb") as f:
            f.write(struct.pack("<II", 4, len(pickle)) + pickle + blob)
        with formats.open_any(p) as a:
            a.extract(os.path.join(self.t, "out"))
        got = tree(os.path.join(self.t, "out"))
        for name, d in files.items():
            self.assertEqual(got[name], hashlib.sha256(d).hexdigest())
        self.assertFalse(os.path.exists(os.path.join(self.t, "evil.txt")), "no escape from dest")

    def test_truncated_and_bomb_inputs_are_clean_errors(self):
        import brotli
        cases = {"t.lz4": lz4_frame(self.payload)[0][:30], "t.lz": lzip(self.payload)[:40],
                 "t.aes": aescrypt(b"x" * 100, "pw")[:120]}
        for n, blob in cases.items():
            p = os.path.join(self.t, n)
            with open(p, "wb") as f:
                f.write(blob)
            with formats.open_any(p, "pw") as a:
                self.assertTrue(a.test(), n)
                with self.assertRaises((ValueError, BadPassword), msg=n):
                    a.extract(os.path.join(self.t, "o_" + n))
        bomb = os.path.join(self.t, "bomb.lz")  # 64 MiB of zeros in a few KiB: decoded in bounded steps
        with open(bomb, "wb") as f:
            f.write(lzip(bytes(64 << 20)))
        with formats.open_any(bomb) as a:
            self.assertEqual(a.test(), [])
        self.assertTrue(brotli)

    def test_volume_names(self):
        self.assertEqual(formats.stem("x.part01.rar"), "x")
        self.assertEqual(formats.stem("x.vol1.egg"), "x")
        open(os.path.join(self.t, "s.part01.rar"), "w").close()
        self.assertEqual(os.path.basename(formats.first_volume(os.path.join(self.t, "s.part07.rar"))), "s.part01.rar")


if __name__ == "__main__":
    unittest.main()
