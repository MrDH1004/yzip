"""In-process EGG/ALZ adapter for the yzip-native Rust cdylib."""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import sys
import tempfile
import time
from typing import Iterable, Optional

import ziplib
from ziplib import BadPassword, Cancelled, Entry


def native_dll_path() -> str:
    configured = os.environ.get("YZIP_NATIVE_DLL")
    if configured:
        return configured
    here = os.path.dirname(os.path.abspath(__file__))
    bundle = os.path.abspath(getattr(sys, "_MEIPASS", here))
    candidates = (
        os.path.join(bundle, "tools", "yzip_native.dll"),
        os.path.join(bundle, "yzip_native.dll"),
        os.path.join(here, "tools", "yzip_native.dll"),
        os.path.join(here, "nativearchive", "target", "release", "yzip_native.dll"),
    )
    for path in candidates:
        if os.path.isfile(path):
            return path
    return candidates[0]


def available() -> bool:
    return os.path.isfile(native_dll_path())


class NativeLegacyError(RuntimeError):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class _Native:
    def __init__(self):
        path = native_dll_path()
        if not os.path.isfile(path):
            from nativearchive import NativeBackendUnavailable
            raise NativeBackendUnavailable(f"yzip_native.dll is not bundled: {path}")
        try:
            loader = getattr(ctypes, "WinDLL", ctypes.CDLL)
            self.lib = loader(path)
        except OSError as exc:
            from nativearchive import NativeBackendUnavailable
            raise NativeBackendUnavailable(f"cannot load yzip_native.dll: {path}: {exc}") from exc
        self.lib.yzip_native_list.restype = ctypes.c_int
        self.lib.yzip_native_list.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                                              ctypes.POINTER(ctypes.c_void_p)]
        self.lib.yzip_native_extract.restype = ctypes.c_int
        self.lib.yzip_native_extract.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
        self.lib.yzip_native_last_error.restype = ctypes.c_char_p
        self.lib.yzip_native_free_string.restype = None
        self.lib.yzip_native_free_string.argtypes = [ctypes.c_void_p]

    @staticmethod
    def _text(value: str) -> bytes:
        return value.encode("utf-8")

    def _error(self, code: int) -> NativeLegacyError:
        raw = self.lib.yzip_native_last_error()
        message = raw.decode("utf-8", "replace") if raw else "native archive operation failed"
        return NativeLegacyError(code, message)

    def list(self, kind: str, path: str, password: Optional[str]) -> list[dict]:
        output = ctypes.c_void_p()
        code = self.lib.yzip_native_list(self._text(kind), self._text(path),
                                         self._text(password) if password is not None else None,
                                         ctypes.byref(output))
        if code != 0:
            raise self._error(code)
        if not output.value:
            raise NativeLegacyError(2, "native archive returned no listing")
        try:
            raw = ctypes.string_at(output.value)
            return json.loads(raw.decode("utf-8"))
        finally:
            self.lib.yzip_native_free_string(output)

    def extract(self, kind: str, path: str, destination: str, password: Optional[str]):
        code = self.lib.yzip_native_extract(self._text(kind), self._text(path), self._text(destination),
                                            self._text(password) if password is not None else None)
        if code != 0:
            raise self._error(code)


def _mtime(seconds: int) -> tuple:
    if not seconds:
        return (1980, 1, 1, 0, 0, 0)
    try:
        return time.localtime(seconds)[:6]
    except (OverflowError, OSError, ValueError):
        return (1980, 1, 1, 0, 0, 0)


class LegacyArchive:
    def __init__(self, path: str, kind: str, password: Optional[str] = None):
        self.path, self.kind, self.password = path, kind, password or None
        self._native = _Native()
        try:
            raw_entries = self._native.list(kind, path, self.password)
        except NativeLegacyError as exc:
            self._raise(exc)
            raise AssertionError("unreachable")
        self._entries = [Entry(str(item["name"]).replace("\\", "/"), bool(item["is_dir"]), int(item["size"]),
                               int(item["csize"]), _mtime(int(item.get("mtime", 0))), str(item["method"]),
                               bool(item["encrypted"]), 0) for item in raw_entries]

    @staticmethod
    def _raise(error: NativeLegacyError):
        if error.code == 3:
            raise BadPassword(str(error))
        raise ValueError(f"cannot read archive: {error}")

    def entries(self) -> list[Entry]:
        return list(self._entries)

    def needs_password(self) -> bool:
        return any(entry.encrypted for entry in self._entries)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def extract(self, dest: str, names: Optional[Iterable[str]] = None, progress=None, cancel=None,
                overwrite=True):
        import formats

        if cancel and cancel():
            raise Cancelled()
        root = os.path.realpath(dest)
        os.makedirs(root, exist_ok=True)
        stage = tempfile.mkdtemp(prefix="~yzip", suffix=".stage", dir=root)
        try:
            try:
                import ctypes as _ctypes
                _ctypes.windll.kernel32.SetFileAttributesW(stage, 0x2)
            except Exception:
                pass
            self._native.extract(self.kind, self.path, stage, self.password)
            if cancel and cancel():
                raise Cancelled()
            wanted = None if names is None else set(names)
            selected = [entry for entry in self._entries if wanted is None or entry.name in wanted]
            total = sum(entry.size for entry in selected if not entry.is_dir)
            if progress:
                progress(total, total, os.path.basename(self.path))
            plan = formats._Planner(dest, overwrite, self.path)
            for entry in selected:
                if entry.is_dir:
                    plan.target(entry.name, True)
                    continue
                source = ziplib.safe_target(stage, entry.name)
                if source is None or not os.path.isfile(source) or os.path.islink(source):
                    continue
                target = plan.target(entry.name, False)
                if target is None:
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                ziplib.finish_part(source, target)
                try:
                    ts = time.mktime(entry.mtime + (0, 0, -1))
                    os.utime(target, (ts, ts))
                except (OverflowError, OSError, ValueError):
                    pass
        except NativeLegacyError as exc:
            self._raise(exc)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def test(self, progress=None, cancel=None) -> list[str]:
        if cancel and cancel():
            raise Cancelled()
        stage = tempfile.mkdtemp(prefix="yzip-native-test-")
        try:
            if progress:
                progress(0, sum(entry.size for entry in self._entries if not entry.is_dir), os.path.basename(self.path))
            try:
                self._native.extract(self.kind, self.path, stage, self.password)
            except NativeLegacyError as exc:
                if exc.code == 3:
                    raise BadPassword(str(exc)) from exc
                return [os.path.basename(self.path)]
            if progress:
                total = sum(entry.size for entry in self._entries if not entry.is_dir)
                progress(total, total, os.path.basename(self.path))
            return []
        finally:
            shutil.rmtree(stage, ignore_errors=True)
