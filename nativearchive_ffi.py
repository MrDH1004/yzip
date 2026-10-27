"""Small, checked ctypes binding for the libarchive read API.

Only the read-side functions used by yzip are exposed here.  The DLL is
loaded from the application bundle, never through PATH, so a missing or
unloadable native backend produces an actionable capability error.
"""
from __future__ import annotations

import ctypes
import os
import re
import sys
from dataclasses import dataclass
from typing import Optional


ARCHIVE_EOF = 1
ARCHIVE_OK = 0
ARCHIVE_WARN = -20
ARCHIVE_FAILED = -25
AE_IFMT = 0o170000
AE_IFREG = 0o100000
AE_IFDIR = 0o040000


class LibArchiveError(RuntimeError):
    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


class LibArchiveUnavailable(LibArchiveError):
    pass


@dataclass(frozen=True)
class Header:
    name: str
    is_dir: bool
    is_regular: bool
    size: int
    mtime: int
    encrypted: bool
    entry: ctypes.c_void_p


def libarchive_dll_path() -> str:
    configured = os.environ.get("YZIP_LIBARCHIVE_DLL")
    if configured:
        return configured
    here = os.path.dirname(os.path.abspath(__file__))
    bundle = os.path.abspath(getattr(sys, "_MEIPASS", here))
    candidates = (
        os.path.join(bundle, "tools", "libarchive.dll"),
        os.path.join(bundle, "libarchive.dll"),
        os.path.join(here, "tools", "libarchive.dll"),
        os.path.join(here, "libarchive.dll"),
    )
    for path in candidates:
        if os.path.isfile(path):
            return path
    return candidates[0]


def available() -> bool:
    return os.path.isfile(libarchive_dll_path())


def volume_paths(path: str) -> list[str]:
    """Return a contiguous RAR volume set for libarchive's filename API."""
    match = re.match(r"^(.*)\.part(\d+)\.rar$", path, re.IGNORECASE)
    if match:
        base, number = match.group(1), match.group(2)
        result = []
        index = 1
        while True:
            candidate = f"{base}.part{index:0{len(number)}d}.rar"
            if not os.path.isfile(candidate):
                break
            result.append(candidate)
            index += 1
        return result or [path]
    if path.lower().endswith(".rar"):
        result = [path]
        stem = path[:-4]
        index = 0
        while os.path.isfile(f"{stem}.r{index:02d}"):
            result.append(f"{stem}.r{index:02d}")
            index += 1
        return result
    return [path]


def _decode(raw: Optional[bytes]) -> str:
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp949", "replace")


class Reader:
    def __init__(self, path: str, password: Optional[str] = None, paths: Optional[list[str]] = None):
        self.path = path
        self.password = password
        self.paths = paths or [path]
        self._lib = self._load_library()
        self._handle = ctypes.c_void_p()
        self._open()

    @staticmethod
    def _load_library():
        path = libarchive_dll_path()
        if not os.path.isfile(path):
            raise LibArchiveUnavailable(f"libarchive.dll is not bundled: {path}")
        try:
            loader = getattr(ctypes, "WinDLL", ctypes.CDLL)
            lib = loader(path)
        except OSError as exc:
            raise LibArchiveUnavailable(f"cannot load libarchive.dll: {path}: {exc}") from exc

        def bind(name, restype, *argtypes):
            try:
                fn = getattr(lib, name)
            except AttributeError as exc:
                raise LibArchiveUnavailable(f"libarchive.dll is missing {name}") from exc
            fn.restype = restype
            fn.argtypes = list(argtypes)
            return fn

        lib._yzip = {
            "read_new": bind("archive_read_new", ctypes.c_void_p),
            "filter_all": bind("archive_read_support_filter_all", ctypes.c_int, ctypes.c_void_p),
            "format_all": bind("archive_read_support_format_all", ctypes.c_int, ctypes.c_void_p),
            "open_filename_w": bind("archive_read_open_filename_w", ctypes.c_int, ctypes.c_void_p,
                                     ctypes.c_wchar_p, ctypes.c_size_t),
            "open_filenames_w": bind("archive_read_open_filenames_w", ctypes.c_int, ctypes.c_void_p,
                                      ctypes.POINTER(ctypes.c_wchar_p), ctypes.c_size_t),
            "add_passphrase": bind("archive_read_add_passphrase", ctypes.c_int, ctypes.c_void_p, ctypes.c_char_p),
            "next_header": bind("archive_read_next_header", ctypes.c_int, ctypes.c_void_p,
                                 ctypes.POINTER(ctypes.c_void_p)),
            "data": bind("archive_read_data", ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_size_t),
            "data_skip": bind("archive_read_data_skip", ctypes.c_int, ctypes.c_void_p),
            "free": bind("archive_read_free", ctypes.c_int, ctypes.c_void_p),
            "error_string": bind("archive_error_string", ctypes.c_char_p, ctypes.c_void_p),
            "entry_path_utf8": bind("archive_entry_pathname_utf8", ctypes.c_char_p, ctypes.c_void_p),
            "entry_path": bind("archive_entry_pathname", ctypes.c_char_p, ctypes.c_void_p),
            "entry_filetype": bind("archive_entry_filetype", ctypes.c_uint32, ctypes.c_void_p),
            "entry_mtime": bind("archive_entry_mtime", ctypes.c_int64, ctypes.c_void_p),
            "entry_mtime_set": bind("archive_entry_mtime_is_set", ctypes.c_int, ctypes.c_void_p),
            "entry_size": bind("archive_entry_size", ctypes.c_int64, ctypes.c_void_p),
            "entry_encrypted": bind("archive_entry_is_encrypted", ctypes.c_int, ctypes.c_void_p),
        }
        return lib

    def _error(self, fallback: str, code: Optional[int] = None) -> LibArchiveError:
        raw = self._lib._yzip["error_string"](self._handle)
        message = _decode(raw) or fallback
        return LibArchiveError(message, code)

    @staticmethod
    def _check(code: int, what: str, error_factory):
        if code < ARCHIVE_WARN:
            raise error_factory(what, code)

    def _open(self):
        handle = self._lib._yzip["read_new"]()
        if not handle:
            raise LibArchiveUnavailable("archive_read_new returned a null handle")
        self._handle = ctypes.c_void_p(handle)
        try:
            for name, what in (("filter_all", "enable archive filters"), ("format_all", "enable archive formats")):
                code = self._lib._yzip[name](self._handle)
                self._check(code, what, self._error)
            if self.password is not None:
                encoded = self.password.encode("utf-8")
                code = self._lib._yzip["add_passphrase"](self._handle, encoded)
                self._check(code, "add archive passphrase", self._error)
            paths = [os.path.abspath(value) for value in self.paths]
            if any("\x00" in value for value in paths):
                raise ValueError("archive path contains NUL")
            if len(paths) == 1:
                code = self._lib._yzip["open_filename_w"](self._handle, paths[0], 1 << 20)
            else:
                names = (ctypes.c_wchar_p * (len(paths) + 1))()
                for index, value in enumerate(paths):
                    names[index] = value
                names[len(paths)] = None
                code = self._lib._yzip["open_filenames_w"](self._handle, names, 1 << 20)
            self._check(code, f"open archive: {self.path}", self._error)
        except BaseException:
            self.close()
            raise

    def next_header(self) -> Optional[Header]:
        entry = ctypes.c_void_p()
        code = self._lib._yzip["next_header"](self._handle, ctypes.byref(entry))
        if code == ARCHIVE_EOF:
            return None
        if code < ARCHIVE_WARN:
            raise self._error("read archive header", code)
        if not entry:
            raise self._error("archive returned an empty header", code)

        raw_name = self._lib._yzip["entry_path_utf8"](entry)
        if not raw_name:
            raw_name = self._lib._yzip["entry_path"](entry)
        filetype = self._lib._yzip["entry_filetype"](entry)
        mtime = 0
        if self._lib._yzip["entry_mtime_set"](entry):
            mtime = self._lib._yzip["entry_mtime"](entry)
        size = max(0, self._lib._yzip["entry_size"](entry))
        return Header(
            _decode(raw_name).replace("\\", "/"),
            (filetype & AE_IFMT) == AE_IFDIR,
            (filetype & AE_IFMT) == AE_IFREG,
            size,
            mtime,
            self._lib._yzip["entry_encrypted"](entry) > 0,
            entry,
        )

    def read_data(self, size: int = 1 << 20) -> bytes:
        buf = ctypes.create_string_buffer(size)
        count = self._lib._yzip["data"](self._handle, buf, size)
        if count < 0:
            raise self._error("read archive data", int(count))
        return buf.raw[:count]

    def skip_data(self):
        code = self._lib._yzip["data_skip"](self._handle)
        if code < ARCHIVE_WARN:
            raise self._error("skip archive data", code)

    def close(self):
        if getattr(self, "_handle", None):
            handle, self._handle = self._handle, ctypes.c_void_p()
            self._lib._yzip["free"](handle)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
