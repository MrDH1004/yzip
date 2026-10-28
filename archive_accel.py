"""Optional ctypes loader for yzip's native acceleration bridge."""

from __future__ import annotations

import ctypes
import enum
import os
import threading
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Iterable


ABI_VERSION = 1
CAP_ZIP_EXTRACT = 1 << 0


class AccelStatus(enum.IntEnum):
    OK = 0
    CANCELLED = 1
    UNSUPPORTED = 2
    ERROR = 3
    CORRUPT = 4


@dataclass(frozen=True)
class NativeZipJob:
    source_index: int
    staging_path: str
    expected_size: int
    expected_crc: int


@dataclass(frozen=True)
class NativeResult:
    status: AccelStatus
    completed_bytes: int


class _CNativeZipJob(ctypes.Structure):
    _fields_ = [
        ("source_index", ctypes.c_uint32),
        ("staging_path", ctypes.c_wchar_p),
        ("expected_size", ctypes.c_uint64),
        ("expected_crc", ctypes.c_uint32),
    ]


class _CNativeProgress(ctypes.Structure):
    _fields_ = [
        ("completed_bytes", ctypes.c_uint64),
        ("total_bytes", ctypes.c_uint64),
    ]


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
        extract_zip = getattr(library, "yz_accel_extract_zip", None)
        if self.capabilities & CAP_ZIP_EXTRACT and extract_zip is None:
            raise AttributeError("missing required export: yz_accel_extract_zip")
        if extract_zip is not None:
            extract_zip.argtypes = [
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.POINTER(_CNativeZipJob),
                ctypes.c_size_t,
                ctypes.c_uint64,
                ctypes.POINTER(_CNativeProgress),
                ctypes.POINTER(ctypes.c_uint32),
            ]
            extract_zip.restype = ctypes.c_uint32

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

    def extract_zip(
        self,
        source: os.PathLike[str] | str,
        jobs: Iterable[NativeZipJob],
        progress: Callable[[int, int, str], None] | None,
        cancel: Callable[[], bool] | None,
    ) -> NativeResult:
        native_jobs = list(jobs)
        array_type = _CNativeZipJob * len(native_jobs)
        c_jobs = array_type(
            *(
                _CNativeZipJob(
                    job.source_index,
                    job.staging_path,
                    job.expected_size,
                    job.expected_crc,
                )
                for job in native_jobs
            )
        )
        total = sum(job.expected_size for job in native_jobs)
        counters = _CNativeProgress(0, total)
        cancellation = CancellationFlag(self)
        stop = threading.Event()
        callback_error: list[BaseException] = []

        def poll() -> None:
            while not stop.wait(0.1):
                try:
                    if cancel is not None and cancel():
                        cancellation.cancel()
                    if progress is not None:
                        progress(int(counters.completed_bytes), total, "")
                except BaseException as error:
                    callback_error.append(error)
                    cancellation.cancel()
                    return

        monitor = threading.Thread(target=poll, name="yzip-native-progress", daemon=True)
        monitor.start()
        try:
            raw_status = int(
                self._library.yz_accel_extract_zip(
                    str(_default_7z_path()),
                    os.fspath(source),
                    c_jobs,
                    len(native_jobs),
                    total,
                    ctypes.pointer(counters),
                    cancellation.pointer,
                )
            )
        finally:
            stop.set()
            monitor.join()
        if callback_error:
            raise callback_error[0]
        if progress is not None:
            progress(int(counters.completed_bytes), total, "")
        cancelled_after_completion = cancel is not None and cancel()
        try:
            status = AccelStatus(raw_status)
        except ValueError:
            status = AccelStatus.ERROR
        if cancelled_after_completion:
            status = AccelStatus.CANCELLED
        return NativeResult(status, int(counters.completed_bytes))


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


def _default_7z_path() -> Path:
    configured = os.environ.get("YZIP_7Z_DLL")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent / "tools" / "7z.dll"


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
