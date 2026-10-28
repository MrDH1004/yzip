"""Engine runtime limits and leased yzip-only temporary workspaces (no appcore import)."""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import uuid

_lock = threading.RLock()
_threads = 0
_system_temp = tempfile.gettempdir()
_sessions: dict[str, tuple[str, object]] = {}
_MARKER = b"yzip-temp-session-v1\n"


def worker_count(default_cap: int | None = None) -> int:
    """CPU count limited by the user's ceiling and the engine's existing cap."""
    count = os.cpu_count() or 2
    if _threads:
        count = min(count, _threads)
    if default_cap is not None:
        count = min(count, default_cap)
    return max(1, count)


def _lease(file):
    file.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _redirect(path: str) -> bool:
    return os.path.islink(path) or os.path.isjunction(path)


def validate(worker_threads: int = 0, temp_dir: str = "") -> str:
    """Probe the selected private root without changing threads, tempdir or leases.

    A not-yet-created directory is created so writability is actually tested.
    Returns its resolved yzip root; all probe files are removed immediately.
    """
    if type(worker_threads) is not int or not 0 <= worker_threads <= 256:
        raise ValueError("worker_threads must be an integer from 0 to 256")
    if not isinstance(temp_dir, str) or "\0" in temp_dir:
        raise ValueError("temp_dir must be a path string")
    base = os.path.realpath(os.path.expanduser(temp_dir or _system_temp))
    root = os.path.join(base, "yzip-temp")
    if _redirect(root):
        raise ValueError("yzip-temp must not be a link or junction")
    os.makedirs(root, exist_ok=True)
    fd, probe = tempfile.mkstemp(prefix=".probe-", dir=root)
    os.close(fd)
    os.remove(probe)
    return root


def configure(worker_threads: int = 0, temp_dir: str = "") -> str:
    """Validate/create a writable session, then publish limits. Return temp path.

    Each session's marker remains exclusively locked for the process lifetime,
    including after switching roots, protecting open archives and background jobs.
    """
    global _threads
    root = validate(worker_threads, temp_dir)
    with _lock:
        if _redirect(root):
            raise ValueError("yzip-temp must not be a link or junction")
        os.makedirs(root, exist_ok=True)
        if root not in _sessions:
            session = os.path.join(root, "session-" + uuid.uuid4().hex)
            os.mkdir(session)
            marker = os.path.join(session, ".yzip-owner")
            file = None
            try:
                file = open(marker, "x+b")
                file.write(_MARKER)
                file.flush()
                _lease(file)
            except BaseException:
                if file is not None:
                    file.close()
                if os.path.isfile(marker):
                    os.remove(marker)
                os.rmdir(session)
                raise
            _sessions[root] = session, file
        session, _ = _sessions[root]
        # Probe again even for a previously configured directory.
        fd, probe = tempfile.mkstemp(prefix=".probe-", dir=session)
        os.close(fd)
        os.remove(probe)
        _threads = worker_threads
        tempfile.tempdir = session
        return session


def cleanup_temp(*, dry_run: bool = False, paths=None) -> dict | list[str]:
    """Delete only marked, unlocked session-UUID trees below configured roots.

    Never deletes a root, user-selected directory, unknown file, or redirected
    tree. Active sessions (including this process) are always skipped. Returns
    counts {removed, skipped, errors}; removed counts session directories.
    dry_run lists eligible paths; paths restricts deletion to that exact list,
    with ownership, containment, redirection and lease checks repeated at use.
    """
    result = {"removed": 0, "skipped": 0, "errors": 0}
    candidates = []
    if isinstance(paths, (str, bytes, os.PathLike)):
        raise ValueError("paths must be a collection of session paths")
    with _lock:
        selected = None if paths is None else {os.path.normcase(os.path.abspath(p)) for p in paths}
        for root in _sessions:
            if _redirect(root) or not os.path.isdir(root):
                continue
            try:
                names = os.listdir(root)
            except OSError:
                result["errors"] += 1
                continue
            for name in names:
                if not re.fullmatch(r"session-[0-9a-f]{32}", name):
                    continue
                path = os.path.join(root, name)
                if selected is not None and os.path.normcase(os.path.abspath(path)) not in selected:
                    continue
                owner = os.path.join(path, ".yzip-owner")
                if _redirect(path) or not os.path.isdir(path) or _redirect(owner):
                    result["skipped"] += 1
                    continue
                if any(path == s[0] for s in _sessions.values()):
                    result["skipped"] += 1
                    continue
                try:
                    with open(owner, "r+b") as file:
                        _lease(file)
                        if file.read(len(_MARKER) + 1) != _MARKER:
                            result["skipped"] += 1
                            continue
                        redirected = any(_redirect(os.path.join(d, n))
                                         for d, dirs, files in os.walk(path, followlinks=False)
                                         for n in dirs + files)
                        if redirected:
                            result["skipped"] += 1
                            continue
                        if dry_run:
                            candidates.append(path)
                            continue
                        # Claim only a verified abandoned session.
                        claimed = os.path.join(root, "deleting-" + uuid.uuid4().hex)
                        # Windows forbids renaming an open marker's parent directory;
                        # all participating processes honor this marker lock, and
                        # only abandoned sessions reach here.
                    os.rename(path, claimed)
                    # Verify the resolved final deletion target immediately before
                    # recursive removal. Only an immediate child of this root is owned.
                    resolved = os.path.realpath(claimed)
                    if (_redirect(claimed) or os.path.normcase(os.path.dirname(resolved)) !=
                            os.path.normcase(os.path.realpath(root))):
                        result["skipped"] += 1
                        continue
                    shutil.rmtree(claimed)
                    result["removed"] += 1
                except (PermissionError, BlockingIOError):
                    result["skipped"] += 1
                except OSError:
                    result["errors"] += 1
    return candidates if dry_run else result
