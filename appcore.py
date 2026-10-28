"""Qt-free pieces shared by the exe entry and the GUI: settings, error log, priority, and a background job.
Kept free of PySide6 so "<name>.zip으로 압축하기" can start compressing before Qt (~0.2 s) is loaded."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Mapping

import formats
import runtimeconfig
import ziplib


DEFAULTS = {
    "language": "ko", "theme": "system", "font_family": "", "font_size": 10,
    "compress_format": "zip", "compress_level": 6, "compress_dir": "",
    "compress_split": "분할 안 함", "compress_open_folder": False,
    "extract_dir": "", "extract_smart": True, "extract_mode": "ask",
    "extract_open_folder": False, "low_priority": False, "worker_threads": 0,
    "temp_dir": "", "shell_enabled": True, "shell_compress": True,
    "shell_extract_here": True, "shell_extract_folder": True,
    "shell_multi": "combined", "auto_update": True,
}
_SETTINGS_LOCK = threading.RLock()


def split_bytes(value: str) -> int:
    """Parse the GUI split label; reject invalid or dangerously small volumes."""
    if value in ("", "분할 안 함"):
        return 0
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)?\s*", value, re.I)
    if not match:
        raise ValueError("compress_split: invalid volume size")
    size = int(float(match[1]) * 1024 ** ("B", "KB", "MB", "GB", "TB").index((match[2] or "B").upper()))
    if size < 64 << 10:
        raise ValueError("compress_split: minimum volume size is 64KB")
    return size


def normalize_settings(values: Mapping | None = None, *, strict: bool = False) -> dict:
    """Return a complete independent settings dict, preserving unknown keys.

    Tolerant disk reads replace invalid known values with defaults. Strict imports
    and saves raise ValueError without coercing strings, booleans or numbers.
    Paths need not exist yet; temp_dir is checked for writability when applied.
    """
    if values is None:
        values = {}
    if not isinstance(values, Mapping):
        if strict:
            raise ValueError("settings must be an object")
        values = {}
    result = dict(values)
    enums = {"language": ("ko", "en"), "theme": ("system", "light", "dark"),
             "compress_format": tuple(formats.BY_ID),
             "extract_mode": ("ask", "overwrite", "skip", "rename"),
             "shell_multi": ("combined", "separate")}
    ranges = {"font_size": (1, 96), "compress_level": (0, 9), "worker_threads": (0, 256)}
    for key, default in DEFAULTS.items():
        value = values.get(key, default)
        valid = type(value) is type(default)
        if valid and key in enums:
            valid = value in enums[key]
        if valid and key in ranges:
            valid = ranges[key][0] <= value <= ranges[key][1]
        if valid and isinstance(value, str):
            valid = "\0" not in value
        if valid and key == "compress_split":
            try:
                split_bytes(value)
            except (ValueError, OverflowError):
                valid = False
        if not valid and strict:
            raise ValueError(f"{key}: invalid setting {value!r}")
        result[key] = value if valid else default
    return result


def settings_path() -> str:
    return os.environ.get("YZIP_SETTINGS") or os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "yzip", "settings.json")


def load_settings() -> dict:
    try:
        with open(settings_path(), encoding="utf-8") as f:
            s = json.load(f)
        return normalize_settings(s)
    except (OSError, ValueError):
        return normalize_settings()


def save_settings(values: Mapping) -> dict:
    """Strictly validate and atomically merge an imported/edited settings object.

    Returns the saved full dict. ValueError and OSError propagate to the caller;
    validation/write failures leave the previous file intact.
    """
    if not isinstance(values, Mapping):
        raise ValueError("settings must be an object")
    with _SETTINGS_LOCK:
        s = load_settings()
        s.update(values)
        s = normalize_settings(s, strict=True)
        data = json.dumps(s, ensure_ascii=False, allow_nan=False, indent=2)
        path = os.path.abspath(settings_path())
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".yzip-settings-", suffix=".tmp", dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return s


def save_setting(key: str, value):
    """Compatibility wrapper; UI bulk saves should use save_settings for errors."""
    try:
        save_settings({key: value})
    except OSError:
        pass


def validate_runtime_settings(values: Mapping | None = None) -> dict:
    """Validate preferences before saving; probe temp writability without applying.

    Returns normalized settings. ValueError/OSError propagate; runtime tempdir,
    worker ceiling, priority and active leases remain unchanged.
    """
    settings = normalize_settings(load_settings() if values is None else values, strict=True)
    runtimeconfig.validate(settings["worker_threads"], settings["temp_dir"])
    return settings


def apply_runtime_settings(values: Mapping | None = None, *, strict: bool = True) -> dict:
    """Apply runtime settings; strict=False is only for startup recovery.

    An unavailable saved temporary path is logged and replaced at runtime with
    the system temporary workspace. The saved settings are never modified.
    Explicit preferences application is strict by default and raises on failure.
    """
    settings = normalize_settings(load_settings() if values is None else values, strict=True)
    try:
        runtimeconfig.configure(settings["worker_threads"], settings["temp_dir"])
    except (OSError, ValueError) as exc:
        if strict:
            raise
        log_exception(exc)
        runtimeconfig.configure(settings["worker_threads"], "")
    set_low_priority(settings["low_priority"])
    return settings


def cleanup_temp(*, dry_run: bool = False, paths=None) -> dict | list[str]:
    """List eligible idle sessions, or remove only the supplied rechecked paths.

    No arguments returns {removed, skipped, errors}. dry_run=True returns a list
    of session directory paths under actually applied roots, never a UI draft.
    """
    return runtimeconfig.cleanup_temp(dry_run=dry_run, paths=paths)


def set_low_priority(on: bool):
    """Idle priority for this process (and so its worker threads), like Bandizip's '낮은 우선순위'."""
    if os.name == "nt":
        import ctypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetCurrentProcess.restype = ctypes.c_void_p  # a 64-bit pseudo handle; the default int would truncate it
        k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k.SetPriorityClass.restype = ctypes.c_int
        if not k.SetPriorityClass(k.GetCurrentProcess(), 0x40 if on else 0x20):  # IDLE / NORMAL
            raise ctypes.WinError(ctypes.get_last_error())


def log_path() -> str:
    d = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "yzip")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "error.log")


def log_exception(exc: BaseException):
    """A windowed exe has no stderr, so unexpected errors are appended to %LOCALAPPDATA%\\yzip\\error.log."""
    import traceback
    try:
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)) + "\n")
    except OSError:
        pass


def outcome(fn, progress, cancelled):
    """Run fn(progress, cancelled) -> (ok, result); on failure result is (kind, message) for the UI."""
    try:
        return True, fn(progress, cancelled)
    except ziplib.Cancelled:
        return False, ("cancel", "")
    except ziplib.BadPassword as e:
        return False, ("password", str(e))
    except ValueError as e:  # a format rule (e.g. "파일 하나만"): the message is meant for the user as is
        return False, ("error", str(e))
    except Exception as e:  # surface any I/O or zip error to the UI
        log_exception(e)
        return False, ("error", f"{type(e).__name__}: {e}")


class Job:
    """fn(progress, cancelled) on a plain thread. state holds the latest (done, total, name) for a window
    that may attach later (yzip_gui.run_job(job=...))."""

    def __init__(self, fn, title: str, reveal: str, done_text: str):
        self.fn, self.title, self.reveal, self.done_text = fn, title, reveal, done_text
        self.cancelled = False
        self.ok, self.result = False, None
        self.state = (0, 0, "")
        self.started = time.time()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        self.ok, self.result = outcome(self.fn, self._progress, lambda: self.cancelled)

    def _progress(self, d, t, n):
        self.state = (d, t, n)

    def wait(self, timeout: float) -> bool:
        self.thread.join(timeout)
        return not self.thread.is_alive()


def can_write(folder: str) -> bool:
    """False only when Windows refuses to create a file there (C:\\, Program Files: administrators only).
    A folder that does not exist yet is judged by the nearest one that does."""
    d = os.path.abspath(folder)
    while not os.path.isdir(d) and os.path.dirname(d) != d:
        d = os.path.dirname(d)
    probe = os.path.join(d, f"~yzip{os.getpid()}_{time.time_ns() % 10**9}.tmp")
    try:
        os.close(os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except PermissionError:
        return False
    except OSError:
        return True  # some other problem: let the job run and report it
    try:
        os.remove(probe)
    except OSError:
        pass
    return True


def run_elevated(args: list[str]) -> bool:
    """Start yzip with args as administrator (Windows' UAC prompt). False when it could not start (the user
    said no to the prompt)."""
    import ctypes
    import subprocess
    import sys
    if ctypes.windll.shell32.IsUserAnAdmin():  # already elevated: asking again would loop
        return False
    pre = [] if getattr(sys, "frozen", False) else [os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                 "yzip_main.py")]
    r = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, subprocess.list2cmdline(pre + args),
                                            None, 1)
    return r > 32


def write_list(paths: list[str]) -> str:
    """The same UTF-8 list file the shell extension writes, for handing a selection to another yzip process."""
    import tempfile
    fd, path = tempfile.mkstemp(prefix="yzip_sel_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write("".join(p + "\n" for p in paths))
    return path


def read_list(path: str) -> list[str]:
    """The shell extension's UTF-8 list of selected items; the file is deleted after reading."""
    try:
        with open(path, encoding="utf-8") as f:
            # split on \n only: splitlines() also breaks at U+2028/U+0085, which are legal in file names
            return [ln.rstrip("\r") for ln in f.read().split("\n") if ln.strip("\r")]
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def quick_targets(paths: list[str], out: str, settings: Mapping | None = None, *,
                  explicit_zip: bool = False) -> list[tuple[list[str], str]]:
    """Plan outputs using saved format, or ZIP for a named shell quick action.

    Separate mode archives each selection. Format changes replace only the final
    .zip suffix, preserving dotted folder names. Redirected/separate outputs avoid
    existing files and duplicate output names within the selection.
    """
    s = normalize_settings(load_settings() if settings is None else settings, strict=True)
    fmt = formats.BY_ID["zip" if explicit_zip else s["compress_format"]]
    separate = s["shell_multi"] == "separate" and len(paths) > 1
    groups = [[path] for path in paths] if separate else [list(paths)]
    planned, used = [], set()
    for group in groups:
        if separate:
            first = os.path.abspath(group[0])
            base = first if os.path.isdir(first) else os.path.splitext(first)[0]
            target = base + fmt.ext
        else:
            target = out if fmt.id == "zip" else (out[:-4] if out.lower().endswith(".zip") else out) + fmt.ext
        if s["compress_dir"]:
            target = os.path.join(s["compress_dir"], os.path.basename(target))
        if separate or target != out:
            original = target[:-len(fmt.ext)]
            i = 2
            while (os.path.lexists(target) or os.path.exists(target + ".001") or
                   os.path.normcase(os.path.abspath(target)) in used):
                target = f"{original} ({i}){fmt.ext}"
                i += 1
        used.add(os.path.normcase(os.path.abspath(target)))
        planned.append((group, target))
    return planned


def quick_compress(paths: list[str], out: str) -> Job:
    """Run the shell's named ZIP action, honoring other saved compression options."""
    settings = apply_runtime_settings(strict=False)
    if not paths:
        raise ValueError("No files selected")
    targets = quick_targets(paths, out, settings, explicit_zip=True)
    fmt = formats.BY_ID["zip"]

    def run(progress, cancel):
        outputs = []
        for group, target in targets:
            if cancel():
                raise ziplib.Cancelled()
            if settings["compress_dir"]:
                os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
            outputs.extend(formats.create(
                fmt.id, group, target, level=settings["compress_level"],
                store_only=settings["compress_level"] == 0,
                split_size=split_bytes(settings["compress_split"]) if fmt.split else 0,
                progress=progress, cancel=cancel))
        return outputs

    target = targets[0][1]
    reveal = os.path.dirname(os.path.abspath(target)) if len(targets) > 1 else target
    return Job(run, f"압축하기 {os.path.basename(target)}",
               reveal, "압축 작업을 완료했습니다.")
