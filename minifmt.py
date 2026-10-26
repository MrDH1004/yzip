"""Smaller formats Bandizip reads that neither 7-Zip nor the stdlib cover, decoded here in Python:

streams (one file):   .br (Brotli), .lz (lzip), .lz4 (LZ4 frame / legacy), .aes (AES Crypt, needs the password)
archives:             .uu / .uue / .xxe (uuencode, xxencode, base64 'begin-base64'), .asar (Electron)

Streams plug into formats.StreamArchive through open_stream(); the archives have the ziplib.Archive API."""
from __future__ import annotations

import binascii
import hashlib
import hmac
import json
import lzma
import os
import struct
import zlib
from typing import Iterator, Optional

import ziplib
from ziplib import BadPassword, Entry

STREAM_EXTS = {".br": "br", ".lz": "lz", ".lz4": "lz4", ".aes": "aes"}
ARCHIVE_EXTS = {".uu": "uu", ".uue": "uu", ".xxe": "uu", ".asar": "asar"}
CHUNK = 1 << 20


class _ChunkReader:
    """read(n) over a generator of byte chunks."""

    def __init__(self, chunks: Iterator[bytes], closer=None):
        self.it, self.buf, self.closer = chunks, bytearray(), closer

    def read(self, n=-1) -> bytes:
        while n < 0 or len(self.buf) < n:
            try:
                self.buf += next(self.it)
            except StopIteration:
                break
            except (struct.error, IndexError, EOFError) as e:  # a header cut short
                raise ValueError("손상되었거나 잘린 파일입니다") from e
        if n < 0:
            n = len(self.buf)
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def close(self):
        if self.closer:
            self.closer()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class _Buffered:
    """A file with pushback, for formats whose decoder stops in the middle of what was read."""

    def __init__(self, f):
        self.f, self.extra = f, b""

    def read(self, n: int) -> bytes:
        out = self.extra[:n]
        self.extra = self.extra[n:]
        if len(out) < n:
            out += self.f.read(n - len(out))
        return out

    def unread(self, b: bytes):
        self.extra = b + self.extra


# ---------- Brotli ----------

def _brotli(f) -> Iterator[bytes]:
    import brotli
    d = brotli.Decompressor()
    while buf := f.read(4096):  # small input steps: this brotli has no output limit per call
        yield d.process(buf)
    if not d.is_finished():
        raise ValueError("Brotli: 데이터가 잘렸습니다")


# ---------- lzip ----------

def _lzip(f) -> Iterator[bytes]:
    """Members of 'LZIP' + version + coded dictionary size, raw LZMA (lc3 lp0 pb2, end marker), then
    CRC32 / data size / member size. CRC and size are checked."""
    b = _Buffered(f)
    first = True
    while True:
        hdr = b.read(6)
        if len(hdr) < 6 or hdr[:4] != b"LZIP":
            if first:
                raise ValueError("lzip 파일이 아닙니다")
            return  # trailing data after the last member
        first = False
        if hdr[4] != 1 or not 12 <= hdr[5] & 0x1F <= 29:
            raise ValueError("지원하지 않는 lzip 파일입니다")
        ds = 1 << (hdr[5] & 0x1F)
        ds -= (ds // 16) * ((hdr[5] >> 5) & 7)
        d = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[{"id": lzma.FILTER_LZMA1, "dict_size": ds,
                                                             "lc": 3, "lp": 0, "pb": 2}])
        crc, size = 0, 0
        buf = b""
        while not d.eof:
            if d.needs_input:
                buf = b.read(CHUNK)
                if not buf:
                    raise ValueError("lzip: 데이터가 잘렸습니다")
            out = d.decompress(buf, max_length=CHUNK)  # bounded output: a tiny file may expand enormously
            buf = b""
            crc, size = zlib.crc32(out, crc), size + len(out)
            yield out
        b.unread(d.unused_data)
        trailer = b.read(20)
        if len(trailer) < 20:
            raise ValueError("lzip: 데이터가 잘렸습니다")
        want_crc, want_size = struct.unpack("<IQ", trailer[:12])
        if want_crc != crc or want_size != size:
            raise ValueError("lzip: CRC 오류")


# ---------- LZ4 ----------

def _lz4_block(src: bytes, out: bytearray, start: int, limit: int = 8 << 20):
    """Decode one LZ4 block onto out (which holds the history window; the block's data starts at start). A block
    never decodes to more than its frame's block size (8 MiB at most): more means a hostile file."""
    i, n = 0, len(src)
    while i < n:
        if len(out) - start > limit:
            raise ValueError("LZ4: 손상된 데이터")
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                x = src[i]
                i += 1
                lit += x
                if x != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= n:
            break
        off = src[i] | src[i + 1] << 8
        i += 2
        ml = token & 15
        if ml == 15:
            while True:
                x = src[i]
                i += 1
                ml += x
                if x != 255:
                    break
        ml += 4
        if off == 0 or off > len(out):
            raise ValueError("LZ4: 손상된 데이터")
        s = len(out) - off
        if off >= ml:
            out += out[s:s + ml]
        else:  # overlapping match: the last `off` bytes repeat
            pat = bytes(out[s:])
            out += (pat * (ml // off + 1))[:ml]


def _lz4(f) -> Iterator[bytes]:
    b = _Buffered(f)
    first = True
    while True:
        magic = b.read(4)
        if len(magic) < 4:
            if first:
                raise ValueError("LZ4 파일이 아닙니다")
            return
        m = struct.unpack("<I", magic)[0]
        if 0x184D2A50 <= m <= 0x184D2A5F:  # skippable frame
            b.read(struct.unpack("<I", b.read(4))[0])
            continue
        if m == 0x184C2102:  # legacy frame: compressed blocks of up to 8 MiB, no end mark
            first = False
            while True:
                sz = b.read(4)
                if len(sz) < 4:
                    return
                n = struct.unpack("<I", sz)[0]
                if n in (0x184C2102, 0x184D2204):
                    b.unread(sz)
                    break
                data = b.read(n)
                if len(data) < n:
                    raise ValueError("LZ4: 데이터가 잘렸습니다")
                out = bytearray()
                _lz4_block(data, out, 0)
                yield bytes(out)
            continue
        if m != 0x184D2204:
            if first:
                raise ValueError("LZ4 파일이 아닙니다")
            return
        first = False
        flg, bd = b.read(2)
        if flg >> 6 != 1:
            raise ValueError("지원하지 않는 LZ4 버전입니다")
        block_ck, content_size, content_ck, dict_id = flg & 0x10, flg & 0x08, flg & 0x04, flg & 0x01
        b.read((8 if content_size else 0) + (4 if dict_id else 0) + 1)  # sizes, dictionary id, header checksum
        window = bytearray()
        while True:
            n = struct.unpack("<I", b.read(4))[0]
            if n == 0:
                break
            raw, n = n & 0x80000000, n & 0x7FFFFFFF
            data = b.read(n)
            if len(data) < n:
                raise ValueError("LZ4: 데이터가 잘렸습니다")
            if block_ck:
                b.read(4)
            start = len(window)
            if raw:
                window += data
            else:
                _lz4_block(data, window, start)
            yield bytes(window[start:])
            if len(window) > (1 << 17):  # keep the 64 KiB history dependent blocks may refer to
                del window[:len(window) - (1 << 16)]
        if content_ck:
            b.read(4)


# ---------- AES Crypt ----------

def _aescrypt(f, password: Optional[str]) -> Iterator[bytes]:
    """AES Crypt file format v1 / v2 (v0 has no key check). The HMACs are verified: a wrong password or damage
    is reported, never written out as garbage."""
    from Cryptodome.Cipher import AES
    hdr = f.read(5)
    if hdr[:3] != b"AES" or hdr[3] not in (1, 2):
        raise ValueError("AES Crypt 파일이 아닙니다 (지원: 버전 1, 2)")
    if not password:
        raise BadPassword("password required")
    if hdr[3] == 2:  # extensions: (length, data) until an empty one
        while True:
            n = struct.unpack(">H", f.read(2))[0]
            if n == 0:
                break
            f.read(n)
    iv1 = f.read(16)
    key = iv1 + bytes(16)
    pw = password.encode("utf-16-le")
    for _ in range(8192):
        key = hashlib.sha256(key + pw).digest()
    enc = f.read(48)
    mac = f.read(32)
    if not hmac.compare_digest(hmac.new(key, enc, hashlib.sha256).digest(), mac):
        raise BadPassword("wrong password")
    inner = AES.new(key, AES.MODE_CBC, iv1).decrypt(enc)
    iv2, key2 = inner[:16], inner[16:48]
    rest_start = f.tell()
    end = f.seek(0, 2)
    f.seek(rest_start)
    clen = end - rest_start - 33  # the ciphertext, then 1 byte (length mod 16) and the HMAC
    if clen < 0 or clen % 16:
        raise ValueError("AES Crypt: 손상된 파일")
    h = hmac.new(key2, digestmod=hashlib.sha256)
    c = AES.new(key2, AES.MODE_CBC, iv2)
    left, pending = clen, b""
    while left:
        buf = f.read(min(CHUNK, left))
        left -= len(buf)
        h.update(buf)
        plain = c.decrypt(buf)
        if pending:
            yield pending
        pending = plain
    tail = f.read(1)[0] & 0x0F
    if not hmac.compare_digest(h.digest(), f.read(32)):
        raise ValueError("AES Crypt: 데이터 무결성 오류")
    if pending:  # the last block is padded unless the size is a multiple of 16 (tail 0)
        yield pending[:len(pending) - (16 - tail) % 16]


def open_stream(kind: str, path: str, password: Optional[str] = None) -> _ChunkReader:
    f = open(path, "rb")
    try:
        gen = {"br": _brotli, "lz": _lzip, "lz4": _lz4}.get(kind)
        it = gen(f) if gen else _aescrypt(f, password)
        return _ChunkReader(it, f.close)
    except BaseException:
        f.close()
        raise


# ---------- uuencode / xxencode / base64 ----------

_XX = "+-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_XX_TO_UU = str.maketrans({c: chr(32 + i) for i, c in enumerate(_XX)})


def _uu_files(text: str, xx_hint: bool = False) -> list[tuple[str, bytes]]:
    """Every 'begin <mode> <name>' ... 'end' block (uu or xx) and 'begin-base64 <mode> <name>' ... '===='.
    xx_hint: the file is named .xxe (a short xxencoded line can also look like uuencode)."""
    out, lines, i = [], text.splitlines(), 0
    while i < len(lines):
        ln = lines[i]
        i += 1
        b64 = ln.startswith("begin-base64 ")
        if not (b64 or ln.startswith("begin ")):
            continue
        parts = ln.split(" ", 2)
        if len(parts) < 3:
            continue
        name, data = parts[2].strip(), bytearray()
        xx = None
        while i < len(lines):
            body = lines[i]
            i += 1
            if b64:
                if body.strip() == "====":
                    break
                data += binascii.a2b_base64(body)
                continue
            if body.strip() == "end":
                break
            if not body:
                continue
            if xx is None:  # uuencode lines start with ' '..'`' (45 bytes: 'M'); xxencode's 45 bytes: 'h'
                xx = xx_hint or not (" " <= body[0] <= "`")
            if xx:
                body = body.translate(_XX_TO_UU)
            n = (ord(body[0]) - 32) & 63
            if n == 0:
                continue
            try:
                data += binascii.a2b_uu(body)[:n]
            except binascii.Error:  # some encoders drop trailing padding
                data += binascii.a2b_uu(body.rstrip() + " " * (61 - len(body.rstrip())))[:n]
        out.append((name, bytes(data)))
    return out


class _MemArchive:
    """Archive API over (name, bytes, mtime) members held in memory (small text-encoded formats)."""

    method = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def close(self):
        pass

    def needs_password(self) -> bool:
        return False

    def entries(self) -> list[Entry]:
        mt = __import__("time").localtime(self.mtime)[:6]
        return [Entry(n, False, len(d), len(d), mt, self.method, False, zlib.crc32(d)) for n, d in self.members]

    def extract(self, dest, names=None, progress=None, cancel=None, overwrite=True):
        import io
        import formats
        plan = formats._Planner(dest, overwrite, self.path)
        todo = [(n, d) for n, d in self.members if names is None or n in names]
        counter = formats._Counter(sum(len(d) for _, d in todo), progress, cancel)
        for n, d in todo:
            t = plan.target(n, False)
            if t:
                formats._copy_out(io.BytesIO(d), t, counter, n, self.mtime)

    def test(self, progress=None, cancel=None) -> list[str]:
        if progress:
            progress(1, 1, os.path.basename(self.path))
        return []


class UUArchive(_MemArchive):
    method = "UU"

    def __init__(self, path: str):
        self.path, self.mtime = path, os.path.getmtime(path)
        if os.path.getsize(path) > 512 << 20:
            raise ValueError("너무 큰 uuencode 파일입니다")
        with open(path, encoding="latin-1") as f:
            self.members = _uu_files(f.read(), path.lower().endswith(".xxe"))
        if not self.members:
            raise ValueError("uuencode / xxencode 데이터가 없습니다")


# ---------- ASAR (Electron) ----------

class AsarArchive:
    """Electron's .asar: a Chromium pickle holding a JSON tree of {size, offset} entries, then the data.
    Entries marked "unpacked" are read from the "<name>.asar.unpacked" folder next to it; links are skipped."""

    def __init__(self, path: str):
        self.path = path
        with open(path, "rb") as f:
            head = f.read(16)
            if len(head) < 16:
                raise ValueError("ASAR 파일이 아닙니다")
            _, header_size, _, json_len = struct.unpack("<4I", head)
            raw = f.read(json_len)
        try:
            tree = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ValueError("ASAR 파일이 아닙니다")
        self.base = 8 + header_size
        self.items: list[tuple[str, dict]] = []

        def walk(node, prefix):
            for name, sub in node.get("files", {}).items():
                full = prefix + name
                if "files" in sub:
                    self.items.append((full + "/", sub))
                    walk(sub, full + "/")
                elif "link" not in sub:
                    self.items.append((full, sub))
        walk(tree, "")
        self.mtime = os.path.getmtime(path)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def close(self):
        pass

    def needs_password(self) -> bool:
        return False

    def entries(self) -> list[Entry]:
        mt = __import__("time").localtime(self.mtime)[:6]
        return [Entry(n, n.endswith("/"), int(m.get("size", 0)), int(m.get("size", 0)), mt, "ASAR", False, 0)
                for n, m in self.items]

    def _open(self, name, meta):
        import io
        if meta.get("unpacked"):  # never outside the .unpacked folder, whatever the name says
            p = ziplib.safe_target(self.path + ".unpacked", name)
            if p is None:
                raise ValueError("ASAR: 잘못된 이름 " + name)
            return open(p, "rb")
        f = open(self.path, "rb")
        f.seek(self.base + int(meta.get("offset", 0)))
        data = f.read(int(meta.get("size", 0)))
        f.close()
        if len(data) != int(meta.get("size", 0)):
            raise ValueError("ASAR: 데이터가 잘렸습니다")
        return io.BytesIO(data)

    def extract(self, dest, names=None, progress=None, cancel=None, overwrite=True):
        import formats
        plan = formats._Planner(dest, overwrite, self.path)
        todo = [(n, m) for n, m in self.items if names is None or n in names]
        counter = formats._Counter(sum(int(m.get("size", 0)) for _, m in todo), progress, cancel)
        for n, m in todo:
            t = plan.target(n, n.endswith("/"))
            if t and not n.endswith("/"):
                with self._open(n, m) as src:
                    formats._copy_out(src, t, counter, n, self.mtime)

    def test(self, progress=None, cancel=None) -> list[str]:
        bad = []
        for n, m in self.items:
            if n.endswith("/"):
                continue
            try:
                with self._open(n, m) as src:
                    src.read()
            except (OSError, ValueError):
                bad.append(n)
        if progress:
            progress(1, 1, os.path.basename(self.path))
        return bad
