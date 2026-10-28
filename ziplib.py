"""ZIP core logic: create / list / extract / test, AES encryption, CP949 name repair, raw split volumes."""
from __future__ import annotations

import os
import runtimeconfig
import re
import shutil
import struct
import tempfile
import threading
import time
import warnings
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import zipwriter
from archive_accel import (
    CAP_ZIP_EXTRACT,
    AccelStatus,
    NativeZipJob,
    load_accelerator,
)

pyzipper = None  # imported only for encrypted archives

Progress = Callable[[int, int, str], None]  # done_bytes, total_bytes, current_name


class Cancelled(Exception):
    pass


class BadPassword(Exception):
    pass


class NativeExtractionError(Exception):
    pass


class NativeRollbackError(NativeExtractionError):
    def __init__(self, cause: BaseException, failures: list[str], recoverable_backups: list[str]):
        self.cause = cause
        self.failures = failures
        self.recoverable_backups = recoverable_backups
        detail = "; ".join(failures)
        backups = ", ".join(recoverable_backups) or "none"
        super().__init__(
            f"{cause}; native ZIP rollback incomplete: {detail}; "
            f"recoverable backups: {backups}"
        )


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


_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_DEVICES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _safe_part(p: str) -> str:
    """One path component made writable on Windows: no ':' (alternate data streams, drive letters), no reserved
    characters, no trailing dots/spaces (Windows drops them, so 'a.' would overwrite 'a'), no device names,
    at most 240 characters (NTFS allows 255; the extension is kept)."""
    p = _BAD_CHARS.sub("_", p).rstrip(" .")
    if p.split(".")[0].strip().upper() in _DEVICES:
        p = "_" + p
    if len(p) > 240:
        stem, ext = os.path.splitext(p)
        ext = ext if len(ext) <= 20 else ""
        p = stem[:240 - len(ext)].rstrip(" .") + ext
    return p


def part_path(target: str) -> str:
    """Temporary name next to target for writing it; short, so a 240-character name still fits."""
    import secrets
    return os.path.join(os.path.dirname(target), f"~yzip{secrets.token_hex(4)}.part")


def finish_part(part: str, target: str):
    """Put a completely written part file in place of target (an existing read-only file is replaced too)."""
    try:
        os.replace(part, target)
    except PermissionError:
        if not os.path.isfile(target):
            raise
        import stat
        os.chmod(target, stat.S_IWRITE)
        os.replace(part, target)


def _safe_rel(name: str) -> str:
    """Archive member name -> relative path inside the destination ('' = nothing to write)."""
    name = name.replace("\\", "/")
    parts = [_safe_part(p) for p in name.split("/") if p not in ("", ".", "..")]
    return "/".join(p for p in parts if p)


def safe_target(root: str, name: str) -> Optional[str]:
    """Absolute path for a member under root (a realpath), or None when the name points outside it."""
    rel = _safe_rel(name)
    if not rel:
        return None
    target = os.path.realpath(os.path.join(root, *rel.split("/")))
    try:
        if os.path.commonpath([root, target]) != root:  # zip-slip guard (also a symlinked folder inside root)
            return None
    except ValueError:  # another drive
        return None
    return target


def _verify_native_staging(job: NativeZipJob, name: str, cancel) -> None:
    size = 0
    crc = 0
    with open(job.staging_path, "rb") as extracted:
        while True:
            if cancel and cancel():
                raise Cancelled()
            data = extracted.read(1 << 20)
            if not data:
                break
            size += len(data)
            if size > job.expected_size:
                raise zipfile.BadZipFile(f"size mismatch: {name}")
            crc = zlib.crc32(data, crc)
    if cancel and cancel():
        raise Cancelled()
    if size != job.expected_size or crc != job.expected_crc:
        raise zipfile.BadZipFile(f"CRC mismatch: {name}")


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


def _remove_stale_volumes(path: str, first: int):
    """Delete path.NNN volumes from an earlier, longer split: readers join volumes while numbers continue."""
    i = first
    while os.path.exists(f"{path}.{i:03d}"):
        os.remove(f"{path}.{i:03d}")
        i += 1


def split_file(path: str, volume_size: int) -> list[str]:
    _remove_stale_volumes(path, 1)
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
        try:
            # Listing plain ZIPs must not load Cryptodome/cffi on every CLI run.
            # The standard reader also handles non-deflate methods without AES.
            self.zf = zipfile.ZipFile(real)
            self._reader = zipfile.ZipFile
            self._bad_zip = zipfile.BadZipFile
            if any(info.flag_bits & 1 for info in self.zf.infolist()):
                self.zf.close()
                global pyzipper
                import pyzipper
                self.zf = pyzipper.AESZipFile(real)
                self._reader = pyzipper.AESZipFile
                self._bad_zip = pyzipper.BadZipFile
        except BaseException:
            reader = getattr(self, "zf", None)
            if reader is not None:
                reader.close()
            if self._tmp:
                os.remove(self._tmp)
            raise
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
            except (RuntimeError, zipfile.BadZipFile, self._bad_zip):
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

    def _native_accelerator(self):
        if os.environ.get("YZIP_NATIVE_ZIP_EXTRACT") != "1":
            return None
        if self._tmp is not None:
            return None
        infos = self.zf.infolist()
        if not infos or any(info.flag_bits & 1 for info in infos):
            return None
        files = [info for info in infos if not info.is_dir()]
        methods = {info.compress_type for info in files}
        if not methods or not methods.issubset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
            return None
        if len(methods) != 1:
            return None
        if len(files) >= 256 and sum(info.file_size for info in files) <= len(files) * (64 << 10):
            return None
        accelerator = load_accelerator()
        if accelerator is None or not accelerator.capabilities & CAP_ZIP_EXTRACT:
            return None
        return accelerator

    def _extract_native(self, accelerator, jobs, total, skipped, progress, cancel):
        native_jobs = []
        for index, name, info, target, _target_existed in jobs:
            native_jobs.append(NativeZipJob(index, part_path(target), info.file_size, info.CRC))
        staged = {job.staging_path for job in native_jobs}

        def native_progress(done, _native_total, name):
            if progress:
                progress(min(total, skipped + done), total, name)

        if progress:
            progress(skipped, total, "")
        try:
            result = accelerator.extract_zip(self._real, native_jobs, native_progress, cancel)
            if result.status == AccelStatus.UNAVAILABLE:
                return False
            if result.status == AccelStatus.CANCELLED:
                raise Cancelled()
            if result.status == AccelStatus.CORRUPT:
                raise zipfile.BadZipFile("native ZIP CRC or size validation failed")
            if result.status == AccelStatus.IO_ERROR:
                detail = accelerator.last_error() if hasattr(accelerator, "last_error") else ""
                raise OSError(detail or "native ZIP I/O failed")
            if result.status != AccelStatus.OK:
                raise NativeExtractionError(
                    f"native ZIP extraction failed with status {result.status.name}"
                )

            workers = max(1, min(runtimeconfig.worker_count(8), len(native_jobs)))
            with ThreadPoolExecutor(workers) as pool:
                futures = [
                    pool.submit(_verify_native_staging, job, planned[1], cancel)
                    for job, planned in zip(native_jobs, jobs)
                ]
                try:
                    for future in futures:
                        future.result()
                except BaseException:
                    for future in futures:
                        future.cancel()
                    raise
            if cancel and cancel():
                raise Cancelled()

            published = []
            backups = []
            try:
                for job, (_index, _name, _info, target, target_existed) in zip(native_jobs, jobs):
                    if cancel and cancel():
                        raise Cancelled()
                    backup = None
                    if target_existed:
                        backup = part_path(target) + ".native-backup"
                        os.replace(target, backup)
                        backups.append((backup, target))
                    finish_part(job.staging_path, target)
                    staged.discard(job.staging_path)
                    published.append(target)
                    if cancel and cancel():
                        raise Cancelled()
            except BaseException as publish_error:
                rollback_failures = []
                for target in reversed(published):
                    try:
                        os.remove(target)
                    except FileNotFoundError:
                        pass
                    except OSError as error:
                        rollback_failures.append(f"remove {target}: {error}")
                for backup, target in reversed(backups):
                    try:
                        os.replace(backup, target)
                    except OSError as error:
                        rollback_failures.append(f"restore {backup} -> {target}: {error}")
                if rollback_failures:
                    recoverable_backups = [
                        backup for backup, _target in backups if os.path.exists(backup)
                    ]
                    raise NativeRollbackError(
                        publish_error, rollback_failures, recoverable_backups
                    ) from publish_error
                raise
            for backup, _target in backups:
                try:
                    os.remove(backup)
                except OSError as error:
                    warnings.warn(
                        f"native ZIP backup cleanup failed; recoverable backup kept at {backup}: {error}",
                        RuntimeWarning,
                        stacklevel=2,
                    )
            for _index, _name, info, target, _target_existed in jobs:
                try:
                    timestamp = time.mktime(info.date_time + (0, 0, -1))
                    os.utime(target, (timestamp, timestamp))
                except (OverflowError, ValueError, OSError):
                    pass
            return True
        finally:
            for staging_path in staged:
                try:
                    os.remove(staging_path)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    warnings.warn(
                        f"native ZIP staging cleanup failed at {staging_path}: {error}",
                        RuntimeWarning,
                        stacklevel=2,
                    )

    def extract(self, dest: str, names: Optional[Iterable[str]] = None,
                progress: Optional[Progress] = None, cancel: Optional[Callable[[], bool]] = None,
                overwrite=True):
        """overwrite: True replaces, False skips existing files, "rename" writes "name (2).ext" next to them."""
        wanted = None if names is None else set(names)
        sel = [n for n in self._names if wanted is None or n in wanted]
        total = sum(self._names[n].file_size for n in sel)
        root = os.path.realpath(dest)
        os.makedirs(root, exist_ok=True)
        jobs, directories, skipped = [], [], 0
        taken = set()  # targets already handed out in this run (rename mode)
        source_indices = {id(info): index for index, info in enumerate(self.zf.infolist())}

        def free_name(target):
            if not os.path.exists(target) and target.lower() not in taken:
                return target
            base, ext = os.path.splitext(target)
            i = 2
            while os.path.exists(f"{base} ({i}){ext}") or f"{base} ({i}){ext}".lower() in taken:
                i += 1
            return f"{base} ({i}){ext}"
        for n in sel:
            if cancel and cancel():
                raise Cancelled()
            info = self._names[n]
            target = safe_target(root, n)
            if target is None:
                continue
            if info.is_dir():
                directories.append(target)
            elif overwrite is False and os.path.exists(target):
                skipped += info.file_size
            else:
                if overwrite == "rename":
                    target = free_name(target)
                    taken.add(target.lower())
                    target_existed = False
                elif overwrite is False:
                    target_existed = False
                else:
                    target_existed = os.path.exists(target)
                jobs.append((source_indices[id(info)], n, info, target, target_existed))

        # Names that differ only in case land on the same Windows file: keep the last.
        jobs = list({target.lower(): job for job in jobs for target in (job[3],)}.values())
        parents = dict.fromkeys(os.path.dirname(target) for _, _, _, target, _ in jobs)
        accelerator = self._native_accelerator()
        if accelerator is not None and jobs:
            for parent in parents:
                if cancel and cancel():
                    raise Cancelled()
                os.makedirs(parent, exist_ok=True)
            if self._extract_native(accelerator, jobs, total, skipped, progress, cancel):
                for directory in directories:
                    os.makedirs(directory, exist_ok=True)
                return

        for directory in directories:
            os.makedirs(directory, exist_ok=True)

        state = {"done": skipped}
        lock = threading.Lock()
        stop = threading.Event()
        local = threading.local()
        handles = []
        pwd = self.zf.pwd

        def handle():  # one zip handle per worker thread; handles are not thread-safe
            z = getattr(local, "z", None)
            if z is None:
                z = local.z = self._reader(self._real)
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
                        if size > info.file_size:  # more than the header declares: corrupt or a zip bomb
                            raise zipfile.BadZipFile(f"size mismatch: {info.filename}")
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
            _index, n, info, target, _target_existed = job
            if stop.is_set():
                return
            if cancel and cancel():
                raise Cancelled()
            part = part_path(target)  # an existing file is replaced only once the new one is complete
            try:
                with open(part, "wb") as dst:
                    for buf in chunks(info):
                        dst.write(buf)
                        with lock:
                            state["done"] += len(buf)
                            d = state["done"]
                        if progress:
                            progress(d, total, n)
                        if stop.is_set() or (cancel and cancel()):
                            raise Cancelled()
                finish_part(part, target)
            except BaseException as e:
                if os.path.exists(part):
                    os.remove(part)
                if isinstance(e, (RuntimeError, zipfile.BadZipFile, self._bad_zip)) and info.flag_bits & 1 \
                        and ("password" in str(e).lower() or "Bad" in str(e)):
                    raise BadPassword(str(e))  # only an encrypted member can fail because of the password
                raise
            try:
                ts = time.mktime(info.date_time + (0, 0, -1))
                os.utime(target, (ts, ts))
            except (OverflowError, ValueError, OSError):
                pass

        # names that differ only in case land on the same Windows file: keep the last, never write it twice at once
        # Prepare shared parents once rather than issuing mkdir/stat/exists for
        # every small file. safe_target above still resolves every member path.
        for parent in parents:
            if cancel and cancel():
                raise Cancelled()
            os.makedirs(parent, exist_ok=True)
        workers = max(1, min(runtimeconfig.worker_count(8), len(jobs)))
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
            except (zipfile.BadZipFile, self._bad_zip, EOFError, OSError):
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


def _under(name: str, prefix: str) -> bool:
    p = prefix.rstrip("/")
    return name.rstrip("/") == p or name.startswith(p + "/")


def edit_zip(path: str, drop: Iterable[str] = (), renames: Optional[dict] = None, add: Iterable[str] = (),
             password: Optional[str] = None, level: int = 6, store_only: bool = False,
             progress: Optional[Progress] = None, cancel: Optional[Callable[[], bool]] = None,
             into: str = "") -> None:
    """Delete / rename / add entries of a zip in place. Kept entries are copied byte for byte (names, encryption,
    times stay as they were, nothing is decompressed), so no entry is lost even if its name cannot exist on
    Windows. drop / renames take entry names or folder names (a folder covers everything under it). Added files
    replace entries of the same name; they are encrypted with `password` when one is given. into: folder inside
    the archive ("a/b/") the added files go to."""
    if is_split_first(path):
        raise ValueError("분할 압축은 수정할 수 없습니다. 먼저 하나로 합쳐 주세요.")
    drop, renames = list(drop), dict(renames or {})
    into = into.strip("/") + "/" if into.strip("/") else ""
    items = [(p, into + n) for p, n in collect_items(add)] if add else []
    new_names = {n for _, n in items}
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
    secret = [i for i in infos if i.flag_bits & 1 and not i.is_dir()]
    if items and secret:  # new files join an encrypted archive: only with its (checked) password
        if not password:
            raise BadPassword("password required")
        with Archive(path, password) as a:
            probe = min(secret, key=lambda i: i.compress_size)
            try:
                with a.zf.open(probe.filename) as f:  # by name: pyzipper has its own ZipInfo objects
                    while f.read(1 << 20):
                        pass
            except (RuntimeError, zipfile.BadZipFile, pyzipper.BadZipFile) as e:
                raise BadPassword(str(e))
    keep = []
    for info in infos:
        name = fix_name(info)
        if any(_under(name, d) for d in drop):
            continue
        for old, new in renames.items():
            if _under(name, old):
                name = new.rstrip("/") + name[len(old.rstrip("/")):]
                break
        if name in new_names:
            continue
        keep.append((info, name))
    if renames:  # a rename must not land on a name that stays: two entries of one name, one hidden
        before = {}
        for info in infos:
            before.setdefault(fix_name(info).rstrip("/"), []).append(info)
        after = {}
        for info, name in keep:
            after.setdefault(name.rstrip("/"), []).append(info)
        folders = {n.rsplit("/", i)[0] for n in after for i in range(1, n.count("/") + 1)}
        for name, group in after.items():
            if len(group) > max(len(before.get(name, [])), 1) or \
                    (name in folders and any(not i.is_dir() for i in group) and name not in before):
                raise ValueError(f"이미 있는 이름입니다: {name}")
    tmp_out = path + ".partial"
    try:
        zipwriter.write_zip(items, tmp_out, level, store_only, progress, cancel, password=password or None,
                            copy=(path, keep))
        os.replace(tmp_out, path)
    except BaseException:
        if os.path.exists(tmp_out):
            os.remove(tmp_out)
        raise
