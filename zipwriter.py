"""Multi-threaded ZIP writer. Data is deflated in independent 1 MiB blocks on a thread pool (zlib releases the
GIL), each block ends with a sync flush so the blocks concatenate into one valid deflate stream (the pigz
technique). Entries stream with a data descriptor, so memory stays bounded.

With a password, entries use WinZip AES-256 (AE-2): per entry a random salt, PBKDF2-HMAC-SHA1 (1000 rounds)
key derivation computed ahead on the pool, AES-CTR (little-endian counter from 1) and HMAC-SHA1 over the
ciphertext, applied in order by the writer thread. Every entry gets its own salt and therefore its own keys.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import runtimeconfig
import struct
import time
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

CHUNK = 1 << 20
ZIP64_LIMIT = 0xFFFFFFFF  # lowered by tests to exercise the zip64 paths
_FINAL_BLOCK = b"\x03\x00"  # empty final fixed-Huffman block ends the deflate stream


AES_SALT, AES_KEY, AES_MAC_LEN = 16, 32, 10  # AES-256


def _dos_time(mtime: float):
    try:
        t = time.localtime(mtime)
        y, mo, d, h, mi, s = t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec
    except (OverflowError, OSError, ValueError):  # before 1970 / far future: Windows localtime refuses
        y, mo, d, h, mi, s = 1980, 1, 1, 0, 0, 0
    if y < 1980:  # DOS dates cover 1980..2107
        y, mo, d, h, mi, s = 1980, 1, 1, 0, 0, 0
    elif y > 2107:
        y, mo, d, h, mi, s = 2107, 12, 31, 23, 59, 58
    return (h << 11) | (mi << 5) | (s // 2), ((y - 1980) << 9) | (mo << 5) | d


def _may_need_zip64(size: int, aes: bool) -> bool:
    # deflate of incompressible data grows a little (~0.03% per 1 MiB block) and AES adds salt/verifier/MAC:
    # decide from the worst case, the sizes are only known after the data is written
    return size + size // 1000 + (1 << 16) + (28 if aes else 0) >= ZIP64_LIMIT


class _Entry:
    __slots__ = ("raw", "dos", "attr", "zip64", "method", "flags", "offset", "crc", "csize", "usize",
                 "aes", "keys", "cipher", "mac")

    def __init__(self, name: str, mtime: float, is_dir: bool, size: int, method: int, aes: bool = False):
        self.raw = name.encode("utf-8")
        self.dos = _dos_time(mtime)
        self.attr = ((0o40755 << 16) | 0x10) if is_dir else (0o100644 << 16)
        self.zip64 = (not is_dir) and _may_need_zip64(size, aes)
        self.method = 0 if is_dir else method
        self.flags = 0x0800 | (0 if is_dir else 0x0008)  # UTF-8 names; sizes follow in a data descriptor
        self.offset = self.crc = self.csize = self.usize = 0
        self.aes = aes and not is_dir
        if self.aes:
            self.flags |= 0x0001
        self.keys = self.cipher = self.mac = None

    @property
    def header_method(self):
        return 99 if self.aes else self.method

    @property
    def stored_crc(self):
        return 0 if self.aes else self.crc  # AE-2 stores no CRC; the HMAC authenticates the data instead

    def aes_extra(self) -> bytes:  # 0x9901: AE-2, vendor "AE", strength 3 (256-bit), real method
        return struct.pack("<HHH2sBH", 0x9901, 7, 2, b"AE", 3, self.method) if self.aes else b""


class _Copied:
    """An entry of an existing zip copied byte for byte (add / delete / rename edit without recompressing)."""
    __slots__ = ("info", "raw", "dos", "attr", "zip64", "flags", "offset", "crc", "csize", "usize", "aes",
                 "header_method", "stored_crc", "extra", "made_by")

    def __init__(self, info, name: str):
        self.info = info
        self.made_by = (info.create_system << 8) | info.create_version
        self.raw = name.encode("utf-8")
        y, mo, d, h, mi, s = info.date_time
        self.dos = ((h << 11) | (mi << 5) | (s // 2), (max(y, 1980) - 1980) << 9 | (mo << 5) | d)
        self.attr = info.external_attr
        self.flags = (info.flag_bits & 0x0009) | 0x0800  # keep encrypted + descriptor bits, name is UTF-8 now
        self.crc = self.stored_crc = info.CRC
        self.csize, self.usize = info.compress_size, info.file_size
        self.header_method = info.compress_type
        self.aes = info.compress_type == 99
        self.zip64 = max(self.csize, self.usize) >= 0xFFFFFFFF
        self.extra = _extra_field(info.extra, 0x9901)  # WinZip AES parameters must travel with the data
        self.offset = 0

    def aes_extra(self) -> bytes:
        return self.extra


def _extra_field(extra: bytes, want: int) -> bytes:
    i = 0
    while i + 4 <= len(extra):
        tag, n = struct.unpack_from("<HH", extra, i)
        if tag == want:
            return extra[i:i + 4 + n]
        i += 4 + n
    return b""


def _copy_entry(src, out, c: _Copied):
    info = c.info
    src.seek(info.header_offset)
    head = src.read(30)
    if head[:4] != b"PK\x03\x04":
        raise ValueError(f"local header missing: {info.filename}")
    n_len, x_len = struct.unpack("<HH", head[26:30])
    src.seek(info.header_offset + 30 + n_len + x_len)
    c.offset = out.tell()
    ver = 51 if c.aes else (45 if c.zip64 else 20)
    extra = (struct.pack("<HHQQ", 1, 16, c.usize, c.csize) if c.zip64 else b"") + c.extra
    big = 0xFFFFFFFF if c.zip64 else None
    out.write(struct.pack("<IHHHHHIIIHH", 0x04034B50, ver, c.flags, c.header_method, c.dos[0], c.dos[1],
                          c.stored_crc, big or c.csize, big or c.usize, len(c.raw), len(extra)) + c.raw + extra)
    left = c.csize
    while left:
        buf = src.read(min(left, 4 << 20))
        if not buf:
            raise ValueError(f"truncated data: {info.filename}")
        out.write(buf)
        left -= len(buf)
    if c.flags & 0x0008:  # ZipCrypto checks against the time when bit 3 is set, so keep the descriptor form
        out.write(struct.pack("<IIQQ" if c.zip64 else "<IIII", 0x08074B50, c.stored_crc, c.csize, c.usize))


def _derive(password: bytes, salt: bytes):
    dk = hashlib.pbkdf2_hmac("sha1", password, salt, 1000, 2 * AES_KEY + 2)
    return salt, dk[:AES_KEY], dk[AES_KEY:2 * AES_KEY], dk[2 * AES_KEY:]


def _deflate(data: bytes, level: int) -> bytes:
    c = zlib.compressobj(level, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush(zlib.Z_SYNC_FLUSH)


def _small_file(path: str, level: int, method: int):
    """Overlap small-file opens/reads as well as deflate, with a bounded buffer.

    A growing source must never be silently truncated to the small-file limit.
    The caller's transactional output handling will discard a failed archive.
    """
    with open(path, "rb") as src:
        data = src.read(CHUNK + 1)
    if len(data) > CHUNK:
        raise OSError(f"Source grew beyond the queued small-file limit: {path}")
    blob = _deflate(data, level) if method == 8 and data else data
    return blob, zlib.crc32(data), len(data)


def _local_header(e: _Entry, size_hint: int) -> bytes:
    ver = 51 if e.aes else (45 if e.zip64 else 20)
    extra = (struct.pack("<HHQQ", 1, 16, size_hint, 0) if e.zip64 else b"") + e.aes_extra()
    big = 0xFFFFFFFF if e.zip64 else 0
    return struct.pack("<IHHHHHIIIHH", 0x04034B50, ver, e.flags, e.header_method, e.dos[0], e.dos[1], 0, big, big,
                       len(e.raw), len(extra)) + e.raw + extra


def _central_entry(e: _Entry) -> bytes:
    extra_vals, usize, csize, off = [], e.usize, e.csize, e.offset
    if usize >= 0xFFFFFFFF:
        extra_vals.append(usize)
        usize = 0xFFFFFFFF
    if csize >= 0xFFFFFFFF:
        extra_vals.append(csize)
        csize = 0xFFFFFFFF
    if off >= 0xFFFFFFFF:
        extra_vals.append(off)
        off = 0xFFFFFFFF
    extra = struct.pack("<HH%dQ" % len(extra_vals), 1, 8 * len(extra_vals), *extra_vals) if extra_vals else b""
    extra += e.aes_extra()
    ver = 51 if e.aes else (45 if extra_vals or e.zip64 else 20)
    made_by = getattr(e, "made_by", None) or (3 << 8) | ver  # copied entries keep their host OS (attrs depend on it)
    return struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, made_by, ver, e.flags, e.header_method, e.dos[0],
                       e.dos[1], e.stored_crc, csize, usize, len(e.raw), len(extra), 0, 0, 0, e.attr, off) + e.raw + extra


def write_zip(items, out_path: str, level: int = 6, store_only: bool = False,
              progress: Optional[Callable[[int, int, str], None]] = None,
              cancel: Optional[Callable[[], bool]] = None, workers: Optional[int] = None,
              password: Optional[str] = None, copy=None):
    """items: iterable of (absolute path, archive name); names ending in '/' are directory entries.
    password: encrypt file entries with WinZip AES-256 (the password is used as UTF-8).
    copy: (zip path, [(ZipInfo, new name)]) entries of an existing zip written first, byte for byte."""
    from ziplib import Cancelled
    if password:
        from Cryptodome.Cipher import AES
        from Cryptodome.Util import Counter
    pw = password.encode("utf-8") if password else None
    metadata = []
    for path, name in items:
        if cancel and cancel():
            raise Cancelled()
        metadata.append((path, name, os.stat(path)))
    total = sum(st.st_size for _, name, st in metadata if not name.endswith("/"))
    method = 0 if store_only else 8
    workers = min(workers, runtimeconfig.worker_count()) if workers else runtimeconfig.worker_count(16)
    window = workers * 2
    done = 0
    entries: list[_Entry] = []
    pending: deque = deque()

    with open(out_path, "wb", buffering=1 << 20) as out, ThreadPoolExecutor(workers) as pool:
        def drain(limit: int):
            nonlocal done
            while len(pending) > limit:
                if cancel and cancel():
                    raise Cancelled()
                kind, *rest = pending.popleft()
                if kind == "hdr":
                    e, size = rest
                    e.offset = out.tell()
                    out.write(_local_header(e, size))
                    entries.append(e)
                    if e.aes:
                        salt, key, mac_key, verifier = e.keys.result()
                        out.write(salt + verifier)
                        e.csize += len(salt) + len(verifier)
                        e.cipher = AES.new(key, AES.MODE_CTR, counter=Counter.new(128, initial_value=1, little_endian=True))
                        e.mac = hmac.new(mac_key, digestmod=hashlib.sha1)
                elif kind in ("data", "small"):
                    e, fut = rest[:2]
                    if kind == "small":
                        blob, e.crc, e.usize = fut.result()
                        done += e.usize
                        if progress:
                            progress(done, total, rest[2])
                    else:
                        blob = fut.result()
                    if e.aes:
                        blob = e.cipher.encrypt(blob)
                        e.mac.update(blob)
                    e.csize += len(blob)
                    out.write(blob)
                else:  # "end"
                    (e,) = rest
                    if e.method == 8:
                        tail = _FINAL_BLOCK
                        if e.aes:
                            tail = e.cipher.encrypt(tail)
                            e.mac.update(tail)
                        out.write(tail)
                        e.csize += len(tail)
                    if e.aes:
                        out.write(e.mac.digest()[:AES_MAC_LEN])
                        e.csize += AES_MAC_LEN
                        e.cipher = e.mac = None
                    crc = 0 if e.aes else e.crc
                    out.write(struct.pack("<IIQQ" if e.zip64 else "<IIII", 0x08074B50, crc, e.csize, e.usize))

        try:
            if copy:
                with open(copy[0], "rb") as src:
                    for info, name in copy[1]:
                        if cancel and cancel():
                            raise Cancelled()
                        c = _Copied(info, name)
                        _copy_entry(src, out, c)
                        entries.append(c)
            for path, name, st in metadata:
                if cancel and cancel():
                    raise Cancelled()
                is_dir = name.endswith("/")
                size = 0 if is_dir else st.st_size
                e = _Entry(name, st.st_mtime, is_dir, size, method, aes=bool(pw))
                if e.aes:  # key derivation (PBKDF2) runs on the pool while earlier data is still being written
                    e.keys = pool.submit(_derive, pw, os.urandom(AES_SALT))
                if is_dir:
                    pending.append(("hdr", e, 0))
                    drain(window)
                    continue
                pending.append(("hdr", e, size))
                if size <= CHUNK:
                    pending.append(("small", e, pool.submit(_small_file, path, level, method), name))
                    pending.append(("end", e))
                    drain(window)
                    continue
                with open(path, "rb") as src:
                    while True:
                        data = src.read(CHUNK)
                        if not data:
                            break
                        e.crc = zlib.crc32(data, e.crc)
                        e.usize += len(data)
                        fut = pool.submit(_deflate, data, level) if method == 8 else pool.submit(bytes, data)
                        pending.append(("data", e, fut))
                        done += len(data)
                        if progress:
                            progress(done, total, name)
                        drain(window)
                        if cancel and cancel():
                            raise Cancelled()
                pending.append(("end", e))
            drain(0)
        except BaseException:
            pool.shutdown(wait=True, cancel_futures=True)
            raise

        cd_start = out.tell()
        for e in entries:
            out.write(_central_entry(e))
        cd_size = out.tell() - cd_start
        n = len(entries)
        if n >= 0xFFFF or cd_start >= 0xFFFFFFFF or cd_size >= 0xFFFFFFFF:
            z64_off = out.tell()
            out.write(struct.pack("<IQHHIIQQQQ", 0x06064B50, 44, 45, 45, 0, 0, n, n, cd_size, cd_start))
            out.write(struct.pack("<IIQI", 0x07064B50, 0, z64_off, 1))
            n, cd_size, cd_start = min(n, 0xFFFF), min(cd_size, 0xFFFFFFFF), min(cd_start, 0xFFFFFFFF)
        out.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, n, n, cd_size, cd_start, 0))
