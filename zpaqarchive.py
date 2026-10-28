"""ZPAQ reader backed by the bundled public-domain zpaq command line tool.

ZPAQ is a journaling format rather than a 7-Zip codec, so it is kept as a
compatibility backend with the same list/test/extract contract as the other
archive readers. The tool always extracts into a private staging directory;
the common planner decides what can reach the user's destination.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from typing import Optional

import ziplib
from cliarchive import _HIDDEN, _REPARSE, _Proc, _rmtree, _run, _staged_size, tool_path
from ziplib import BadPassword, Cancelled, Entry

_LIST_LINE = re.compile(
    r"^[#=+\-?^]\s+(\d{4}-\d\d-\d\d)\s+(\d\d:\d\d:\d\d)\s+(\d+)\s+(\S*)\s+(.+?)\s*$"
)


def _mtime(date: str, clock: str) -> tuple:
    return tuple(int(part) for part in (*date.split("-"), *clock.split(":")))


def _wrong_password(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in ("password", "passphrase", "encrypted", "key required", "wrong key"))


def _normalise_name(name: str, is_dir: bool) -> str:
    name = name.replace("\\", "/").strip()
    if len(name) >= 3 and name[1] == ":" and name[2] == "/":
        name = name[0] + name[2:]
    name = name.rstrip("/")
    return name + "/" if is_dir else name


class ZpaqArchive:
    def __init__(self, path: str, password: Optional[str] = None):
        self.path, self.password = path, password
        self.exe = tool_path("zpaq.exe")
        rc, text = _run(self._argv("list"))
        self._entries = self._parse(text)
        if _wrong_password(text):
            raise BadPassword("wrong password")
        if rc not in (0, 1) or (rc == 1 and not self._entries):
            detail = self._why(text)
            raise ValueError(f"ZPAQ 압축 파일을 읽을 수 없습니다: {os.path.basename(path)}{detail}")
        self._encrypted = self._looks_encrypted()

    def _argv(self, command: str) -> list[str]:
        argv = [self.exe, command, self.path]
        if self.password is not None:
            argv += ["-key", self.password]
        return argv

    @staticmethod
    def _why(text: str) -> str:
        lines = [line.strip() for line in text.splitlines() if "error" in line.lower() or "invalid" in line.lower()]
        return f"\n{lines[-1]}" if lines else ""

    @staticmethod
    def _parse(text: str) -> list[Entry]:
        entries = []
        for line in text.splitlines():
            match = _LIST_LINE.match(line)
            if not match:
                continue
            date, clock, size, attrs, raw_name = match.groups()
            is_dir = "D" in attrs or raw_name.endswith("/")
            name = _normalise_name(raw_name, is_dir)
            if not name:
                continue
            entries.append(Entry(name, is_dir, 0 if is_dir else int(size), 0,
                                 _mtime(date, clock), "ZPAQ", False, 0))
        return entries

    def _looks_encrypted(self) -> bool:
        try:
            with open(self.path, "rb") as archive:
                return archive.read(1) not in (b"z", b"7")
        except OSError:
            return False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        pass

    def entries(self) -> list[Entry]:
        return list(self._entries)

    def needs_password(self) -> bool:
        return self._encrypted

    @staticmethod
    def _selected(name: str, wanted: Optional[set[str]]) -> bool:
        if wanted is None:
            return True
        clean = name.rstrip("/")
        return any(clean == item.rstrip("/") or clean.startswith(item.rstrip("/") + "/") for item in wanted)

    def extract(self, dest, names=None, progress=None, cancel=None, overwrite=True):
        import formats

        plan = formats._Planner(dest, overwrite, self.path)
        wanted = None if names is None else {name.replace("\\", "/") for name in names}
        chosen = [entry for entry in self._entries if self._selected(entry.name, wanted)]
        total = sum(entry.size for entry in chosen if not entry.is_dir)
        stage = tempfile.mkdtemp(prefix="~yzip", suffix=".stage", dir=plan.root)
        try:
            try:
                import ctypes
                ctypes.windll.kernel32.SetFileAttributesW(stage, _HIDDEN)
            except Exception:
                pass
            # Extract all into staging and apply the selection only while moving files.
            # This avoids passing archive member names as command-line switches.
            self._watch([*self._argv("extract"), "-force", "-to", stage], stage, total, progress, cancel)
            self._move(stage, plan, wanted)
        finally:
            if os.path.exists(stage):
                _rmtree(stage)

    def _watch(self, argv, stage, total, progress, cancel):
        name = os.path.basename(self.path)
        if cancel and cancel():
            raise Cancelled()
        proc = _Proc(argv)
        try:
            while True:
                try:
                    proc.p.wait(0.1)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if cancel and cancel():
                    proc.kill()
                    raise Cancelled()
                if progress:
                    progress(min(_staged_size(stage), total), total, name)
            if proc.p.returncode not in (0, 1):
                text = proc.text()
                if _wrong_password(text):
                    raise BadPassword("wrong password")
                raise ValueError(f"ZPAQ 압축 해제에 실패했습니다: {os.path.basename(self.path)}{self._why(text)}")
            if progress:
                progress(total, total, name)
        finally:
            proc.close()

    def _move(self, stage, plan, wanted):
        mtimes = {entry.name: entry.mtime for entry in self._entries}
        for dp, dirs, files in os.walk(stage):
            rel = os.path.relpath(dp, stage)
            rel = "" if rel == "." else rel.replace("\\", "/") + "/"
            for directory in list(dirs):
                name = rel + directory + "/"
                if not self._selected(name, wanted):
                    dirs.remove(directory)
                    continue
                source = os.path.join(dp, directory)
                if getattr(os.lstat(source), "st_file_attributes", 0) & _REPARSE:
                    dirs.remove(directory)
                    continue
                plan.target(name, True)
            for filename in files:
                source = os.path.join(dp, filename)
                if getattr(os.lstat(source), "st_file_attributes", 0) & _REPARSE:
                    continue
                name = rel + filename
                if not self._selected(name, wanted):
                    continue
                target = plan.target(name, False)
                if target is None:
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                ziplib.finish_part(source, target)
                mtime = mtimes.get(name)
                if mtime:
                    try:
                        stamp = time.mktime(mtime + (0, 0, -1))
                        os.utime(target, (stamp, stamp))
                    except (OverflowError, OSError, ValueError):
                        pass

    def test(self, progress=None, cancel=None) -> list[str]:
        if cancel and cancel():
            raise Cancelled()
        rc, text = _run([*self._argv("extract"), "-test"])
        if _wrong_password(text):
            raise BadPassword("wrong password")
        if rc != 0:
            return [os.path.basename(self.path)]
        if progress:
            total = sum(entry.size for entry in self._entries if not entry.is_dir)
            progress(total, total, os.path.basename(self.path))
        return []
