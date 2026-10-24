"""Multi-threaded LZMA2 writers for .7z and .xz.

Python's lzma runs one thread per call but drops the GIL while it works, so the input is cut into blocks that
are compressed side by side. Each block starts with an LZMA2 dictionary reset, which is how 7-Zip's and xz's
own multi-threaded encoders work, so any reader decodes the result as one ordinary stream.
  .xz : one stream, one xz block per input block (the layout `xz -T0` writes), CRC32 check.
  .7z : solid archive, one LZMA2 folder (blocks joined at LZMA2 chunk level), optional 7zAES-256 for data and
        header (file names hidden), written directly instead of through py7zr (single-threaded, ~1 MB/s)."""
from __future__ import annotations

import collections
import hashlib
import lzma
import os
import struct
import zlib
from concurrent.futures import ThreadPoolExecutor

_DICT = [1 << 18, 1 << 20, 2 << 20, 4 << 20, 4 << 20, 8 << 20, 8 << 20, 16 << 20, 32 << 20, 64 << 20]
_MEM_BUDGET = 2 << 30


def plan(preset: int, total: int) -> tuple[int, int]:
    """Block size and thread count: as many threads as fit in a ~2 GB budget (the encoder needs ~12x the
    dictionary), and blocks between 1x and 4x the dictionary (7-Zip's default is 4x), small enough that each
    thread gets ~3 blocks: with one block per thread the last, slowest block leaves the other cores idle."""
    d = _DICT[preset]
    cpu = os.cpu_count() or 2
    workers = max(1, min(cpu, _MEM_BUDGET // (d * 15)))
    block = max(1 << 20, min(4 * d, max(d, -(-total // (workers * 3)))))
    return block, workers


def _filters(preset):
    return [{"id": lzma.FILTER_LZMA2, "preset": preset}]


def _dict_prop(size: int) -> int:
    """LZMA2 dictionary-size byte: smallest p with (2 | p&1) << (p//2 + 11) >= size."""
    for p in range(40):
        if (2 | (p & 1)) << (p // 2 + 11) >= size:
            return p
    return 40


class _Ordered:
    """Runs fn on submitted items in a thread pool and hands results back in submit order, keeping at most
    `workers + 1` jobs in flight so memory stays bounded."""

    def __init__(self, fn, workers: int):
        self.fn, self.workers = fn, workers
        self.pool = ThreadPoolExecutor(workers)
        self.pending: collections.deque = collections.deque()

    def submit(self, item):
        self.pending.append(self.pool.submit(self.fn, item))
        while len(self.pending) > self.workers:
            yield self.pending.popleft().result()

    def drain(self):
        while self.pending:
            yield self.pending.popleft().result()

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)


def _raw_lzma2(preset):
    """Block compressor: raw LZMA2 without its end marker (one 0x00 is written after the last block)."""
    filters = _filters(preset)

    def go(data):
        c = lzma.compress(data, lzma.FORMAT_RAW, filters=filters)
        assert c[-1] == 0
        return c[:-1]
    return go


# ---------------------------------------------------------------- .xz

def _xz_varint(n: int) -> bytes:
    out = bytearray()
    while n >= 0x80:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def _pad4(n: int) -> bytes:
    return b"\0" * (-n % 4)


class XZWriter:
    """Write-only file object producing a multi-block .xz stream; compression runs on worker threads."""

    def __init__(self, raw, preset: int = 6, size_hint: int = 0):
        self.raw, self.preset = raw, preset
        self.block, workers = plan(preset, size_hint)
        self.buf = bytearray()
        self.index: list[tuple[int, int]] = []
        self.jobs = _Ordered(self._encode, workers)
        self.closed = False
        flags = b"\x00\x01"  # check type 1 = CRC32
        raw.write(b"\xfd7zXZ\x00" + flags + struct.pack("<I", zlib.crc32(flags)))

    def _encode(self, data):
        body = lzma.compress(data, lzma.FORMAT_RAW, filters=_filters(self.preset))
        props = bytes([0x21, 1, _dict_prop(_DICT[self.preset])])  # filter id LZMA2, 1 property byte
        fields = b"\xc0" + _xz_varint(len(body)) + _xz_varint(len(data)) + props  # both sizes present, 1 filter
        size = 1 + len(fields)
        size += -size % 4 + 4
        hdr = bytes([size // 4 - 1]) + fields + _pad4(1 + len(fields))
        hdr += struct.pack("<I", zlib.crc32(hdr))
        check = struct.pack("<I", zlib.crc32(data))
        unpadded = len(hdr) + len(body) + len(check)
        return hdr + body + _pad4(len(body)) + check, unpadded, len(data)

    def _put(self, results):
        for blob, unpadded, usize in results:
            self.raw.write(blob)
            self.index.append((unpadded, usize))

    def write(self, b) -> int:
        self.buf += b
        while len(self.buf) >= self.block:
            chunk = bytes(self.buf[:self.block])
            del self.buf[:self.block]
            self._put(self.jobs.submit(chunk))
        return len(b)

    def flush(self):
        pass

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.buf:
                self._put(self.jobs.submit(bytes(self.buf)))
                self.buf.clear()
            self._put(self.jobs.drain())
        finally:
            self.jobs.close()
        idx = b"\x00" + _xz_varint(len(self.index)) + b"".join(_xz_varint(u) + _xz_varint(s) for u, s in self.index)
        idx += _pad4(len(idx))
        idx += struct.pack("<I", zlib.crc32(idx))
        self.raw.write(idx)
        tail = struct.pack("<I", len(idx) // 4 - 1) + b"\x00\x01"
        self.raw.write(struct.pack("<I", zlib.crc32(tail)) + tail + b"YZ")

    def abort(self):
        """Stop the workers without finishing the stream (the caller deletes the partial file)."""
        self.closed = True
        self.jobs.close()

    def __enter__(self):
        return self

    def __exit__(self, et, *_):
        if et is None:
            self.close()
        else:
            self.abort()


def _xz_read_varint(b: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        c = b[i]
        i += 1
        n |= (c & 0x7F) << shift
        shift += 7
        if c < 0x80:
            return n, i
        if shift > 63:
            raise ValueError("bad xz varint")


def xz_blocks(path: str):
    """Block table of an .xz file, read from the block headers alone: [(offset, length, unpadded, usize,
    stream flags)]. Raises Unsupported when a block does not record its sizes (single-threaded xz output):
    then it cannot be cut without decoding."""
    blocks = []
    with open(path, "rb") as f:
        end = os.fstat(f.fileno()).st_size
        pos = 0
        while pos < end:
            f.seek(pos)
            head = f.read(12)
            if head[:6] != b"\xfd7zXZ\x00":
                if not head.strip(b"\0"):  # stream padding between concatenated streams
                    pos += 4
                    continue
                raise Unsupported("not xz")
            flags = head[6:8]
            check = {0: 0, 1: 4, 4: 8, 10: 32}.get(flags[1] & 0x0F)
            if check is None:
                raise Unsupported("check type")
            pos += 12
            while True:
                f.seek(pos)
                b0 = f.read(1)
                if not b0:
                    raise ValueError("xz stream ends early")
                if b0 == b"\0":  # index (indicator, count, records, padding, CRC32), then the 12-byte footer
                    f.seek(pos)
                    idx = f.read(16)
                    count, _ = _xz_read_varint(idx, 1)
                    f.seek(pos)
                    idx = f.read(count * 20 + 16)  # a record is two varints of at most 10 bytes each
                    i = 1
                    for _ in range(count * 2 + 1):
                        _, i = _xz_read_varint(idx, i)
                    pos += i + (-i % 4) + 4 + 12
                    break
                hsize = (b0[0] + 1) * 4
                f.seek(pos)
                h = f.read(hsize)
                bflags = h[1]
                if bflags & 0xC0 != 0xC0:
                    raise Unsupported("block sizes not recorded")
                csize, i = _xz_read_varint(h, 2)
                usize, i = _xz_read_varint(h, i)
                unpadded = hsize + csize + check
                length = hsize + csize + (-csize % 4) + check
                blocks.append((pos, length, unpadded, usize, flags))
                pos += length
    return blocks


def _xz_mini_stream(flags: bytes, block: bytes, unpadded: int, usize: int) -> bytes:
    """One block wrapped as a complete .xz stream, so liblzma checks its header CRC and data check."""
    idx = b"\x00\x01" + _xz_varint(unpadded) + _xz_varint(usize)
    idx += _pad4(len(idx))
    idx += struct.pack("<I", zlib.crc32(idx))
    tail = struct.pack("<I", len(idx) // 4 - 1) + flags
    return (b"\xfd7zXZ\x00" + flags + struct.pack("<I", zlib.crc32(flags)) + block + idx
            + struct.pack("<I", zlib.crc32(tail)) + tail + b"YZ")


class XZReader:
    """Read-only, forward-only file object over a multi-block .xz file; blocks decode in parallel ahead of
    the reader. Build it with open_xz(), which falls back to lzma.open for single-block files."""

    def __init__(self, path: str, blocks):
        self.f = open(path, "rb")
        self.blocks = collections.deque(blocks)
        self.workers = os.cpu_count() or 2
        self.pool = ThreadPoolExecutor(self.workers)
        self.pending: collections.deque = collections.deque()
        self.held = 0
        self.cur, self.pos = b"", 0
        self._fill()

    def _decode(self, args):
        return lzma.decompress(_xz_mini_stream(*args))

    def _fill(self):
        while self.blocks and len(self.pending) < self.workers * 2 and self.held < _INFLIGHT:
            off, length, unpadded, usize, flags = self.blocks.popleft()
            self.f.seek(off)
            data = self.f.read(length)
            self.pending.append((self.pool.submit(self._decode, (flags, data, unpadded, usize)), usize))
            self.held += usize

    def read(self, n: int = -1) -> bytes:
        out = []
        while n < 0 or n > 0:
            if self.pos >= len(self.cur):
                if not self.pending:
                    break
                fut, usize = self.pending.popleft()
                self.cur, self.pos = fut.result(), 0
                self.held -= usize
                self._fill()
                continue
            take = len(self.cur) - self.pos if n < 0 else min(n, len(self.cur) - self.pos)
            out.append(self.cur[self.pos:self.pos + take])
            self.pos += take
            if n > 0:
                n -= take
        return b"".join(out)

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def open_xz(path: str):
    try:
        blocks = xz_blocks(path)
    except (Unsupported, ValueError, IndexError, OSError):
        blocks = None
    if not blocks or len(blocks) < 2:
        return lzma.open(path, "rb")
    return XZReader(path, blocks)


# ---------------------------------------------------------------- .7z reading

_SEG_CAP = 128 << 20      # a segment bigger than this (unpacked) is decoded as a stream on the main thread
_INFLIGHT = 1 << 30       # unpacked bytes held by parallel jobs at once


class Unsupported(Exception):
    """Layout the fast reader does not handle; the caller falls back to py7zr."""


def fast_plan(z) -> list[dict]:
    """Folder list for fast_extract from an open py7zr.SevenZipFile (py7zr parses the header, including an
    encrypted or compressed one). Handles folders made of LZMA2 or COPY, optionally behind 7zAES, with one
    packed stream each: what 7-Zip, Bandizip, py7zr and write_7z produce for these settings."""
    ms = z.header.main_streams
    if ms is None:
        return []
    pi, ui, ss = ms.packinfo, ms.unpackinfo, ms.substreamsinfo
    files = [f for f in z.files if not f.emptystream]
    counts = ss.num_unpackstreams_folders if ss else [1] * ui.numfolders
    sizes = ss.unpacksizes if ss else None
    digests = iter(ss.digests if ss and ss.digests else [])
    out, fi, si = [], 0, 0
    for i, fo in enumerate(ui.folders):
        ids = [c["method"] for c in fo.coders]
        if any(c.get("numinstreams", 1) != 1 or c.get("numoutstreams", 1) != 1 for c in fo.coders) \
                or fo.packed_indices not in ([0], None) or len(pi.packsizes) != ui.numfolders:
            raise Unsupported("coder graph")
        aes = None
        if ids[0] == _ID_AES:
            aes, ids = fo.coders[0]["properties"], ids[1:]
            if len(ids) != 1 or fo.bindpairs[0].incoder != 1 or fo.bindpairs[0].outcoder != 0:
                raise Unsupported("aes chain")
        if ids not in ([_ID_LZMA2], [_ID_COPY]) or (aes is None and len(fo.coders) != 1):
            raise Unsupported(f"coders {ids}")
        n = counts[i]
        members = []
        for k in range(n):
            size = sizes[si] if sizes is not None else fo.unpacksizes[-1]
            si += 1
            crc = fo.crc if n == 1 and fo.digestdefined else next(digests, None)
            members.append((files[fi], size, crc))
            fi += 1
        out.append({"offset": z.afterheader + pi.packpos + pi.packpositions[i], "packsize": pi.packsizes[i],
                    "aes": aes, "coded": fo.unpacksizes[0], "lzma2": ids == [_ID_LZMA2],
                    "dict": fo.coders[-1].get("properties", b"\x00")[0] if ids == [_ID_LZMA2] else 0,
                    "members": members})
    return out


def _aes_reader(f, size, coded, props, password):
    b0 = props[0]
    cycles = b0 & 0x3F
    b1 = props[1] if len(props) > 1 else 0
    salt_n = ((b0 >> 7) & 1) + (b1 >> 4)
    iv_n = ((b0 >> 6) & 1) + (b1 & 0x0F)
    salt, iv = props[2:2 + salt_n], props[2 + salt_n:2 + salt_n + iv_n].ljust(16, b"\0")
    if cycles == 0x3F:
        raise Unsupported("raw aes key")
    from Cryptodome.Cipher import AES
    c = AES.new(aes_key(password, salt, cycles), AES.MODE_CBC, iv)
    left, out = size, coded

    def read(n):
        nonlocal left, out
        want = min(left, max(16, n - n % 16))
        b = f.read(want)
        left -= len(b)
        b = c.decrypt(b[:len(b) - len(b) % 16]) if b else b""
        b = b[:out]
        out -= len(b)
        return b
    return read


class _Buf:
    """Exact reads over a chunked source."""

    def __init__(self, read):
        self.src, self.buf, self.pos = read, b"", 0

    def take(self, n: int) -> bytes:
        while len(self.buf) - self.pos < n:
            more = self.src(max(1 << 20, n))
            if not more:
                raise ValueError("7z data ends early")
            self.buf = self.buf[self.pos:] + more
            self.pos = 0
        b = self.buf[self.pos:self.pos + n]
        self.pos += n
        return b


def _segments(src: _Buf):
    """Cut an LZMA2 stream before every chunk that resets the dictionary and sets properties (0xE0+): each
    piece then decodes on its own. Yields (packed piece, unpacked size, starts_fresh, is_complete)."""
    cur, usize, fresh = [], 0, True
    while True:
        c = src.take(1)[0]
        if c == 0:
            break
        if c in (1, 2):
            h = src.take(2)
            n = (h[0] << 8 | h[1]) + 1
            chunk, un = bytes([c]) + h + src.take(n), n
        elif c >= 0x80:
            h = src.take(5 if c >= 0xC0 else 4)
            un = ((c & 0x1F) << 16 | h[0] << 8 | h[1]) + 1
            chunk = bytes([c]) + h + src.take((h[2] << 8 | h[3]) + 1)
        else:
            raise ValueError("corrupt LZMA2 data")
        if c >= 0xE0 and cur:
            yield b"".join(cur), usize, fresh, True
            cur, usize, fresh = [], 0, True
        elif usize >= _SEG_CAP:
            yield b"".join(cur), usize, fresh, False
            cur, usize, fresh = [], 0, False
        cur.append(chunk)
        usize += un
    if cur:
        yield b"".join(cur), usize, fresh, True


class _Sink:
    """Splits a folder's output into its member files, checking each CRC."""

    def __init__(self, members, open_out, counter):
        self.members, self.open_out, self.counter = members, open_out, counter
        self.i, self.left, self.out, self.crc = -1, 0, None, 0
        self._next()

    def _next(self):
        while True:
            if self.out is not None:
                self.out.close()
                fi, size, crc = self.members[self.i]
                if crc is not None and self.crc != crc:
                    raise ValueError(f"CRC mismatch: {fi.filename}")
            self.i += 1
            self.out, self.crc = None, 0
            if self.i >= len(self.members):
                self.left = -1
                return
            fi, self.left, _ = self.members[self.i]
            self.out = self.open_out(fi)  # None: skip this member
            if self.left:
                return

    def write(self, b):
        mv = memoryview(b)
        while len(mv):
            if self.left < 0:
                raise ValueError("7z folder holds more data than its files")
            part = mv[:self.left]
            mv = mv[len(part):]
            self.left -= len(part)
            if self.out is not None:  # skipped members are only decoded past (solid archive)
                self.crc = zlib.crc32(part, self.crc)
                self.out.write(part)
                self.counter.add(len(part), self.members[self.i][0].filename)
            if not self.left:
                self._next()

    def finish(self):
        if self.left > 0:
            raise ValueError("7z data ends early")


def fast_extract(path: str, folders: list[dict], password, open_out, counter) -> None:
    """Decode folders from fast_plan. open_out(file_info) returns a writable object (close() is called) or
    None to skip that member; LZMA2 pieces that start with a dictionary reset decode in parallel."""
    workers = os.cpu_count() or 2
    pool = ThreadPoolExecutor(workers)
    try:
        with open(path, "rb") as f:
            for fo in folders:
                f.seek(fo["offset"])
                if fo["aes"] is not None:
                    if not password:
                        raise ValueError("password required")
                    read = _aes_reader(f, fo["packsize"], fo["coded"], fo["aes"], password)
                else:
                    left = fo["packsize"]

                    def read(n, f=f):
                        nonlocal left
                        b = f.read(min(n, left))
                        left -= len(b)
                        return b
                sink = _Sink(fo["members"], open_out, counter)
                if not fo["lzma2"]:
                    while b := read(1 << 20):
                        sink.write(b)
                    sink.finish()
                    continue
                d = 2 | (fo["dict"] & 1)
                filters = [{"id": lzma.FILTER_LZMA2, "dict_size": min(d << (fo["dict"] // 2 + 11), 1 << 31)}]
                pending: collections.deque = collections.deque()
                held = 0
                stream = None

                def job(piece):
                    return lzma.decompress(piece + b"\x00", lzma.FORMAT_RAW, filters=filters)

                for piece, usize, fresh, complete in _segments(_Buf(read)):
                    if fresh and complete:
                        pending.append((pool.submit(job, piece), usize))
                        held += usize
                        while pending and (len(pending) > workers or held > _INFLIGHT):
                            fut, u = pending.popleft()
                            sink.write(fut.result())
                            held -= u
                        continue
                    while pending:  # an oversized piece: keep order, decode it as a stream here
                        sink.write(pending.popleft()[0].result())
                    held = 0
                    if fresh:
                        stream = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=filters)
                    sink.write(stream.decompress(piece + (b"\x00" if complete else b"")))
                while pending:
                    sink.write(pending.popleft()[0].result())
                sink.finish()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


# ---------------------------------------------------------------- .7z writing

_SIG = b"7z\xbc\xaf\x27\x1c\x00\x04"
_ID_COPY, _ID_LZMA2, _ID_AES = b"\x00", b"\x21", b"\x06\xf1\x07\x01"
_AES_CYCLES = 19  # 2^19 SHA-256 rounds, 7-Zip's default


def _num(n: int) -> bytes:
    """7z variable-length number: i leading 1-bits in the first byte = i extra little-endian bytes."""
    for i in range(8):
        if n < 1 << (8 * i + 7 - i):
            return bytes([((0xFF << (8 - i)) & 0xFF) | (n >> (8 * i))]) + (n & ((1 << 8 * i) - 1)).to_bytes(i, "little")
    return b"\xff" + n.to_bytes(8, "little")


def _bits(flags) -> bytes:
    out = bytearray((len(flags) + 7) // 8)
    for i, f in enumerate(flags):
        if f:
            out[i // 8] |= 0x80 >> (i % 8)
    return bytes(out)


def aes_key(password: str, salt: bytes = b"", cycles: int = _AES_CYCLES) -> bytes:
    pw = salt + password.encode("utf-16-le")
    h = hashlib.sha256()
    step = 4096
    for start in range(0, 1 << cycles, step):
        h.update(b"".join(pw + struct.pack("<Q", i) for i in range(start, min(start + step, 1 << cycles))))
    return h.digest()


class _Cbc:
    """Streaming AES-256-CBC; the tail is zero-padded to the block size, as 7zAES expects."""

    def __init__(self, key):
        from Cryptodome.Cipher import AES
        self.iv = os.urandom(16)
        self.c = AES.new(key, AES.MODE_CBC, self.iv)
        self.rest = b""

    def update(self, b: bytes) -> bytes:
        b = self.rest + b
        cut = len(b) - len(b) % 16
        self.rest = b[cut:]
        return self.c.encrypt(b[:cut]) if cut else b""

    def final(self) -> bytes:
        return self.c.encrypt(self.rest + b"\0" * (-len(self.rest) % 16)) if self.rest else b""

    def props(self) -> bytes:
        return bytes([_AES_CYCLES | 0x40, 0x0F]) + self.iv  # no salt, 16-byte IV


def _folder(coders) -> bytes:
    """Decode order: coders[0] reads the packed stream, coder i+1 reads coder i's output, the last one yields
    the file data. py7zr ignores bind pairs and relies on this order (the layout it writes itself)."""
    b = _num(len(coders))
    for cid, props in coders:
        b += bytes([len(cid) | (0x20 if props else 0)]) + cid + (_num(len(props)) + props if props else b"")
    for i in range(len(coders) - 1):
        b += _num(i + 1) + _num(i)  # bind pair: in-stream i+1 <- out-stream i
    return b


def _streams_info(pack_pos, pack_size, coders, unpack_sizes, crc=None) -> bytes:
    b = b"\x06" + _num(pack_pos) + _num(1) + b"\x09" + _num(pack_size) + b"\x00"
    b += b"\x07\x0b" + _num(1) + b"\x00" + _folder(coders) + b"\x0c" + b"".join(_num(s) for s in unpack_sizes)
    if crc is not None:
        b += b"\x0a\x01" + struct.pack("<I", crc)
    return b + b"\x00\x00"  # end of UnpackInfo, end of StreamsInfo


def _filetime(t: float) -> int:
    return int((t + 11644473600) * 10_000_000)


def write_7z(out_path: str, items, preset: int, store_only: bool, password, counter) -> None:
    """items: (path, arcname) with folders ending in '/'. counter.add(n, name) reports read bytes and may
    raise to cancel."""
    entries, streams = [], []
    for p, n in items:
        st = os.stat(p)
        is_dir = n.endswith("/")
        entries.append((n.rstrip("/"), is_dir, st.st_mtime, 0x10 if is_dir else 0x20, not is_dir and st.st_size > 0))
        if not is_dir and st.st_size > 0:
            streams.append((p, n))
    total = sum(os.path.getsize(p) for p, _ in streams)
    key = aes_key(password) if password else None
    sizes, crcs = [], []
    with open(out_path, "wb") as f:
        f.write(b"\0" * 32)
        pack = coded = 0
        cbc = _Cbc(key) if key else None

        def emit(b):
            nonlocal pack, coded
            coded += len(b)
            if cbc:
                b = cbc.update(b)
            f.write(b)
            pack += len(b)

        def blocks(size):
            buf = bytearray()
            for p, n in streams:
                crc, got = 0, 0
                with open(p, "rb") as src:
                    while chunk := src.read(1 << 20):
                        crc, got = zlib.crc32(chunk, crc), got + len(chunk)
                        buf += chunk
                        counter.add(len(chunk), n)
                        while len(buf) >= size:
                            yield bytes(buf[:size])
                            del buf[:size]
                sizes.append(got)
                crcs.append(crc)
            if buf:
                yield bytes(buf)

        coders = []
        if streams:
            if store_only:
                for b in blocks(1 << 20):
                    emit(b)
                coders.append((_ID_COPY, b""))
            else:
                block, workers = plan(preset, total)
                jobs = _Ordered(_raw_lzma2(preset), workers)
                try:
                    for b in blocks(block):
                        for c in jobs.submit(b):
                            emit(c)
                    for c in jobs.drain():
                        emit(c)
                finally:
                    jobs.close()
                emit(b"\x00")  # LZMA2 end marker
                coders.append((_ID_LZMA2, bytes([_dict_prop(_DICT[preset])])))
            if cbc:
                tail = cbc.final()
                f.write(tail)
                pack += len(tail)
                coders.insert(0, (_ID_AES, cbc.props()))
            unpack_sizes = ([coded] if cbc else []) + [sum(sizes)]  # one per coder output, in coder order

        hdr = b"\x01"
        if streams:
            hdr += b"\x04" + _streams_info(0, pack, coders, unpack_sizes)[:-1]
            hdr += b"\x08\x0d" + _num(len(sizes))
            if len(sizes) > 1:
                hdr += b"\x09" + b"".join(_num(s) for s in sizes[:-1])
            hdr += b"\x0a\x01" + b"".join(struct.pack("<I", c) for c in crcs) + b"\x00\x00"
        hdr += b"\x05" + _num(len(entries))
        props = []
        if any(not e[4] for e in entries):
            props.append((0x0E, _bits([not e[4] for e in entries])))
            empties = [e for e in entries if not e[4]]
            if any(not e[1] for e in empties):
                props.append((0x0F, _bits([not e[1] for e in empties])))
        props.append((0x11, b"\x00" + b"".join(e[0].encode("utf-16-le") + b"\0\0" for e in entries)))
        props.append((0x14, b"\x01\x00" + b"".join(struct.pack("<Q", _filetime(e[2])) for e in entries)))
        props.append((0x15, b"\x01\x00" + b"".join(struct.pack("<I", e[3]) for e in entries)))
        for pid, data in props:
            hdr += _num(pid) + _num(len(data)) + data
        hdr += b"\x00\x00"

        if key:  # header encrypted: names stay hidden until the password is given. AES alone, not LZMA2+AES:
            hc = _Cbc(key)  # py7zr fails on a compressed+encrypted header (7-Zip/Bandizip read both)
            enc = hc.update(hdr) + hc.final()
            f.write(enc)
            nxt = b"\x17" + _streams_info(pack, len(enc), [(_ID_AES, hc.props())], [len(hdr)], zlib.crc32(hdr))
            nxt_off = pack + len(enc)
        else:
            nxt, nxt_off = hdr, pack
        f.write(nxt)
        start = struct.pack("<QQI", nxt_off, len(nxt), zlib.crc32(nxt))
        f.seek(0)
        f.write(_SIG + struct.pack("<I", zlib.crc32(start)) + start)
