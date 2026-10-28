"""Optional ctypes loader for yzip's native acceleration bridge."""

from __future__ import annotations

import ctypes
import enum
import os
from pathlib import Path


ABI_VERSION = 1


class AccelStatus(enum.IntEnum):
    OK = 0
    CANCELLED = 1
    UNSUPPORTED = 2
    ERROR = 3


class Accelerator:
    def __init__(self, library: object, path: Path):
        self._library = library
        self.path = path

        library.yz_accel_abi_version.argtypes = []
        library.yz_accel_abi_version.restype = ctypes.c_uint32
        library.yz_accel_capabilities.argtypes = []
        library.yz_accel_capabilities.restype = ctypes.c_uint64
        library.yz_accel_last_error.argtypes = [
            ctypes.POINTER(ctypes.c_wchar),
            ctypes.c_size_t,
        ]
        library.yz_accel_last_error.restype = ctypes.c_size_t
        library.yz_accel_cancel.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
        library.yz_accel_cancel.restype = None

        self.abi_version = int(library.yz_accel_abi_version())
        self.capabilities = int(library.yz_accel_capabilities())

    def last_error(self) -> str:
        required = int(self._library.yz_accel_last_error(None, 0))
        if required <= 1:
            return ""
        buffer = ctypes.create_unicode_buffer(required)
        actual = int(self._library.yz_accel_last_error(buffer, required))
        if actual > required:
            buffer = ctypes.create_unicode_buffer(actual)
            self._library.yz_accel_last_error(buffer, actual)
        return buffer.value


class CancellationFlag:
    def __init__(self, accelerator: Accelerator):
        if accelerator is None:
            raise ValueError("accelerator is required")
        self._accelerator = accelerator
        self._value = ctypes.c_uint32(0)

    @property
    def cancelled(self) -> bool:
        return self._value.value != 0

    @property
    def pointer(self):
        return ctypes.pointer(self._value)

    def cancel(self) -> None:
        self._accelerator._library.yz_accel_cancel(self.pointer)


_disabled_reason: str | None = None


def _default_candidates() -> tuple[Path, ...]:
    root = Path(__file__).resolve().parent
    if getattr(__import__("sys"), "frozen", False):
        return (root / "tools" / "yzip_accel.dll",)
    return (
        root / "nativeaccel" / "build" / "yzip_accel.dll",
        root / "tools" / "yzip_accel.dll",
    )


def load_accelerator(path: os.PathLike[str] | str | None = None) -> Accelerator | None:
    global _disabled_reason

    candidates = (Path(path),) if path is not None else _default_candidates()
    failures: list[str] = []
    for candidate in candidates:
        try:
            library = ctypes.WinDLL(str(candidate))
            library.yz_accel_abi_version.argtypes = []
            library.yz_accel_abi_version.restype = ctypes.c_uint32
            abi_version = int(library.yz_accel_abi_version())
            if abi_version != ABI_VERSION:
                _disabled_reason = (
                    f"native accelerator ABI mismatch: expected {ABI_VERSION}, "
                    f"got {abi_version}"
                )
                return None
            accelerator = Accelerator(library, candidate.resolve())
        except (AttributeError, OSError) as error:
            failures.append(f"{candidate}: {error}")
            continue
        _disabled_reason = None
        return accelerator

    detail = "; ".join(failures)
    _disabled_reason = f"native accelerator DLL not found or could not be loaded: {detail}"
    return None


def disabled_reason() -> str | None:
    return _disabled_reason


def _reset_for_tests() -> None:
    global _disabled_reason
    _disabled_reason = None
