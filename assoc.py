"""Archive file associations, per user (HKCU, no admin).

Registers a ProgID per extension (yzip.zip, yzip.7z, ...), lists yzip under each extension's "Open with", and
publishes Capabilities + RegisteredApplications so Settings > Default apps knows yzip. Windows 10/11 keep the
user's own choice (UserChoice, hash-protected) out of reach of programs: where one exists, only the user can
switch it in Settings, so status() reads the handler Windows really uses (AssocQueryString) instead of
trusting what was written. Where no choice exists, the extension's default is pointed at yzip; the value it
had is kept and put back by unregister()."""
from __future__ import annotations

import ctypes
import os
import sys
import winreg
from ctypes import wintypes

from formats import SUPPORTED_TYPES

APP = "yzip"
CAP_KEY = r"Software\yzipAssoc\Capabilities"
REG_APPS = r"Software\RegisteredApplications"
H = winreg.HKEY_CURRENT_USER

# extension -> type name shown by Explorer
# All readable types are offered; executables must never be associated.
TYPES = {ext: label for ext, label in SUPPORTED_TYPES.items() if ext != ".exe"}


def _extensions(exts):
    """None selects all; an empty selection stays empty. Never accept .exe."""
    selected = list(TYPES if exts is None else exts)
    if any(ext not in TYPES for ext in selected):
        raise ValueError("Unsupported archive extension")
    return list(dict.fromkeys(selected))


def exe_path() -> str:
    return sys.executable if getattr(sys, "frozen", False) else os.path.abspath(sys.argv[0])


def open_command(exe: str) -> str:
    if getattr(sys, "frozen", False) or exe.lower().endswith(".exe") and not exe.lower().endswith("python.exe"):
        return f'"{exe}" "%1"'
    return f'"{sys.executable}" "{exe}" "%1"'


def progid(ext: str) -> str:
    return APP + ext


def _set(path, name, value):
    with winreg.CreateKey(H, path) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)


def _get(path, name=""):
    try:
        with winreg.OpenKey(H, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def _del_value(path, name):
    try:
        with winreg.OpenKey(H, path, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
    except OSError:
        pass


def _del_tree(path):
    try:
        with winreg.OpenKey(H, path) as k:
            while True:
                try:
                    sub = winreg.EnumKey(k, 0)
                except OSError:
                    break
                _del_tree(path + "\\" + sub)
        winreg.DeleteKey(H, path)
    except OSError:
        pass


def notify():
    """Tell Explorer that associations changed (icons, Open with, defaults)."""
    ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)  # SHCNE_ASSOCCHANGED, SHCNF_IDLIST


def register(exts=None, exe: str | None = None, icon: str | None = None) -> None:
    exts = _extensions(exts)
    if not exts:
        return
    exe = exe or exe_path()
    icon = icon or (exe + ",0" if exe.lower().endswith(".exe") else exe)
    cmd = open_command(exe)
    _set(CAP_KEY, "ApplicationName", APP)
    _set(CAP_KEY, "ApplicationDescription", "가볍고 빠른 압축 프로그램")
    _set(REG_APPS, APP, CAP_KEY)
    for ext in exts:
        pid = progid(ext)
        base = rf"Software\Classes\{pid}"
        _set(base, "", TYPES.get(ext, ext.upper().lstrip(".") + " 파일"))
        _set(base + r"\DefaultIcon", "", icon)
        _set(base + r"\shell\open\command", "", cmd)
        _set(rf"Software\Classes\{ext}\OpenWithProgids", pid, "")
        _set(CAP_KEY + r"\FileAssociations", ext, pid)
        cls = rf"Software\Classes\{ext}"
        cur = _get(cls)
        if cur != pid:
            if cur and _get(base, "PreviousDefault") is None:
                _set(base, "PreviousDefault", cur)
            _set(cls, "", pid)  # used by Windows only where the user has made no choice of their own
    notify()


def unregister(exts=None) -> None:
    all_types = exts is None
    exts = _extensions(exts)
    if not exts:
        return
    for ext in exts:
        pid = progid(ext)
        base = rf"Software\Classes\{pid}"
        cls = rf"Software\Classes\{ext}"
        if _get(cls) == pid:
            prev = _get(base, "PreviousDefault")
            if prev:
                _set(cls, "", prev)
            else:
                _del_value(cls, "")
        _del_value(cls + r"\OpenWithProgids", pid)
        _del_tree(base)
        _del_value(CAP_KEY + r"\FileAssociations", ext)
    if all_types:
        _del_tree(r"Software\yzipAssoc")
        _del_value(REG_APPS, APP)
    notify()


def enabled(exts=None) -> bool:
    """Whether yzip's per-user ProgID is registered for every requested extension."""
    return all(_get(rf"Software\Classes\{progid(ext)}") is not None for ext in _extensions(exts))


def effective(ext: str) -> tuple[str, str]:
    """(executable, friendly app name) Windows opens ext with right now, '' when nothing is set."""
    out = []
    for what in (2, 4):  # ASSOCSTR_EXECUTABLE, ASSOCSTR_FRIENDLYAPPNAME
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        hr = ctypes.windll.shlwapi.AssocQueryStringW(0, what, ext, "open", buf, ctypes.byref(size))
        out.append(buf.value if hr == 0 else "")
    return out[0], out[1]


def status(exts=None, exe: str | None = None) -> dict:
    """ext -> (is_yzip, app name) as Windows resolves it now."""
    exe = os.path.normcase(os.path.abspath(exe or exe_path()))
    res = {}
    for ext in _extensions(exts):
        path, name = effective(ext)
        res[ext] = (bool(path) and os.path.normcase(os.path.abspath(path)) == exe, name or "(없음)")
    return res


def open_default_apps():
    """Settings > Default apps, on yzip's page where Windows supports it."""
    os.startfile(f"ms-settings:defaultapps?registeredAppUser={APP}")
