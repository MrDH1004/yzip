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


class _Entry:
    __slots__ = ("raw", "dos", "attr", "zip64", "method", "flags", "offset", "crc", "csize", "usize",
                 "aes", "keys", "cipher", "mac")

    def __init__(self, name: str, mtime: float, is_dir: bool, size: int, method: int, aes: bool = False):
        self.raw = name.encode("utf-8")
        t = time.localtime(mtime)
        year = max(t.tm_year, 1980)
        self.dos = ((t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2), ((year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday)
        self.attr = ((0o40755 << 16) | 0x10) if is_dir else (0o100644 << 16)
        self.zip64 = (not is_dir) and size >= ZIP64_LIMIT
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

    def aes_extra(self) -> bytes:  # 0x9901: AE-2, vendor "AE", strength 3 (256-bit), real method
        return struct.pack("<HHH2sBH", 0x9901, 7, 2, b"AE", 3, self.method) if self.aes else b""


def _derive(password: bytes, salt: bytes):
    dk = hashlib.pbkdf2_hmac("sha1", password, salt, 1000, 2 * AES_KEY + 2)
    return salt, dk[:AES_KEY], dk[AES_KEY:2 * AES_KEY], dk[2 * AES_KEY:]


def _deflate(data: bytes, level: int) -> bytes:
    c = zlib.compressobj(level, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush(zlib.Z_SYNC_FLUSH)


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
    crc = 0 if e.aes else e.crc  # AE-2 stores no CRC; the HMAC authenticates the data instead
    return struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, (3 << 8) | ver, ver, e.flags, e.header_method, e.dos[0],
                       e.dos[1], crc, csize, usize, len(e.raw), len(extra), 0, 0, 0, e.attr, off) + e.raw + extra


def write_zip(items, out_path: str, level: int = 6, store_only: bool = False,
              progress: Optional[Callable[[int, int, str], None]] = None,
              cancel: Optional[Callable[[], bool]] = None, workers: Optional[int] = None,
              password: Optional[str] = None):
    """items: iterable of (absolute path, archive name); names ending in '/' are directory entries.
    password: encrypt file entries with WinZip AES-256 (the password is used as UTF-8)."""
    from ziplib import Cancelled
    if password:
        from Cryptodome.Cipher import AES
        from Cryptodome.Util import Counter
    pw = password.encode("utf-8") if password else None
    items = list(items)
    total = sum(os.path.getsize(p) for p, n in items if not n.endswith("/"))
    method = 0 if store_only else 8
    workers = workers or min(os.cpu_count() or 2, 16)
    window = workers * 2
    done = 0
    entries: list[_Entry] = []
    pending: deque = deque()

    with open(out_path, "wb", buffering=1 << 20) as out, ThreadPoolExecutor(workers) as pool:
        def drain(limit: int):
            while len(pending) > limit:
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
                elif kind == "data":
                    e, fut = rest
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
            for path, name in items:
                if cancel and cancel():
                    raise Cancelled()
                is_dir = name.endswith("/")
                size = 0 if is_dir else os.path.getsize(path)
                e = _Entry(name, os.path.getmtime(path), is_dir, size, method, aes=bool(pw))
                if e.aes:  # key derivation (PBKDF2) runs on the pool while earlier data is still being written
                    e.keys = pool.submit(_derive, pw, os.urandom(AES_SALT))
                if is_dir:
                    pending.append(("hdr", e, 0))
                    drain(window)
                    continue
                pending.append(("hdr", e, size))
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
