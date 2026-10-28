"""Archive formats besides ZIP. FORMATS drives the compress dialog; open_any() gives every readable format the
same API as ziplib.Archive (entries / needs_password / extract / test / close / context manager).

create: zip (ziplib), 7z (lzmamt: multi-threaded LZMA2, AES-256 + encrypted names with a password), tar /
tar.gz / tar.bz2 / tar.xz / tar.zst (tarfile; xz via lzmamt), and gz / bz2 / xz / zst for a single file. Reading detects the format from the
file's first bytes, not its extension. RAR, ISO, CAB, LHA, CPIO, RPM and DEB use the bundled libarchive DLL in-process;
EGG and ALZ use the bundled Rust module in-process. Other 7-Zip-only formats remain on the compatibility reader.
"""
from __future__ import annotations

import bz2
import gzip
import lzma
import os
import runtimeconfig
import re
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

# Read-only archive formats.  The formats in LIBARCHIVE_* are handled in-process;
# the remaining 7-Zip-recognized extensions retain the legacy compatibility
# backend until a matching native reader is added.
SEVENZ_ARCHIVE_EXTS = [".rar", ".cab", ".arj", ".lzh", ".lha", ".zipx", ".z", ".taz", ".lzma", ".cpio", ".rpm",
                       ".deb"]
SEVENZ_IMAGE_EXTS = [".iso", ".img", ".udf", ".msi", ".wim", ".swm", ".esd", ".vhd", ".vhdx", ".dmg"]
LIBARCHIVE_ARCHIVE_EXTS = [".rar", ".cab", ".lzh", ".lha", ".cpio", ".rpm", ".deb"]
LIBARCHIVE_IMAGE_EXTS = [".iso", ".img", ".udf"]
NATIVE_EXT_TO_KIND = {e[1:]: e[1:] for e in LIBARCHIVE_ARCHIVE_EXTS + LIBARCHIVE_IMAGE_EXTS}
SFX_EXTS = [".exe"]  # only self-extracting archives; ordinary executables are never treated as archives
ZPAQ_EXTS = [".zpaq"]
# decoded in Python (minifmt)
MINI_EXTS = {".br": "br", ".lz": "lz", ".lz4": "lz4", ".aes": "aes", ".uu": "uu", ".uue": "uu", ".xxe": "uu",
             ".asar": "asar"}
# what "압축 풀기" handles in Explorer and in the open dialog (one registry key per final extension)
_PRIMARY_READ_EXTS = [".zip", ".zipx", ".exe", ".tar", ".tgz", ".lzh", ".iso", ".7z", ".gz", ".xz", ".zpaq"]
_OTHER_READ_EXTS = [".tbz", ".tbz2", ".bz2", ".txz", ".tzst", ".zst", ".egg", ".alz"] + \
                   SEVENZ_ARCHIVE_EXTS + list(MINI_EXTS) + SEVENZ_IMAGE_EXTS
READ_EXTS = _PRIMARY_READ_EXTS + [ext for ext in _OTHER_READ_EXTS if ext not in _PRIMARY_READ_EXTS]
# images and installers: right-clicking them still offers compressing (they are not "just an archive")
IMAGE_EXTS = list(SEVENZ_IMAGE_EXTS)
OPEN_FILTER = "압축 파일 (" + " ".join("*" + e for e in READ_EXTS + [".001", ".r00", ".a00"]) + ");;모든 파일 (*.*)"
_TYPE_LABELS = {
    ".zip": "ZIP 압축 파일", ".zipx": "ZIPX 압축 파일", ".exe": "EXE 자체 압축 실행 파일",
    ".tar": "TAR 묶음 파일", ".tgz": "TGZ 압축 파일", ".lzh": "LZH 압축 파일",
    ".iso": "ISO 디스크 이미지", ".7z": "7Z 압축 파일", ".gz": "GZ 압축 파일",
    ".xz": "XZ 압축 파일", ".zpaq": "ZPAQ 압축 파일", ".cab": "CAB 압축 파일",
}
SUPPORTED_TYPES = {ext: _TYPE_LABELS.get(ext, ext.upper().lstrip(".") + " 파일") for ext in READ_EXTS}
_MULTI_EXTS = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst")
_TAR_NAMES = _MULTI_EXTS + (".tar", ".tgz", ".tbz", ".tbz2", ".txz", ".tzst")
# volumes of one set: x.part2.rar / x.r00 (RAR), x.vol2.egg (EGG), x.a00 (ALZ), x.z01 (ZIP, Bandizip / WinZip)
_VOLUME = re.compile(r"^(.*?)(?:\.part(\d+)\.rar|\.r(\d\d)|\.vol(\d+)\.egg|\.a(\d\d)|\.z(\d\d))$", re.I)


def first_volume(path: str) -> str:
    """The file that opens a whole multi-volume set, for any of its volumes; path itself otherwise."""
    d, name = os.path.split(path)
    m = _VOLUME.match(name)
    if not m:
        return path
    base = m.group(1)
    if m.group(2):  # x.part07.rar -> x.part01.rar (same width)
        cand = f"{base}.part{1:0{len(m.group(2))}d}.rar"
    elif m.group(3):
        cand = base + ".rar"
    elif m.group(4):
        cand = base + ".vol1.egg"
    elif m.group(5):
        cand = base + ".alz"
    else:
        cand = base + ".zip"
    cand = os.path.join(d, cand)
    return cand if os.path.exists(cand) else path


def stem(path: str) -> str:
    """Archive name without its format extension: a.tar.gz -> a, a.zip.001 -> a, a.zip -> a, a.part1.rar -> a,
    a.vol1.egg -> a."""
    name = os.path.basename(path)
    if ziplib.is_split_first(name):
        name = name[:-4]
    m = _VOLUME.match(name)
    if m and (m.group(2) or m.group(4)):
        return m.group(1)
    low = name.lower()
    for e in _MULTI_EXTS:
        if low.endswith(e):
            return name[:-len(e)]
    return os.path.splitext(name)[0]


def with_format(path: str, fmt_id: str) -> str:
    """Same folder and name, extension of fmt_id ("b.zip" -> "b.tar.gz")."""
    return os.path.join(os.path.dirname(path), stem(path) + BY_ID[fmt_id].ext)


# ---------- detection ----------

def _contains_signature(path: str, signature: bytes, limit: int = 64 << 20) -> bool:
    """Find a known archive header in a bounded SFX stub without loading the whole executable."""
    overlap = max(0, len(signature) - 1)
    carry = b""
    remaining = limit
    with open(path, "rb") as f:
        while remaining:
            chunk = f.read(min(1 << 20, remaining))
            if not chunk:
                return False
            block = carry + chunk
            if signature in block:
                return True
            carry = block[-overlap:] if overlap else b""
            remaining -= len(chunk)
    return False

def detect(path: str) -> str:
    """Return the format id used by the appropriate in-process backend."""
    with open(path, "rb") as f:
        head = f.read(8)
    low = path.lower()
    if ziplib.is_split_first(path):
        return "7z" if head[:6] == b"7z\xbc\xaf\x27\x1c" else "zip"
    if head[:4] == b"EGGA":
        return "egg"
    if head[:4] == b"ALZ\x01":
        return "alz"
    if head[:7] == b"Rar!\x1a\x07\x00" or head[:8] == b"Rar!\x1a\x07\x01\x00":
        return "rar"
    if head[:4] == b"MSCF":
        return "cab"
    if head[:3] == b"zPQ" or low.endswith(tuple(ZPAQ_EXTS)):
        return "zpaq"
    if low.endswith(".zipx") or low.endswith(".zip") and os.path.exists(path[:-4] + ".z01"):
        return "7zcli"  # newer ZIP methods / a Bandizip-WinZip split set (.z01 .z02 ... .zip)
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    # before the tar probe: an ISO / disk image starts with zeroed sectors, which tarfile takes for an empty tar
    if low.endswith(tuple(SEVENZ_ARCHIVE_EXTS + SEVENZ_IMAGE_EXTS)):
        return NATIVE_EXT_TO_KIND.get(os.path.splitext(low)[1][1:], "7zcli")
    try:
        with tarfile.open(path, "r:*"):
            return "tar"
    except Exception:  # not a tar; each decompressor raises its own error types
        if path.lower().endswith(_TAR_NAMES):  # a damaged tar.xz is still a tar: report it, never unpack the
            return "tar"                      # whole stream as one file
    if head[:2] == b"\x1f\x8b":
        return "gz"
    if head[:3] == b"BZh":
        return "bz2"
    if head[:6] == b"\xfd7zXZ\x00":
        return "xz"
    if head[:4] == b"\x28\xb5\x2f\xfd" and zstd is not None:
        return "zst"
    if head[:4] == b"LZIP":
        return "lz"
    if head[:4] in (b"\x04\x22\x4d\x18", b"\x02\x21\x4c\x18"):
        return "lz4"
    if head[:3] == b"AES" and low.endswith(".aes"):
        return "aes"
    for ext, kind in MINI_EXTS.items():  # no reliable magic (Brotli, text encodings, ASAR): by name
        if low.endswith(ext):
            return kind
    if zipfile.is_zipfile(path):  # e.g. a ZIP behind a self-extractor stub
        return "zip"
    if low.endswith(tuple(SFX_EXTS)) and _contains_signature(path, b"7z\xbc\xaf\x27\x1c"):
        return "7zcli"
    raise ValueError("지원하지 않는 압축 형식입니다.")


def open_any(path: str, password: Optional[str] = None):
    path = first_volume(path)  # any volume of a set opens the whole set
    kind = detect(path)
    import nativearchive
    if kind in nativearchive.NATIVE_KINDS:
        return nativearchive.open_archive(path, kind, password)
    if kind == "7zcli":
        from cliarchive import CliArchive
        return CliArchive(path, kind, password)
    if kind == "zpaq":
        from zpaqarchive import ZpaqArchive
        return ZpaqArchive(path, password)
    if kind in ("uu", "asar"):
        import minifmt
        return minifmt.UUArchive(path) if kind == "uu" else minifmt.AsarArchive(path)
    if kind in ("br", "lz", "lz4", "aes"):
        return StreamArchive(path, kind, password)
    if kind == "zip":
        return ziplib.Archive(path, password)
    if kind == "7z":
        if ziplib.is_split_first(path):  # a.7z.001, .002 ... (7-Zip / Bandizip volumes): read the joined copy
            tmp = ziplib.join_split(path)
            try:
                a = SevenZArchive(tmp, password)
            except BaseException:
                os.remove(tmp)
                raise
            a.joined = tmp
            return a
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

    def __init__(self, dest: str, overwrite, archive: Optional[str] = None):
        self.root = os.path.realpath(dest)
        os.makedirs(self.root, exist_ok=True)
        self.overwrite = overwrite
        # the archive being read is never a target: 'backup' (a gzip without extension) would unpack onto itself
        self.taken: set[str] = {os.path.realpath(archive).lower()} if archive else set()
        self.given: dict[str, str] = {}  # target (lower) -> member name it was handed to in this run

    def target(self, name: str, is_dir: bool) -> Optional[str]:
        t = ziplib.safe_target(self.root, name)
        if t is None:
            return None
        if is_dir:
            try:
                os.makedirs(t, exist_ok=True)
            except OSError:  # a file of that name is in the way
                return None
            return t
        if self.overwrite is True and self.given.get(t.lower()) == name:
            return t  # the same member again (tar -r / -u appends updates): the later copy wins, as in ZIP
        if os.path.exists(t) or t.lower() in self.taken:
            if self.overwrite is False:
                return None
            if self.overwrite == "rename" or t.lower() in self.taken:
                base, ext = os.path.splitext(t)
                i = 2
                while os.path.exists(f"{base} ({i}){ext}") or f"{base} ({i}){ext}".lower() in self.taken:
                    i += 1
                t = f"{base} ({i}){ext}"
        self.taken.add(t.lower())
        self.given[t.lower()] = name
        return t


class _Counter:
    def __init__(self, total: int, progress: Optional[Progress], cancel):
        self.done, self.total, self.progress, self.cancel = 0, total, progress, cancel

    def add(self, n: int, name: str):
        self.done += n
        if self.progress:
            # a total that turns out too small (gzip's size is kept mod 4 GiB) grows with the data
            self.progress(self.done, max(self.total, self.done) if self.total else 0, name)
        if self.cancel and self.cancel():
            raise Cancelled()


def _copy_out(src, target: str, counter: _Counter, name: str, mtime: Optional[float], later=None):
    """Write src to target through a part file. later: a list to append (part, target, mtime) to instead of
    putting the file in place now, for formats whose checksum only comes at the end of the whole stream."""
    os.makedirs(os.path.dirname(target), exist_ok=True)
    part = ziplib.part_path(target)  # an existing file is replaced only by a complete new one
    try:
        with open(part, "wb") as dst:
            while True:
                buf = src.read(1 << 20)
                if not buf:
                    break
                dst.write(buf)
                counter.add(len(buf), name)
        if later is not None:
            later.append((part, target, mtime))
            return
        ziplib.finish_part(part, target)
    except BaseException:
        if os.path.exists(part):
            os.remove(part)
        raise
    _set_mtime(target, mtime)


def _set_mtime(target, mtime):
    if mtime:
        try:
            os.utime(target, (mtime, mtime))
        except (OverflowError, ValueError, OSError):
            pass


def _local(ts) -> tuple:
    try:
        return time.localtime(ts)[:6]
    except (OverflowError, OSError, ValueError):  # negative / far-future times in a tar header
        return (1980, 1, 1, 0, 0, 0)


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


def _drain(tf):
    """Read the compressed stream past the end-of-archive blocks to its real end: gzip / bz2 / xz / zstd check
    their CRC there, so damage anywhere in the data is reported instead of passing silently."""
    f = tf.raw if isinstance(tf, _TarStream) else tf.fileobj
    while f.read(1 << 20):
        pass


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
        return [Entry(self._name(m), m.isdir(), m.size, m.size, _local(m.mtime), self.label, False, 0)
                for m in self.members]

    def extract(self, dest, names=None, progress=None, cancel=None, overwrite=True):
        wanted = None if names is None else set(names)
        sel = [m for m in self.members if wanted is None or self._name(m) in wanted]
        plan = _Planner(dest, overwrite, self.path)
        counter = _Counter(sum(m.size for m in sel if m.isreg()), progress, cancel)
        chosen = {id(m) for m in sel}
        order = iter(self.members)
        done = []  # (part, target, mtime): put in place only after the stream's own checksum passed
        try:
            with self._open() as tf:
                for m in tf:  # archive order: a compressed tar is only read forwards
                    if not (m.isreg() or m.isdir()):
                        continue
                    if id(next(order)) not in chosen:
                        continue
                    # only regular files and folders get here (no links, no devices); plan.target cleans the
                    # name for Windows and keeps it under dest
                    t = plan.target(self._name(m), m.isdir())
                    if t is None or m.isdir():
                        continue
                    with tf.extractfile(m) as src:
                        _copy_out(src, t, counter, self._name(m), m.mtime, done)
                _drain(tf)
            while done:
                part, t, mtime = done[0]
                ziplib.finish_part(part, t)
                done.pop(0)
                _set_mtime(t, mtime)
        finally:
            for part, _, _ in done:  # damaged, cancelled or failed: nothing half-checked is left behind
                if os.path.exists(part):
                    os.remove(part)

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
                else:
                    _drain(tf)
        except Cancelled:
            raise
        except Exception:
            bad.append(os.path.basename(self.path))
        return bad


# ---------- single compressed file (.gz / .bz2 / .xz / .zst) ----------

def _stream_open(kind: str, path: str, password: Optional[str] = None):
    if kind in ("br", "lz", "lz4", "aes"):
        import minifmt
        return minifmt.open_stream(kind, path, password)
    if kind == "gz":
        return gzip.open(path, "rb")
    if kind == "bz2":
        return bz2.open(path, "rb")
    if kind == "xz":
        return lzmamt.open_xz(path)
    return zstd.open(path, "rb")


class StreamArchive(_Base):
    def __init__(self, path: str, kind: str, password: Optional[str] = None):
        self.path, self.kind, self.password = path, kind, password or None
        self.name = stem(path) or "data"
        self.csize = os.path.getsize(path)
        self.size = 0  # unknown (bz2, zst): progress then shows bytes without a percentage
        if kind == "gz" and self.csize >= 18:  # gzip trailer keeps the size (mod 4 GiB)
            with open(path, "rb") as f:
                f.seek(-4, 2)
                self.size = int.from_bytes(f.read(4), "little")
        elif kind == "xz":  # the index lists every block's unpacked size
            try:
                self.size = sum(b[3] for b in lzmamt.xz_blocks(path))
            except Exception:
                self.size = 0
        self.mtime = os.path.getmtime(path)

    def entries(self) -> list[Entry]:
        return [Entry(self.name, False, self.size or self.csize, self.csize, _local(self.mtime), self.kind.upper(),
                      self.kind == "aes", 0)]

    def needs_password(self) -> bool:
        return self.kind == "aes"  # AES Crypt

    def extract(self, dest, names=None, progress=None, cancel=None, overwrite=True):
        if names is not None and self.name not in names:
            return
        plan = _Planner(dest, overwrite, self.path)
        t = plan.target(self.name, False)
        if t is None:
            return
        with _stream_open(self.kind, self.path, self.password) as src:
            _copy_out(src, t, _Counter(self.size, progress, cancel), self.name, self.mtime)

    def test(self, progress=None, cancel=None) -> list[str]:
        counter = _Counter(self.size, progress, cancel)
        try:
            with _stream_open(self.kind, self.path, self.password) as src:
                while buf := src.read(1 << 20):
                    counter.add(len(buf), self.name)
        except (Cancelled, BadPassword):
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

    joined = None  # temp file of a split archive's volumes, removed on close

    def close(self):
        if self.joined and os.path.exists(self.joined):
            os.remove(self.joined)

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
        plan = _Planner(dest, overwrite, self.path)
        targets = {}  # position in the archive's file list -> (target, info); names may repeat
        for k, fi in enumerate(self.infos):
            if wanted is not None and self._name(fi) not in wanted:
                continue
            t = plan.target(fi.filename, fi.is_directory)
            if t and not fi.is_directory:
                targets[k] = (t, fi)
        if not targets:
            return
        by_name: dict[str, list] = {}  # py7zr's fallback only knows names: repeated ones are taken in order
        for k, (t, fi) in sorted(targets.items()):
            by_name.setdefault(fi.filename, []).append(k)
        counter = _Counter(sum(fi.uncompressed or 0 for _, fi in targets.values()), progress, cancel)
        opened: dict[int, object] = {}

        class Out(Py7zIO):
            def __init__(self, name, path):
                self.name, self.path = name, path
                os.makedirs(os.path.dirname(path), exist_ok=True)
                self.part = ziplib.part_path(path)  # moved over the target only when the whole archive succeeded
                self.f = open(self.part, "w+b")
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

        def make(k):
            old = opened.pop(k, None)
            if old is not None:
                old.close()
            w = opened[k] = Out(targets[k][1].filename, targets[k][0])
            return w

        class Factory(WriterFactory):
            def create(self, filename):
                ks = by_name.get(filename)
                return make(ks.pop(0)) if ks else NullIO()

        def go(py7zr):
            with py7zr.SevenZipFile(self.path, password=self.password) as z:
                try:
                    folders = lzmamt.fast_plan(z)
                except lzmamt.Unsupported:
                    folders = None
                if folders is None:  # unusual coder layout: py7zr's own (single-threaded) decoder
                    z.extract(targets=list(by_name), factory=Factory())
                # ArchiveFile objects are made afresh on every access; .id is their stable position, in the order
                # of self.infos (z.list())
                pos = {f.id: k for k, f in enumerate(z.files)}
            if folders is not None:  # Out counts progress itself; the sink only reports via the counter
                lzmamt.fast_extract(self.path, folders, self.password,
                                    lambda fi: make(pos[fi.id]) if pos.get(fi.id) in targets else None, _NoCount())
        try:
            self._run(go)
            for k, (t, fi) in targets.items():
                w = opened.get(k)
                if w is None:  # empty members get no writer
                    open(t, "wb").close()
                else:
                    w.close()
                    ziplib.finish_part(w.part, t)
                    del opened[k]
        except BaseException:
            for w in opened.values():  # never leave half-written files behind; existing files stay as they were
                w.close()
                if os.path.exists(w.part):
                    os.remove(w.part)
            raise
        for k, (t, fi) in targets.items():
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
            counter = _Counter(sum(fi.uncompressed or 0 for fi in self.infos), progress, cancel)  # also cancels
            try:
                lzmamt.fast_extract(self.path, folders, self.password, lambda fi: _Discard(), counter)
            except (ValueError, lzma.LZMAError) as e:
                if self.need:
                    raise BadPassword(str(e))
                return [str(e)]
            return []
        try:
            bad = self._run(go)
        except (BadPassword, Cancelled):
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
            zstd.CompressionParameter.checksum_flag: 1,  # without it no reader can detect damaged data
            zstd.CompressionParameter.nb_workers: runtimeconfig.worker_count(16)}


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
