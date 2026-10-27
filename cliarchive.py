"""Formats read through the compatibility extractor bundled in tools\\ (see fetch_tools.ps1): 7-Zip for formats
not covered by the in-process backends. Same API as ziplib.Archive (entries / needs_password /
extract / test / close).

The tool extracts into a hidden staging folder inside dest; the files are then moved into place through
formats._Planner, so overwrite / skip / "name (2)", safe Windows names and "nothing changes when it fails" work
as for the built-in formats. Only the compatibility 7-Zip path is implemented here; RAR, ISO, CAB, EGG and ALZ
are routed by formats.py to their in-process backends."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Optional

import ziplib
from ziplib import BadPassword, Cancelled, Entry

_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW
_REPARSE = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT
_HIDDEN = 0x2
TOOL = {"7zcli": "7z.exe"}
LABEL = {"egg": "EGG", "alz": "ALZ"}


def tool_path(name: str) -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.join(getattr(sys, "_MEIPASS", here), "tools"), os.path.join(here, "tools")):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    raise ValueError(f"이 형식을 푸는 도구({name})가 설치되어 있지 않습니다.")


def _wrong_password(text: str) -> bool:
    t = text.lower()
    return "wrong password" in t or "invalid password" in t or "password required" in t \
        or "can not open encrypted archive" in t


def _mtime(s: str) -> tuple:
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)", s or "")
    return tuple(int(x) for x in m.groups()) if m else (1980, 1, 1, 0, 0, 0)


class _Proc:
    """A tool run with stdout / stderr in temp files (no pipe can fill up while we poll)."""

    def __init__(self, argv: list[str], stdout=None):
        self.out = tempfile.TemporaryFile()
        self.err = tempfile.TemporaryFile()
        self.p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stdout or self.out, stderr=self.err,
                                  creationflags=_NO_WINDOW)

    def text(self) -> str:
        parts = []
        for f in (self.out, self.err):
            f.seek(0)
            parts.append(f.read().decode("utf-8", "replace"))
        return "\n".join(parts)

    def kill(self):
        if self.p.poll() is None:
            self.p.kill()
            self.p.wait()

    def close(self):
        self.kill()
        self.out.close()
        self.err.close()


def _run(argv: list[str]) -> tuple[int, str]:
    pr = _Proc(argv)
    try:
        return pr.p.wait(), pr.text()
    finally:
        pr.close()


def _rmtree(path: str):
    """shutil.rmtree that also removes read-only files (7-Zip restores the attribute from the archive)."""
    import stat

    def again(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onexc=again)


def _staged_size(stage: str) -> int:
    n = 0
    for dp, _, files in os.walk(stage):
        for f in files:
            try:
                n += os.stat(os.path.join(dp, f), follow_symlinks=False).st_size
            except OSError:
                pass
    return n


class CliArchive:
    def __init__(self, path: str, kind: str, password: Optional[str] = None):
        if kind != "7zcli":
            raise ValueError(f"{kind} is handled by an in-process backend")
        self.path, self.kind, self.password = path, kind, password or None
        self.exe = tool_path(TOOL[kind])
        rc, text = _run(self._list_argv())
        if rc != 0:
            if _wrong_password(text):  # names are encrypted too (RAR -hp, 7z -mhe)
                raise BadPassword(text.strip().splitlines()[-1] if text.strip() else "password")
            raise ValueError("압축 파일을 열 수 없습니다: " + os.path.basename(path) + self._why(text))
        self._entries = self._parse(text)

    # ----- the tools' command lines and output -----
    def _pw(self) -> list[str]:
        if self.kind == "7zcli":
            return ["-p" + (self.password or "")]  # "-p" alone: an empty password, never a prompt
        return ["-P", self.password or "-"]  # a placeholder: without -P they ask on the console

    def _list_argv(self) -> list[str]:
        if self.kind == "7zcli":
            return [self.exe, "l", "-slt", "-ba", "-sccUTF-8", *self._pw(), self.path]
        return [self.exe, "-l", "--", self.path]  # listing never asks for a password

    @staticmethod
    def _why(text: str) -> str:
        lines = [ln.strip() for ln in text.splitlines() if "error" in ln.lower() or "err:" in ln.lower()]
        return f"\n{lines[-1]}" if lines else ""

    def _parse(self, text: str) -> list[Entry]:
        out = []
        if self.kind == "7zcli":  # "Key = Value" blocks separated by blank lines
            for block in re.split(r"\r?\n\s*\r?\n", text):
                kv = {}
                for ln in block.splitlines():
                    k, sep, v = ln.partition(" = ")
                    if sep:
                        kv[k.strip()] = v.strip()
                if "Path" not in kv:
                    continue
                is_dir = kv.get("Folder") == "+" or "D" in kv.get("Attributes", "").split(" ")[0]
                name = kv["Path"].replace("\\", "/").rstrip("/") + ("/" if is_dir else "")
                crc = kv.get("CRC") or "0"
                out.append(Entry(name, is_dir, int(kv.get("Size") or 0), int(kv.get("Packed Size") or 0),
                                 _mtime(kv.get("Modified", "")), kv.get("Method") or "", kv.get("Encrypted") == "+",
                                 int(crc, 16) if re.fullmatch(r"[0-9A-Fa-f]{1,8}", crc) else 0))
        elif self.kind == "egg":  # [*] size  method  date time  name
            for ln in text.splitlines():
                m = re.match(r"^\s*(\*)?\s*(\d+)\s+(\S+)\s+(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)  (.+)$", ln)
                if m:
                    name = m.group(5)
                    out.append(Entry(name, name.endswith("/"), int(m.group(2)), 0, _mtime(m.group(4)), m.group(3),
                                     bool(m.group(1)), 0))
        else:  # alz: attr  size  packed  method  date time  name[*]
            for ln in text.splitlines():
                m = re.match(r"^([A-Z_]{5})\s+(\d+)\s+(\d+)\s+(\S+)\s+(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)  (.+)$", ln)
                if m:
                    name, enc = m.group(6), False
                    if name.endswith("*"):  # Windows names cannot end in "*": the encryption mark
                        name, enc = name[:-1], True
                    is_dir = m.group(1)[1] == "D"
                    name = name.rstrip("/") + ("/" if is_dir else "")
                    out.append(Entry(name, is_dir, int(m.group(2)), int(m.group(3)), _mtime(m.group(5)), m.group(4),
                                     enc, 0))
        return out

    # ----- archive API -----
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def close(self):
        pass

    def entries(self) -> list[Entry]:
        return list(self._entries)

    def needs_password(self) -> bool:
        return any(e.encrypted for e in self._entries)

    def _fail(self, text: str, what: str):
        if _wrong_password(text):
            raise BadPassword("wrong password")
        raise ValueError(f"{what}: {os.path.basename(self.path)}{self._why(text)}")

    def extract(self, dest, names=None, progress=None, cancel=None, overwrite=True):
        import formats
        plan = formats._Planner(dest, overwrite, self.path)
        chosen = [e for e in self._entries if names is None or e.name in names]
        if names is not None and not any(not e.is_dir for e in chosen):  # folders only: no FILE operand would
            for e in chosen:                                              # mean "everything" to the tools
                plan.target(e.name, True)
            return
        stage = tempfile.mkdtemp(prefix="~yzip", suffix=".stage", dir=plan.root)
        try:
            try:
                import ctypes
                ctypes.windll.kernel32.SetFileAttributesW(stage, _HIDDEN)
            except Exception:
                pass
            total = sum(e.size for e in chosen if not e.is_dir)
            argv, listfile = self._extract_argv(stage, None if names is None else [e.name for e in chosen])
            try:
                self._watch(argv, stage, total, progress, cancel)
            finally:
                if listfile:
                    os.remove(listfile)
            self._move(stage, plan)
        finally:
            if os.path.exists(stage):
                _rmtree(stage)

    def _extract_argv(self, stage: str, names: Optional[list[str]]):
        if self.kind == "7zcli":
            argv = [self.exe, "x", "-y", "-aoa", "-bso0", "-bsp0", "-sccUTF-8", "-scsUTF-8", *self._pw(),
                    "-o" + stage, self.path]
            if names is None:
                return argv, None
            fd, listfile = tempfile.mkstemp(prefix="yzip_7z_", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("".join(n.rstrip("/").replace("/", "\\") + "\n" for n in names))
            return argv + ["@" + listfile], listfile
        # "--": the tools read options anywhere on the line, and a member may be named "--output-dir=..."
        argv = [self.exe, "-q", *self._pw(), "-d", stage, "--", self.path]
        files = [n for n in (names or []) if not n.endswith("/")]
        return argv + files, None

    def _watch(self, argv, stage, total, progress, cancel):
        name = os.path.basename(self.path)
        if cancel and cancel():
            raise Cancelled()
        pr = _Proc(argv)
        try:
            pause = 0.1
            while True:
                try:
                    pr.p.wait(pause)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if cancel and cancel():
                    pr.kill()
                    raise Cancelled()
                if progress:
                    t0 = time.time()
                    done = _staged_size(stage)
                    pause = min(1.0, max(0.1, 4 * (time.time() - t0)))  # a huge tree: look less often
                    progress(min(done, total), total, name)
            if pr.p.returncode != 0:
                self._fail(pr.text(), "압축을 풀 수 없습니다")
            if progress:
                progress(total, total, name)
        finally:
            pr.close()

    def _move(self, stage: str, plan):
        """Staged files into place. Links and junctions a hostile archive may carry (WIM, RAR) are dropped."""
        for dp, dirs, files in os.walk(stage):
            rel = os.path.relpath(dp, stage)
            rel = "" if rel == "." else rel.replace("\\", "/") + "/"
            for d in list(dirs):
                if os.lstat(os.path.join(dp, d)).st_file_attributes & _REPARSE:
                    dirs.remove(d)
                    continue
                plan.target(rel + d, True)
            for f in files:
                src = os.path.join(dp, f)
                if os.lstat(src).st_file_attributes & _REPARSE:
                    continue
                t = plan.target(rel + f, False)
                if t is None:
                    continue
                os.makedirs(os.path.dirname(t), exist_ok=True)
                ziplib.finish_part(src, t)

    def test(self, progress=None, cancel=None) -> list[str]:
        name = os.path.basename(self.path)
        total = sum(e.size for e in self._entries if not e.is_dir)
        if self.kind == "7zcli":
            argv = [self.exe, "t", "-bso0", "-bsp0", "-sccUTF-8", *self._pw(), self.path]
            pr = _Proc(argv)
        else:  # no test mode: unpack everything to stdout (CRCs are checked) and count it
            pr = _Proc([self.exe, "-q", "-p", *self._pw(), "--", self.path], stdout=subprocess.PIPE)
        try:
            if progress:
                progress(0, total, name)
            done = 0
            if pr.p.stdout is not None:
                while buf := pr.p.stdout.read(1 << 20):
                    done += len(buf)
                    if progress:
                        progress(min(done, total), total, name)
                    if cancel and cancel():
                        pr.kill()
                        raise Cancelled()
            while True:
                try:
                    pr.p.wait(0.1)
                    break
                except subprocess.TimeoutExpired:
                    if cancel and cancel():
                        pr.kill()
                        raise Cancelled()
            if pr.p.returncode != 0:
                text = pr.text()
                if _wrong_password(text):
                    raise BadPassword("wrong password")
                bad = [ln.rsplit(" : ", 1)[-1].strip() for ln in text.splitlines() if ln.startswith("ERROR:")]
                return bad or [name]
            if progress:
                progress(total, total, name)
            return []
        finally:
            if pr.p.stdout is not None:
                pr.p.stdout.close()
            pr.close()
