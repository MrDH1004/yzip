"""Archive formats besides ZIP. FORMATS drives the compress dialog; open_any() gives every readable format the
same API as ziplib.Archive (entries / needs_password / extract / test / close / context manager).

create: zip (ziplib), 7z (lzmamt: multi-threaded LZMA2, AES-256 + encrypted names with a password), tar /
tar.gz / tar.bz2 / tar.xz / tar.zst (tarfile; xz via lzmamt), and gz / bz2 / xz / zst for a single file. Reading detects the format from the
file's first bytes, not its extension. RAR is not supported: it cannot be written and reading needs unrar.
"""
from __future__ import annotations

import bz2
import gzip
import lzma
import os
import tarfile
import time
import zipfile
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import lzmamt
import ziplib
from ziplib import BadPassword, Cancelled, Entry

try:
    from compression import zstd
except ImportError:  # Python < 3.14
    zstd = None

Progress = Callable[[int, int, str], None]


@dataclass(frozen=True)
class Fmt:
    id: str
    label: str
    ext: str
    password: bool = False
    split: bool = False
    single: bool = False  # compresses exactly one file (a stream, not an archive)


FORMATS = [
    Fmt("zip", "ZIP", ".zip", password=True, split=True),
    Fmt("7z", "7Z", ".7z", password=True),
    Fmt("tar", "TAR (압축 안 함)", ".tar"),
    Fmt("tgz", "TAR.GZ", ".tar.gz"),
    Fmt("tbz", "TAR.BZ2", ".tar.bz2"),
    Fmt("txz", "TAR.XZ", ".tar.xz"),
    Fmt("tzst", "TAR.ZST", ".tar.zst"),
    Fmt("gz", "GZ (파일 1개)", ".gz", single=True),
    Fmt("bz2", "BZ2 (파일 1개)", ".bz2", single=True),
    Fmt("xz", "XZ (파일 1개)", ".xz", single=True),
    Fmt("zst", "ZST (파일 1개)", ".zst", single=True),
]
if zstd is None:
    FORMATS = [f for f in FORMATS if f.id not in ("tzst", "zst")]
BY_ID = {f.id: f for f in FORMATS}

# what "압축 풀기" handles in Explorer and in the open dialog (one registry key per final extension)
READ_EXTS = [".zip", ".7z", ".tar", ".tgz", ".gz", ".tbz", ".tbz2", ".bz2", ".txz", ".xz", ".tzst", ".zst"]
OPEN_FILTER = "압축 파일 (" + " ".join("*" + e for e in READ_EXTS + [".001"]) + ");;모든 파일 (*.*)"
_MULTI_EXTS = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst")


def stem(path: str) -> str:
    """Archive name without its format extension: a.tar.gz -> a, a.zip.001 -> a, a.zip -> a."""
    name = os.path.basename(path)
    if ziplib.is_split_first(name):
        name = name[:-4]
    low = name.lower()
    for e in _MULTI_EXTS:
        if low.endswith(e):
            return name[:-len(e)]
    return os.path.splitext(name)[0]


def with_format(path: str, fmt_id: str) -> str:
    """Same folder and name, extension of fmt_id ("b.zip" -> "b.tar.gz")."""
    return os.path.join(os.path.dirname(path), stem(path) + BY_ID[fmt_id].ext)


# ---------- detection ----------

def detect(path: str) -> str:
    """'zip' | '7z' | 'tar' (any compression) | 'gz' | 'bz2' | 'xz' | 'zst'; ValueError if none of these."""
    if ziplib.is_split_first(path):
        return "zip"
    with open(path, "rb") as f:
        head = f.read(8)
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    try:
        with tarfile.open(path, "r:*"):
            return "tar"
    except Exception:  # not a tar; each decompressor raises its own error types
        pass
    if head[:2] == b"\x1f\x8b":
        return "gz"
    if head[:3] == b"BZh":
        return "bz2"
    if head[:6] == b"\xfd7zXZ\x00":
        return "xz"
    if head[:4] == b"\x28\xb5\x2f\xfd" and zstd is not None:
        return "zst"
    if zipfile.is_zipfile(path):  # e.g. a ZIP behind a self-extractor stub
        return "zip"
    raise ValueError("지원하지 않는 압축 형식입니다.")


def open_any(path: str, password: Optional[str] = None):
    kind = detect(path)
    if kind == "zip":
        return ziplib.Archive(path, password)
    if kind == "7z":
        return SevenZArchive(path, password)
    if kind == "tar":
        return TarArchive(path)
    return StreamArchive(path, kind)


def is_zip(path: str) -> bool:
    try:
        return detect(path) == "zip"
    except (OSError, ValueError):
        return False


# ---------- shared extraction helpers ----------

class _Planner:
    """Maps archive names to safe targets under dest (no traversal) and applies the overwrite policy:
    True replaces, False skips existing files, "rename" picks "name (2).ext"."""

    def __init__(self, dest: str, overwrite):
        self.root = os.path.realpath(dest)
        os.makedirs(self.root, exist_ok=True)
        self.overwrite = overwrite
        self.taken: set[str] = set()

    def target(self, name: str, is_dir: bool) -> Optional[str]:
        rel = ziplib._safe_rel(name)
        if not rel:
            return None
        t = os.path.realpath(os.path.join(self.root, *rel.split("/")))
        if os.path.commonpath([self.root, t]) != self.root:
            return None
        if is_dir:
            os.makedirs(t, exist_ok=True)
            return t
        if os.path.exists(t) or t.lower() in self.taken:
            if self.overwrite is False:
                return None
            if self.overwrite == "rename":
                base, ext = os.path.splitext(t)
                i = 2
                while os.path.exists(f"{base} ({i}){ext}") or f"{base} ({i}){ext}".lower() in self.taken:
                    i += 1
                t = f"{base} ({i}){ext}"
        self.taken.add(t.lower())
        return t


class _Counter:
    def __init__(self, total: int, progress: Optional[Progress], cancel):
        self.done, self.total, self.progress, self.cancel = 0, total, progress, cancel

    def add(self, n: int, name: str):
        self.done += n
        if self.progress:
            self.progress(min(self.done, self.total), self.total, name)
        if self.cancel and self.cancel():
            raise Cancelled()


def _copy_out(src, target: str, counter: _Counter, name: str, mtime: Optional[float]):
    os.makedirs(os.path.dirname(target), exist_ok=True)
    try:
        with open(target, "wb") as dst:
            while True:
                buf = src.read(1 << 20)
                if not buf:
                    break
                dst.write(buf)
                counter.add(len(buf), name)
    except BaseException:
        if os.path.exists(target):
            os.remove(target)
        raise
    if mtime:
        try:
            os.utime(target, (mtime, mtime))
        except (OverflowError, ValueError, OSError):
            pass


class _Base:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def close(self):
        pass

    def needs_password(self) -> bool:
        return False


# ---------- tar (plain or compressed) ----------

_TAR_LABEL = {b"\x1f\x8b": "TAR.GZ", b"BZ": "TAR.BZ2", b"\xfd7": "TAR.XZ", b"\x28\xb5": "TAR.ZST"}


class _TarStream:
    """tarfile in stream mode over an already decompressed reader; closes the reader too."""

    def __init__(self, raw):
        self.raw = raw
        self.tf = tarfile.open(fileobj=raw, mode="r|")

    def __iter__(self):
        return iter(self.tf)

    def extractfile(self, m):
        return self.tf.extractfile(m)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.tf.close()
        self.raw.close()


class TarArchive(_Base):
    def __init__(self, path: str):
        self.path = path
        with open(path, "rb") as f:
            self.label = _TAR_LABEL.get(f.read(2), "TAR")
        with self._open() as tf:
            # only files and folders: links and devices are never extracted
            self.members = [m for m in tf if m.isreg() or m.isdir()]

    def _open(self):
        """Forward-only reader; .tar.xz made of several blocks (xz -T, yzip) decodes them in parallel."""
        if self.label == "TAR.XZ":
            return _TarStream(lzmamt.open_xz(self.path))
        return tarfile.open(self.path, "r:*")

    @staticmethod
    def _fix(name: str) -> str:
        """Names that are not UTF-8 (Bandizip and old Windows tools write CP949) come back with surrogate
        escapes; decode those bytes as CP949, as ziplib.fix_name does for ZIP."""
        try:
            name.encode("utf-8")
            return name
        except UnicodeEncodeError:
            raw = name.encode("utf-8", "surrogateescape")
            try:
                return raw.decode("cp949")
            except UnicodeDecodeError:
                return raw.decode("utf-8", "replace")

    def _name(self, m) -> str:
        return self._fix(m.name).rstrip("/") + ("/" if m.isdir() else "")

    def entries(self) -> list[Entry]:
        return [Entry(self._name(m), m.isdir(), m.size, m.size, time.localtime(m.mtime)[:6], self.label, False, 0)
                for m in self.members]

    def extract(self, dest, names=None, progress=None, cancel=None, overwrite=True):
        wanted = None if names is None else set(names)
        sel = [m for m in self.members if wanted is None or self._name(m) in wanted]
        plan = _Planner(dest, overwrite)
        counter = _Counter(sum(m.size for m in sel if m.isreg()), progress, cancel)
        chosen = {id(m) for m in sel}
        order = iter(self.members)
        with self._open() as tf:
            for m in tf:  # archive order: a compressed tar is only read forwards
                if not (m.isreg() or m.isdir()):
                    continue
                if id(next(order)) not in chosen:
                    continue
                try:
                    tarfile.data_filter(m, plan.root)  # refuses absolute / outside / special members
                except tarfile.FilterError:
                    continue
                t = plan.target(self._name(m), m.isdir())
                if t is None or m.isdir():
                    continue
                with tf.extractfile(m) as src:
                    _copy_out(src, t, counter, self._name(m), m.mtime)

    def test(self, progress=None, cancel=None) -> list[str]:
        counter = _Counter(sum(m.size for m in self.members if m.isreg()), progress, cancel)
        bad = []
        try:
            with self._open() as tf:
                for m in tf:
                    if not m.isreg():
                        continue
                    try:
                        with tf.extractfile(m) as src:
                            while buf := src.read(1 << 20):
                                counter.add(len(buf), m.name)
                    except Cancelled:
                        raise
                    except Exception:
                        bad.append(m.name)
                        break  # the stream after a damaged member cannot be trusted
        except Cancelled:
            raise
        except Exception:
            bad.append(os.path.basename(self.path))
        return bad


# ---------- single compressed file (.gz / .bz2 / .xz / .zst) ----------

def _stream_open(kind: str, path: str):
    if kind == "gz":
        return gzip.open(path, "rb")
    if kind == "bz2":
        return bz2.open(path, "rb")
    if kind == "xz":
        return lzmamt.open_xz(path)
    return zstd.open(path, "rb")


class StreamArchive(_Base):
    def __init__(self, path: str, kind: str):
        self.path, self.kind = path, kind
        self.name = stem(path) or "data"
        self.csize = os.path.getsize(path)
        self.size = self.csize
        if kind == "gz" and self.csize >= 18:  # gzip trailer keeps the size (mod 4 GiB)
            with open(path, "rb") as f:
                f.seek(-4, 2)
                self.size = int.from_bytes(f.read(4), "little")
        self.mtime = os.path.getmtime(path)

    def entries(self) -> list[Entry]:
        return [Entry(self.name, False, self.size, self.csize, time.localtime(self.mtime)[:6], self.kind.upper(),
                      False, 0)]

    def extract(self, dest, names=None, progress=None, cancel=None, overwrite=True):
        if names is not None and self.name not in names:
            return
        plan = _Planner(dest, overwrite)
        t = plan.target(self.name, False)
        if t is None:
            return
        with _stream_open(self.kind, self.path) as src:
            _copy_out(src, t, _Counter(self.size, progress, cancel), self.name, self.mtime)

    def test(self, progress=None, cancel=None) -> list[str]:
        counter = _Counter(self.size, progress, cancel)
        try:
            with _stream_open(self.kind, self.path) as src:
                while buf := src.read(1 << 20):
                    counter.add(len(buf), self.name)
        except Cancelled:
            raise
        except Exception:
            return [self.name]
        return []


# ---------- 7z ----------

class _NoCount:
    def add(self, n, name):
        pass


class _Discard:
    def write(self, b):
        pass

    def close(self):
        pass


class SevenZArchive(_Base):
    def __init__(self, path: str, password: Optional[str] = None):
        import py7zr
        self.path, self.password = path, password or None
        try:
            with py7zr.SevenZipFile(path, password=self.password) as z:
                self.infos = z.list()
                self.need = z.needs_password()
        except py7zr.exceptions.PasswordRequired as e:
            raise BadPassword(str(e))  # names are encrypted too: nothing is readable without the password
        except FileNotFoundError:
            raise
        except py7zr.exceptions.Bad7zFile:
            if self.password and self._header_encrypted(py7zr):  # wrong key: the decrypted header is garbage
                raise BadPassword("wrong password")
            raise
        except Exception as e:
            if self.password:  # a wrong key turns the encrypted header into garbage
                raise BadPassword(str(e))
            raise

    def _header_encrypted(self, py7zr) -> bool:
        try:
            with py7zr.SevenZipFile(self.path):
                return False
        except py7zr.exceptions.PasswordRequired:
            return True
        except Exception:
            return False

    def _name(self, fi) -> str:
        return fi.filename.replace("\\", "/").rstrip("/") + ("/" if fi.is_directory else "")

    def entries(self) -> list[Entry]:
        out = []
        for fi in self.infos:
            mt = fi.creationtime.timetuple()[:6] if fi.creationtime else (1980, 1, 1, 0, 0, 0)
            out.append(Entry(self._name(fi), fi.is_directory, fi.uncompressed or 0, fi.compressed or 0, mt, "7Z",
                             self.need and not fi.is_directory, fi.crc32 or 0))
        return out

    def needs_password(self) -> bool:
        return self.need

    def _run(self, fn):
        """Run a py7zr read; with an encrypted archive a wrong key surfaces as random decoder errors."""
        import py7zr
        try:
            return fn(py7zr)
        except (Cancelled, KeyboardInterrupt, OSError):
            raise
        except Exception as e:
            if self.need:
                raise BadPassword(str(e))
            raise

    def extract(self, dest, names=None, progress=None, cancel=None, overwrite=True):
        from py7zr.io import NullIO, Py7zIO, WriterFactory
        wanted = None if names is None else set(names)
        sel = [fi for fi in self.infos if wanted is None or self._name(fi) in wanted]
        plan = _Planner(dest, overwrite)
        targets = {}
        for fi in sel:
            t = plan.target(fi.filename, fi.is_directory)
            if t and not fi.is_directory:
                targets[fi.filename] = (t, fi)
        if not targets:
            return
        counter = _Counter(sum(fi.uncompressed or 0 for _, fi in targets.values()), progress, cancel)
        opened: dict[str, object] = {}

        class Out(Py7zIO):
            def __init__(self, name, path):
                self.name, self.path = name, path
                os.makedirs(os.path.dirname(path), exist_ok=True)
                self.f = open(path, "w+b")
                self.n = 0

            def write(self, s):
                self.f.write(s)
                self.n += len(s)
                counter.add(len(s), self.name)
                return len(s)

            def read(self, size=None):
                return self.f.read(size if size is not None else -1)

            def seek(self, offset, whence=0):
                return self.f.seek(offset, whence)

            def flush(self):
                self.f.flush()

            def size(self):
                return self.n

            def close(self):
                if not self.f.closed:
                    self.f.close()

        class Factory(WriterFactory):
            def create(self, filename):
                hit = targets.get(filename)
                if hit is None:
                    return NullIO()
                w = Out(filename, hit[0])
                opened[filename] = w
                return w

        def fast_out(fi):
            hit = targets.get(fi.filename)
            if hit is None:
                return None
            w = Out(fi.filename, hit[0])
            opened[fi.filename] = w
            return w

        def go(py7zr):
            with py7zr.SevenZipFile(self.path, password=self.password) as z:
                try:
                    folders = lzmamt.fast_plan(z)
                except lzmamt.Unsupported:
                    folders = None
                if folders is None:  # unusual coder layout: py7zr's own (single-threaded) decoder
                    z.extract(targets=list(targets), factory=Factory())
            if folders is not None:  # Out counts progress itself; the sink only reports via the counter
                lzmamt.fast_extract(self.path, folders, self.password, fast_out, _NoCount())
        try:
            self._run(go)
        except BaseException:
            for w in opened.values():  # never leave half-written files behind
                w.close()
                if os.path.exists(w.path):
                    os.remove(w.path)
            raise
        for fname, (t, fi) in targets.items():
            w = opened.get(fname)
            if w is None:  # empty members get no writer
                open(t, "wb").close()
            else:
                w.close()
            if fi.creationtime:
                ts = fi.creationtime.timestamp()
                try:
                    os.utime(t, (ts, ts))
                except (OverflowError, ValueError, OSError):
                    pass

    def test(self, progress=None, cancel=None) -> list[str]:
        if progress:
            progress(0, 1, os.path.basename(self.path))

        def go(py7zr):
            with py7zr.SevenZipFile(self.path, password=self.password) as z:
                try:
                    folders = lzmamt.fast_plan(z)
                except lzmamt.Unsupported:
                    bad = z.testzip()
                    return [bad] if bad else []
            try:
                lzmamt.fast_extract(self.path, folders, self.password, lambda fi: _Discard(), _NoCount())
            except (ValueError, lzma.LZMAError) as e:
                if self.need:
                    raise BadPassword(str(e))
                return [str(e)]
            return []
        try:
            bad = self._run(go)
        except BadPassword:
            raise
        except Exception:
            bad = [os.path.basename(self.path)]
        if progress:
            progress(1, 1, os.path.basename(self.path))
        return bad


# ---------- creating ----------

class _Reader:
    """File wrapper that reports progress and honours cancel while a writer pulls data from it."""

    def __init__(self, f, counter: _Counter, name: str):
        self.f, self.counter, self.name = f, counter, name

    def read(self, n=-1):
        b = self.f.read(n)
        if b:
            self.counter.add(len(b), self.name)
        return b


def _zstd_options(level: int):
    lv = max(1, round(1 + (level - 1) * 18 / 8))  # 1..9 -> zstd 1..19
    return {zstd.CompressionParameter.compression_level: lv,
            zstd.CompressionParameter.nb_workers: min(os.cpu_count() or 2, 16)}


def create(fmt_id: str, paths: Iterable[str], out_path: str, level: int = 6, password: Optional[str] = None,
           store_only: bool = False, split_size: int = 0, exclude: Iterable[str] = (),
           progress: Optional[Progress] = None, cancel: Optional[Callable[[], bool]] = None) -> list[str]:
    """Create an archive of format fmt_id. Returns the produced files (several only for a split ZIP)."""
    fmt = BY_ID[fmt_id]
    if fmt_id == "zip":
        return ziplib.create_zip(paths, out_path, level=level, password=password, store_only=store_only,
                                 split_size=split_size, exclude=exclude, progress=progress, cancel=cancel)
    if password and not fmt.password:
        raise ValueError(f"{fmt.label} 형식은 암호를 지원하지 않습니다.")
    items = ziplib.collect_items(paths, exclude)
    if fmt.single and (len(items) != 1 or items[0][1].endswith("/")):
        raise ValueError(f"{fmt.label} 형식은 파일 하나만 압축할 수 있습니다. 여러 개는 TAR.GZ 등을 쓰세요.")
    level = 1 if store_only else max(1, min(9, level))
    counter = _Counter(sum(os.path.getsize(p) for p, n in items if not n.endswith("/")), progress, cancel)
    tmp = out_path + ".partial"
    try:
        if fmt_id == "7z":
            _create_7z(items, tmp, level, store_only, password, counter)
        elif fmt.single:
            _create_stream(fmt_id, items[0], tmp, out_path, level, counter)
        else:
            _create_tar(fmt_id, items, tmp, level, counter)
        os.replace(tmp, out_path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return [out_path]


def _create_tar(fmt_id, items, tmp, level, counter):
    if fmt_id == "txz":  # multi-threaded xz under a streaming tar writer
        with open(tmp, "wb") as raw, lzmamt.XZWriter(raw, level, counter.total) as xz:
            _fill_tar(tarfile.open(fileobj=xz, mode="w|", format=tarfile.PAX_FORMAT), items, counter)
        return
    mode = {"tar": "w", "tgz": "w:gz", "tbz": "w:bz2", "tzst": "w:zst"}[fmt_id]
    kw = {}
    if fmt_id in ("tgz", "tbz"):
        kw["compresslevel"] = level
    elif fmt_id == "tzst":
        kw["options"] = _zstd_options(level)
    _fill_tar(tarfile.open(tmp, mode, format=tarfile.PAX_FORMAT, **kw), items, counter)


def _fill_tar(tf, items, counter):
    with tf:
        for p, n in items:
            if counter.cancel and counter.cancel():
                raise Cancelled()
            ti = tf.gettarinfo(p, n.rstrip("/"))
            if ti.isreg():
                with open(p, "rb") as f:
                    tf.addfile(ti, _Reader(f, counter, n))
            else:
                tf.addfile(ti)


def _create_stream(fmt_id, item, tmp, out_path, level, counter):
    p, n = item
    with open(p, "rb") as src, open(tmp, "wb") as raw:
        if fmt_id == "gz":  # the header keeps the original name (without .gz), not the temp file's name
            dst = gzip.GzipFile(filename=os.path.basename(out_path), mode="wb", fileobj=raw, compresslevel=level,
                                mtime=int(os.path.getmtime(p)))
        elif fmt_id == "bz2":
            dst = bz2.BZ2File(raw, "wb", compresslevel=level)
        elif fmt_id == "xz":
            dst = lzmamt.XZWriter(raw, level, counter.total)
        else:
            dst = zstd.ZstdFile(raw, "wb", options=_zstd_options(level))
        with dst:
            while buf := src.read(1 << 20):
                dst.write(buf)
                counter.add(len(buf), n)


def _create_7z(items, tmp, level, store_only, password, counter):
    lzmamt.write_7z(tmp, items, level, store_only, password, counter)
