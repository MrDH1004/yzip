"""Update check against the GitHub latest-release API (public repo, no token).

A release carries yzip-setup-X.Y.Z.exe and yzip-setup-X.Y.Z.exe.sha256. The installer is downloaded to a temp
folder, checked against that SHA-256 and started silently; it closes yzip, installs, re-registers the Explorer
menu and starts the new version with --updated. YZIP_UPDATE_URL points the check at another URL (tests use a
local server); YZIP_NO_UPDATE turns automatic checks off."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

from version import REPO, __version__

UA = f"yzip/{__version__} (+https://github.com/{REPO})"
CHECK_EVERY = 24 * 3600


def api_url() -> str:
    return os.environ.get("YZIP_UPDATE_URL") or f"https://api.github.com/repos/{REPO}/releases/latest"


def parse_version(s: str) -> tuple:
    nums = re.findall(r"\d+", s or "")
    return tuple(int(n) for n in nums[:3]) if nums else (0,)


@dataclass
class Release:
    version: str
    url: str          # installer download
    sha_url: str      # its .sha256
    name: str
    notes: str
    page: str


def _get(url: str, timeout: float = 8.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def check(current: str = __version__) -> Optional[Release]:
    """The newer release, or None when up to date. Network / format problems raise."""
    data = json.loads(_get(api_url()))
    if data.get("draft") or data.get("prerelease"):
        return None
    tag = data.get("tag_name", "")
    if parse_version(tag) <= parse_version(current):
        return None
    assets = {a["name"]: a["browser_download_url"] for a in data.get("assets", [])}
    setup = next((n for n in assets if re.fullmatch(r"yzip-setup-[\d.]+\.exe", n)), None)
    if not setup or setup + ".sha256" not in assets:
        return None  # a release without a verifiable installer is not offered
    return Release(tag.lstrip("v"), assets[setup], assets[setup + ".sha256"], setup,
                   data.get("body") or "", data.get("html_url") or "")


def download(rel: Release, progress: Optional[Callable[[int, int, str], None]] = None,
             cancel: Optional[Callable[[], bool]] = None) -> str:
    """Installer into a fresh temp folder, verified against the published SHA-256. Returns its path."""
    want = _get(rel.sha_url).decode("ascii", "replace").split()[0].lower()
    if not re.fullmatch(r"[0-9a-f]{64}", want):
        raise ValueError("업데이트 파일의 체크섬 형식이 올바르지 않습니다.")
    folder = tempfile.mkdtemp(prefix="yzip_update_")
    path = os.path.join(folder, rel.name)
    req = urllib.request.Request(rel.url, headers={"User-Agent": UA})
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=30) as r, open(path, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 16):
            if cancel and cancel():
                raise InterruptedError("cancelled")
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total, rel.name)
    if h.hexdigest() != want:
        os.remove(path)
        raise ValueError("다운로드한 업데이트 파일이 손상되었거나 변조되었습니다 (SHA-256 불일치).")
    return path


def launch_installer(path: str) -> None:
    """Start the installer silently and detached; the caller exits right after so files can be replaced."""
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                     creationflags=flags, close_fds=True)


def due(settings: dict) -> bool:
    """Automatic check wanted now: on, not disabled for tests, and not checked within a day."""
    if os.environ.get("YZIP_NO_UPDATE") or not settings.get("auto_update", True):
        return False
    return time.time() - float(settings.get("update_checked_at", 0)) >= CHECK_EVERY
