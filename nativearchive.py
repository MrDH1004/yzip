"""In-process archive backend boundary.

The concrete libarchive and legacy EGG/ALZ readers are loaded lazily so the
ordinary ZIP/7z paths remain usable when the optional native payload is not
installed.  Format adapters must expose the same methods as the existing
archive classes: entries, needs_password, extract, test, close, and context
manager support.
"""
from __future__ import annotations

from typing import Optional


NATIVE_KINDS = frozenset({
    "rar", "cab", "iso", "udf", "img", "lzh", "lha", "cpio", "rpm", "deb", "egg", "alz",
})


class NativeBackendUnavailable(RuntimeError):
    """The in-process native reader is not present or could not be loaded."""


def open_archive(path: str, kind: str, password: Optional[str] = None):
    """Open *path* using the in-process backend for *kind*.

    The two backends are imported lazily so the ordinary ZIP/7z paths do not
    pay native-library startup cost.  A missing backend is deliberate: after
    migration, falling back to a child process would make the advertised
    in-process guarantee false.
    """
    if kind in ("egg", "alz"):
        from nativearchive_legacy import LegacyArchive
        return LegacyArchive(path, kind, password)
    if kind in NATIVE_KINDS:
        from nativearchive_libarchive import LibArchive
        return LibArchive(path, kind, password)
    raise NativeBackendUnavailable(f"no in-process archive backend for {kind}")


def available(kind: str) -> bool:
    if kind in ("egg", "alz"):
        from nativearchive_legacy import available as legacy_available
        return legacy_available()
    if kind in NATIVE_KINDS:
        from nativearchive_libarchive import LibArchive
        return LibArchive.available()
    return False
