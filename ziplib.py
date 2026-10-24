"""ZIP core logic: create / list / extract / test, AES encryption, CP949 name repair, raw split volumes."""
from __future__ import annotations

import os
import re
import shutil
import struct
import tempfile
import threading
import time
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import zipwriter

pyzipper = None  # imported by Archive on first use

Progress = Callable[[int, int, str], None]  # done_bytes, total_bytes, current_name


class Cancelled(Exception):
    pass


class BadPassword(Exception):
    pass


@dataclass
class Entry:
    name: str
    is_dir: bool
    size: int
    csize: int
    mtime: tuple
    method: str
    encrypted: bool
    crc: int


_METHODS = {0: "Store", 8: "Deflate", 12: "BZip2", 14: "LZMA"}


def fix_name(info: zipfile.ZipInfo) -> str:
    """Names without the UTF-8 flag are decoded as cp437 by zipfile; Korean archives are usually cp949."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("cp949")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def _safe_rel(name: str) -> str:
    name = name.replace("\\", "/")
    parts = [p for p in name.split("/") if p not in ("", ".", "..")]
    return "/".join(parts)


# ---------- split volumes (name.zip.001, .002 ...) ----------

_SPLIT_RE = re.compile(r"^(?P<base>.+)\.(?P<num>\d{3})$")


def is_split_first(path: str) -> bool:
    m = _SPLIT_RE.match(path)
    return bool(m and m.group("num") == "001")


def join_split(first_part: str) -> str:
    """Join name.zip.001.. into a temp file and return its path (caller deletes)."""
    base = _SPLIT_RE.match(first_part).group("base")
    fd, tmp = tempfile.mkstemp(suffix=".zip")
    with os.fdopen(fd, "wb") as out:
        i = 1
        while os.path.exists(f"{base}.{i:03d}"):
            with open(f"{base}.{i:03d}", "rb") as f:
                shutil.copyfileobj(f, out, 1 << 20)
            i += 1
    return tmp


def split_file(path: str, volume_size: int) -> list[str]:
    parts = []
    with open(path, "rb") as src:
        i = 1
        while True:
            chunk = src.read(volume_size)
            if not chunk:
                break
            p = f"{path}.{i:03d}"
            with open(p, "wb") as dst:
                dst.write(chunk)
            parts.append(p)
            i += 1
    os.remove(path)
    return parts


# ---------- reading ----------


class Archive:
    def __init__(self, path: str, password: Optional[str] = None):
        self.path = path
        self._tmp = None
        real = path
        if is_split_first(path):
            real = self._tmp = join_split(path)
        self._real = real
        global pyzipper
        import pyzipper  # ~75 ms (Cryptodome, cffi): only reading needs it, not the right-click compress path
        self.zf = pyzipper.AESZipFile(real)
        self._names = {fix_name(i): i for i in self.zf.infolist()}
        self.set_password(password)

    def close(self):
        self.zf.close()
        if self._tmp and os.path.exists(self._tmp):
            os.remove(self._tmp)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def set_password(self, pw: Optional[str]):
        """Non-ASCII passwords: UTF-8 is the WinZip-AES standard, but Bandizip (REA-verified with
        bz.exe 7.46) derives the key from the ANSI/cp949 bytes, so fall back to that on a mismatch."""
        if not pw:
            self.zf.setpassword(None)
            return
        candidates = [pw.encode("utf-8")]
        if not pw.isascii():
            try:
                candidates.append(pw.encode("cp949"))
            except UnicodeEncodeError:
                pass
        probe = next((i for i in self._names.values() if i.flag_bits & 1 and not i.is_dir()), None)
        for cand in candidates:
            self.zf.setpassword(cand)
            if probe is None or len(candidates) == 1:
                return
            try:
                with self.zf.open(probe) as f:
                    f.read()  # CRC / HMAC check fails on a wrong key
                return
            except (RuntimeError, zipfile.BadZipFile, pyzipper.BadZipFile):
                continue
        self.zf.setpassword(candidates[0])

    def entries(self) -> list[Entry]:
        out = []
        for name, i in self._names.items():
            ctype = i.compress_type
            if ctype == 99:  # WinZip AES wraps the real method
                method = "AES"
            else:
                method = _METHODS.get(ctype, str(ctype))
            out.append(Entry(name, i.is_dir(), i.file_size, i.compress_size, i.date_time,
                             method, bool(i.flag_bits & 1), i.CRC))
        return out

    def needs_password(self) -> bool:
        return any(i.flag_bits & 1 for i in self._names.values())

    def extract(self, dest: str, names: Optional[Iterable[str]] = None,
                progress: Optional[Progress] = None, cancel: Optional[Callable[[], bool]] = None,
                overwrite=True):
        """overwrite: True replaces, False skips existing files, "rename" writes "name (2).ext" next to them."""
        wanted = None if names is None else set(names)
        sel = [n for n in self._names if wanted is None or n in wanted]
        total = sum(self._names[n].file_size for n in sel)
        root = os.path.realpath(dest)
        os.makedirs(root, exist_ok=True)
        jobs, skipped = [], 0
        taken = set()  # targets already handed out in this run (rename mode)

        def free_name(target):
            if not os.path.exists(target) and target.lower() not in taken:
                return target
            base, ext = os.path.splitext(target)
            i = 2
            while os.path.exists(f"{base} ({i}){ext}") or f"{base} ({i}){ext}".lower() in taken:
                i += 1
            return f"{base} ({i}){ext}"
        for n in sel:
            info = self._names[n]
            rel = _safe_rel(n)
            if not rel:
                continue
            target = os.path.realpath(os.path.join(root, *rel.split("/")))
            if os.path.commonpath([root, target]) != root:  # zip-slip guard
                continue
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
            elif overwrite is False and os.path.exists(target):
                skipped += info.file_size
            else:
                if overwrite == "rename":
                    target = free_name(target)
                    taken.add(target.lower())
                jobs.append((n, info, target))

        state = {"done": skipped}
        lock = threading.Lock()
        stop = threading.Event()
        local = threading.local()
        handles = []
        pwd = self.zf.pwd

        def handle():  # one zip handle per worker thread; handles are not thread-safe
            z = getattr(local, "z", None)
            if z is None:
                z = local.z = pyzipper.AESZipFile(self._real)
                z.setpassword(pwd)
                with lock:
                    handles.append(z)
            return z

        def raw_file():
            f = getattr(local, "f", None)
            if f is None:
                f = local.f = open(self._real, "rb")
                with lock:
                    handles.append(f)
            return f

        def chunks(info):
            if info.flag_bits & 1 or info.compress_type not in (0, 8):
                with handle().open(info) as src:  # encrypted / other methods: pyzipper
                    while True:
                        buf = src.read(1 << 20)
                        if not buf:
                            return
                        yield buf
            # Plain store/deflate: read the member's bytes directly. zlib, crc32 and file I/O all release the
            # GIL, so the worker threads really run in parallel (pyzipper's Python read path did not).
            f = raw_file()
            f.seek(info.header_offset)
            head = f.read(30)
            if head[:4] != b"PK\x03\x04":
                raise zipfile.BadZipFile(f"local header missing: {info.filename}")
            n_len, x_len = struct.unpack("<HH", head[26:30])
            f.seek(info.header_offset + 30 + n_len + x_len)
            left, crc, size = info.compress_size, 0, 0
            dec = zlib.decompressobj(-15) if info.compress_type == 8 else None
            while left:
                data = f.read(min(left, 4 << 20))
                if not data:
                    raise zipfile.BadZipFile(f"truncated data: {info.filename}")
                left -= len(data)
                while data:
                    if dec:
                        out = dec.decompress(data, 8 << 20)  # bounded output even for extreme ratios
                        data = dec.unconsumed_tail
                    else:
                        out, data = data, b""
                    if out:
                        crc = zlib.crc32(out, crc)
                        size += len(out)
                        yield out
            if dec:
                out = dec.flush()
                if out:
                    crc = zlib.crc32(out, crc)
                    size += len(out)
                    yield out
            if crc != info.CRC or size != info.file_size:
                raise zipfile.BadZipFile(f"CRC mismatch: {info.filename}")

        def one(job):
            n, info, target = job
            if stop.is_set():
                return
            if cancel and cancel():
                raise Cancelled()
            os.makedirs(os.path.dirname(target), exist_ok=True)
            try:
                with open(target, "wb") as dst:
                    for buf in chunks(info):
                        dst.write(buf)
                        with lock:
                            state["done"] += len(buf)
                            d = state["done"]
                        if progress:
                            progress(d, total, n)
                        if stop.is_set() or (cancel and cancel()):
                            raise Cancelled()
            except (RuntimeError, zipfile.BadZipFile, pyzipper.BadZipFile, Cancelled) as e:
                if os.path.exists(target):
                    os.remove(target)
                if isinstance(e, Cancelled):
                    raise
                if info.flag_bits & 1 and ("password" in str(e).lower() or "Bad" in str(e)):
                    raise BadPassword(str(e))  # only an encrypted member can fail because of the password
                raise
            try:
                ts = time.mktime(info.date_time + (0, 0, -1))
                os.utime(target, (ts, ts))
            except (OverflowError, ValueError, OSError):
                pass

        workers = max(1, min(os.cpu_count() or 2, 8, len(jobs)))
        try:
            with ThreadPoolExecutor(workers) as pool:
                futures = [pool.submit(one, j) for j in jobs]
                try:
                    for f in futures:
                        f.result()
                except BaseException:
                    stop.set()  # remaining queued jobs return immediately
                    raise
        finally:
            for z in handles:
                z.close()

    def test(self, progress: Optional[Progress] = None,
             cancel: Optional[Callable[[], bool]] = None) -> list[str]:
        """Return names of broken entries (empty list = archive is OK)."""
        bad = []
        total = sum(i.file_size for i in self._names.values())
        done = 0
        for n, info in self._names.items():
            if cancel and cancel():
                raise Cancelled()
            if info.is_dir():
                continue
            try:
                with self.zf.open(info) as f:
                    while True:
                        buf = f.read(1 << 20)
                        if not buf:
                            break
                        done += len(buf)
                        if progress:
                            progress(done, total, n)
            except RuntimeError as e:
                if "password" in str(e).lower():
                    raise BadPassword(str(e))
                bad.append(n)
            except (zipfile.BadZipFile, pyzipper.BadZipFile, EOFError, OSError):
                bad.append(n)
        return bad


# ---------- writing ----------


def _collect(paths: Iterable[str]) -> list[tuple[str, str]]:
    """(absolute path, archive name) pairs; folders are walked, empty folders kept."""
    items = []
    for p in paths:
        p = os.path.abspath(p)
        base = os.path.basename(p.rstrip("\\/"))
        if os.path.isdir(p):
            items.append((p, base + "/"))
            for dp, dns, fns in os.walk(p):
                rel_dir = os.path.relpath(dp, os.path.dirname(p)).replace("\\", "/")
                for d in dns:
                    items.append((os.path.join(dp, d), f"{rel_dir}/{d}/"))
                for f in fns:
                    items.append((os.path.join(dp, f), f"{rel_dir}/{f}"))
        else:
            items.append((p, base))
    return items


def collect_items(paths: Iterable[str], exclude: Iterable[str] = ()) -> list[tuple[str, str]]:
    """_collect minus names ending in one of `exclude` (case-insensitive); shared by every output format."""
    excl = [e.lower() for e in exclude if e]
    return [(p, n) for p, n in _collect(paths) if not any(
        os.path.basename(n.rstrip("/")).lower().endswith(e) for e in excl)]


def create_zip(paths: Iterable[str], out_path: str, level: int = 6, password: Optional[str] = None,
               store_only: bool = False, split_size: int = 0,
               exclude: Iterable[str] = (), progress: Optional[Progress] = None,
               cancel: Optional[Callable[[], bool]] = None) -> list[str]:
    """Create a zip. Returns the list of produced files (several when split)."""
    items = collect_items(paths, exclude)
    tmp_out = out_path + ".partial"
    try:
        # multi-threaded; with a password the entries are WinZip AES-256 (legacy ZipCrypto is not offered)
        zipwriter.write_zip(items, tmp_out, level, store_only, progress, cancel, password=password or None)
        os.replace(tmp_out, out_path)
    except BaseException:
        if os.path.exists(tmp_out):
            os.remove(tmp_out)
        raise
    if split_size and os.path.getsize(out_path) > split_size:
        return split_file(out_path, split_size)
    return [out_path]
