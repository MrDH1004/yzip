"""Archive API adapter backed by libarchive's in-process reader."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from typing import Iterable, Optional

import nativearchive_ffi as ffi
import ziplib
from ziplib import BadPassword, Cancelled, Entry


def _local(seconds: int) -> tuple:
    if not seconds:
        return (1980, 1, 1, 0, 0, 0)
    try:
        return time.localtime(seconds)[:6]
    except (OverflowError, OSError, ValueError):
        return (1980, 1, 1, 0, 0, 0)


def _password_error(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in ("password", "passphrase", "encrypted", "decrypt", "credential"))


class LibArchive:
    def __init__(self, path: str, kind: str, password: Optional[str] = None):
        self.path, self.kind, self.password = path, kind, password or None
        self._entries = self._list_entries()

    @staticmethod
    def available() -> bool:
        return ffi.available()

    def _reader(self):
        try:
            paths = ffi.volume_paths(self.path) if self.kind == "rar" else [self.path]
            return ffi.Reader(self.path, self.password, paths)
        except ffi.LibArchiveUnavailable as exc:
            from nativearchive import NativeBackendUnavailable
            raise NativeBackendUnavailable(str(exc)) from exc
        except ffi.LibArchiveError as exc:
            self._raise_archive_error(exc)
            raise AssertionError("unreachable")

    def _raise_archive_error(self, exc):
        text = str(exc)
        if _password_error(text):
            raise BadPassword(text)
        raise ValueError(f"cannot read archive: {os.path.basename(self.path)}: {text}") from exc

    def _entry(self, header: ffi.Header) -> Entry:
        return Entry(header.name, header.is_dir, header.size, 0, _local(header.mtime), self.kind.upper(),
                     header.encrypted, 0)

    def _list_entries(self) -> list[Entry]:
        result = []
        try:
            with self._reader() as reader:
                while True:
                    header = reader.next_header()
                    if header is None:
                        break
                    result.append(self._entry(header))
                    reader.skip_data()
        except (BadPassword, ValueError):
            raise
        except ffi.LibArchiveError as exc:
            self._raise_archive_error(exc)
        return result

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

    def _stage(self, root: str) -> str:
        stage = tempfile.mkdtemp(prefix="~yzip", suffix=".stage", dir=root)
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(stage, 0x2)
        except Exception:
            pass
        return stage

    def extract(self, dest: str, names: Optional[Iterable[str]] = None, progress=None, cancel=None,
                overwrite=True):
        import formats

        root = os.path.realpath(dest)
        os.makedirs(root, exist_ok=True)
        wanted = None if names is None else set(names)
        selected = [entry for entry in self._entries if wanted is None or entry.name in wanted]
        total = sum(entry.size for entry in selected if not entry.is_dir)
        done = 0
        stage = self._stage(root)
        pending = []
        try:
            with self._reader() as reader:
                index = 0
                while True:
                    header = reader.next_header()
                    if header is None:
                        break
                    selected_entry = wanted is None or header.name in wanted
                    if header.is_dir:
                        if selected_entry:
                            pending.append(("dir", header.name, header.mtime, None))
                        reader.skip_data()
                    elif not selected_entry or not header.is_regular:
                        reader.skip_data()
                    else:
                        part = os.path.join(stage, f"{index:08d}.part")
                        index += 1
                        try:
                            with open(part, "wb") as out:
                                while True:
                                    if cancel and cancel():
                                        raise Cancelled()
                                    data = reader.read_data()
                                    if not data:
                                        break
                                    out.write(data)
                                    done += len(data)
                                    if progress:
                                        progress(done, total, header.name)
                        except BaseException:
                            if os.path.exists(part):
                                os.remove(part)
                            raise
                        pending.append(("file", header.name, header.mtime, part))

            plan = formats._Planner(dest, overwrite, self.path)
            for kind, name, mtime, part in pending:
                if kind == "dir":
                    plan.target(name, True)
                    continue
                target = plan.target(name, False)
                if target is None:
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                ziplib.finish_part(part, target)
                if mtime:
                    try:
                        os.utime(target, (mtime, mtime))
                    except (OverflowError, OSError, ValueError):
                        pass
        except ffi.LibArchiveError as exc:
            self._raise_archive_error(exc)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def test(self, progress=None, cancel=None) -> list[str]:
        total = sum(entry.size for entry in self._entries if not entry.is_dir)
        done = 0
        current = os.path.basename(self.path)
        try:
            with self._reader() as reader:
                while True:
                    if cancel and cancel():
                        raise Cancelled()
                    header = reader.next_header()
                    if header is None:
                        break
                    current = header.name or os.path.basename(self.path)
                    if not header.is_regular:
                        reader.skip_data()
                        continue
                    while True:
                        data = reader.read_data()
                        if not data:
                            break
                        done += len(data)
                        if progress:
                            progress(done, total, current)
        except ffi.LibArchiveError as exc:
            if _password_error(str(exc)):
                raise BadPassword(str(exc)) from exc
            return [current]
        return []
