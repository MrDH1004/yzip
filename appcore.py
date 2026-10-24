"""Qt-free pieces shared by the exe entry and the GUI: settings, error log, priority, and a background job.
Kept free of PySide6 so "<name>.zip으로 압축하기" can start compressing before Qt (~0.2 s) is loaded."""
from __future__ import annotations

import json
import os
import threading
import time

import formats
import ziplib


def settings_path() -> str:
    return os.environ.get("YZIP_SETTINGS") or os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "yzip", "settings.json")


def load_settings() -> dict:
    try:
        with open(settings_path(), encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def save_setting(key: str, value):
    s = load_settings()
    s[key] = value
    try:
        os.makedirs(os.path.dirname(settings_path()), exist_ok=True)
        tmp = f"{settings_path()}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f)
        os.replace(tmp, settings_path())  # a crash or a second yzip never leaves a half-written file
    except OSError:
        pass


def set_low_priority(on: bool):
    """Idle priority for this process (and so its worker threads), like Bandizip's '낮은 우선순위'."""
    if os.name == "nt":
        import ctypes
        k = ctypes.WinDLL("kernel32")
        k.GetCurrentProcess.restype = ctypes.c_void_p  # a 64-bit pseudo handle; the default int would truncate it
        k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k.SetPriorityClass(k.GetCurrentProcess(), 0x40 if on else 0x20)  # IDLE / NORMAL_PRIORITY_CLASS


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


def quick_compress(paths: list[str], out: str) -> Job:
    """'<name>.zip으로 압축하기': ZIP with default options to the name the menu showed."""
    set_low_priority(bool(load_settings().get("low_priority")))
    return Job(lambda p, c: formats.create("zip", paths, out, progress=p, cancel=c),
               f"압축하기 {os.path.basename(out)}", out, "압축 작업을 완료했습니다.")
