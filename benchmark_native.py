"""Reproducible Bandizip/yzip ZIP and 7z benchmark harness for Windows."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from ctypes import wintypes


ROOT = Path(__file__).resolve().parent
BZ_EXE = Path(r"C:\Program Files\Bandizip\bz.exe")
MB = 1024 * 1024


@dataclass(frozen=True)
class BenchmarkCase:
    tool: str
    format: str
    operation: str
    threads: int
    level: int
    source: Path
    archive: Path


@dataclass(frozen=True)
class ProcessSample:
    wall_seconds: float
    cpu_seconds: float
    peak_rss_bytes: int
    returncode: int
    archive_size_bytes: int = 0
    tree_sha256: str = ""
    command: tuple[str, ...] = ()


@dataclass(frozen=True)
class BenchmarkResult:
    case: BenchmarkCase
    measured_runs: int
    wall_median_seconds: float
    wall_p95_seconds: float
    cpu_median_seconds: float
    peak_rss_bytes: int
    archive_size_bytes: int
    requested_threads: int
    command: tuple[str, ...]
    sha256_tree: str

    def to_json(self) -> dict:
        result = asdict(self)
        result["case"]["source"] = str(self.case.source)
        result["case"]["archive"] = str(self.case.archive)
        result["command"] = list(self.command)
        return result


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def expand_cases(source: Path, archive_root: Path) -> list[BenchmarkCase]:
    source = Path(source)
    archive_root = Path(archive_root)
    return [
        BenchmarkCase(tool, archive_format, operation, threads, 5, source,
                      archive_root / f"baseline.{archive_format}")
        for tool in ("bandizip", "yzip")
        for archive_format in ("zip", "7z")
        for operation in ("compress", "extract")
        for threads in (1, 4, 8, 16)
    ]


def tree_sha256(root: Path) -> str:
    root = Path(root)
    digest = hashlib.sha256()
    for path in sorted((item for item in root.rglob("*") if item.is_file()),
                       key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        with path.open("rb") as file:
            for block in iter(lambda: file.read(MB), b""):
                digest.update(block)
    return digest.hexdigest()


def _yzip_command(arguments: Sequence[str], threads: int, temp_root: Path) -> list[str]:
    script = (
        "import runtimeconfig, yzip_cli; "
        f"runtimeconfig.configure({threads}, {str(temp_root)!r}); "
        f"raise SystemExit(yzip_cli.main({list(arguments)!r}))"
    )
    return [sys.executable, "-c", script]


def build_command(case: BenchmarkCase, output_root: Path) -> list[str]:
    output_root = Path(output_root)
    if case.operation == "compress":
        archive = output_root / f"result.{case.format}"
        if case.tool == "bandizip":
            return [str(BZ_EXE), "c", f"-fmt:{case.format}", f"-l:{case.level}",
                    f"-t:{case.threads}", "-y", str(archive), str(case.source)]
        return _yzip_command(
            ["c", str(archive), str(case.source), "-l", str(case.level), f"-fmt:{case.format}"],
            case.threads, output_root / "yzip-temp",
        )
    if case.operation == "extract":
        extracted = output_root / "extracted"
        if case.tool == "bandizip":
            return [str(BZ_EXE), "x", f"-t:{case.threads}", "-y", f"-o:{extracted}", str(case.archive)]
        return _yzip_command(
            ["x", str(case.archive), "-o", str(extracted)], case.threads, output_root / "yzip-temp",
        )
    raise ValueError(f"unsupported operation: {case.operation}")


def _filetime_seconds(value: wintypes.FILETIME) -> float:
    return ((value.dwHighDateTime << 32) | value.dwLowDateTime) / 10_000_000


def _measure_process(command: Sequence[str]) -> tuple[float, float, int, int]:
    started = time.perf_counter()
    process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    _, stderr = process.communicate()
    wall = time.perf_counter() - started
    if os.name != "nt":
        return wall, 0.0, 0, process.returncode

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    handle = wintypes.HANDLE(int(process._handle))
    created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
    if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                    ctypes.byref(kernel), ctypes.byref(user)):
        raise ctypes.WinError(ctypes.get_last_error())
    counters = _ProcessMemoryCounters(cb=ctypes.sizeof(_ProcessMemoryCounters))
    if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    if process.returncode:
        message = stderr.decode(errors="replace").strip()
        raise RuntimeError(f"command failed ({process.returncode}): {message}")
    return wall, _filetime_seconds(kernel) + _filetime_seconds(user), counters.PeakWorkingSetSize, process.returncode


def _extracted_source_root(output_root: Path, source: Path) -> Path:
    extracted = output_root / "extracted"
    nested = extracted / source.name
    return nested if nested.is_dir() else extracted


def _run_iteration(case: BenchmarkCase) -> ProcessSample:
    with tempfile.TemporaryDirectory(prefix="yzip-native-benchmark-") as temp:
        output_root = Path(temp)
        command = build_command(case, output_root)
        wall, cpu, peak_rss, returncode = _measure_process(command)
        archive = output_root / f"result.{case.format}"
        archive_size = archive.stat().st_size if archive.is_file() else case.archive.stat().st_size
        result_root = case.source if case.operation == "compress" else _extracted_source_root(output_root, case.source)
        return ProcessSample(wall, cpu, peak_rss, returncode, archive_size,
                             tree_sha256(result_root), tuple(command))


def _nearest_rank_p95(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def run_case(case: BenchmarkCase, runs: int = 7) -> BenchmarkResult:
    if runs < 1:
        raise ValueError("runs must be at least 1")
    expected_hash = tree_sha256(case.source)
    _run_iteration(case)  # warm-up is intentionally discarded
    samples = [_run_iteration(case) for _ in range(runs)]
    for sample in samples:
        if sample.tree_sha256 != expected_hash:
            raise RuntimeError("SHA-256 tree mismatch")
    walls = [sample.wall_seconds for sample in samples]
    return BenchmarkResult(
        case=case,
        measured_runs=runs,
        wall_median_seconds=statistics.median(walls),
        wall_p95_seconds=_nearest_rank_p95(walls),
        cpu_median_seconds=statistics.median(sample.cpu_seconds for sample in samples),
        peak_rss_bytes=max(sample.peak_rss_bytes for sample in samples),
        archive_size_bytes=int(statistics.median(sample.archive_size_bytes for sample in samples)),
        requested_threads=case.threads,
        command=samples[0].command,
        sha256_tree=expected_hash,
    )


def _make_dataset(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "empty.bin").write_bytes(b"")
    (root / "logs").mkdir()
    text = ("2026-10-28 INFO reproducible benchmark row\n" * 4096).encode("utf-8")
    for index in range(8):
        (root / "logs" / f"server-{index:02d}.log").write_bytes(text)
    random_bytes = bytes(range(256)) * 4096
    for index in range(2):
        (root / f"binary-{index}.dat").write_bytes(random_bytes)


def _prepare_archives(source: Path, archive_root: Path) -> None:
    archive_root.mkdir(parents=True, exist_ok=True)
    for archive_format in ("zip", "7z"):
        case = BenchmarkCase("bandizip", archive_format, "compress", 8, 5, source,
                             archive_root / f"baseline.{archive_format}")
        command = build_command(case, archive_root)
        generated = archive_root / f"result.{archive_format}"
        subprocess.run(command, cwd=ROOT, check=True)
        generated.replace(case.archive)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=7)
    args = parser.parse_args(argv)
    if not BZ_EXE.is_file():
        parser.error(f"Bandizip console tool not found: {BZ_EXE}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="yzip-native-dataset-") as temp:
        workspace = Path(temp)
        source = workspace / "dataset"
        archive_root = workspace / "archives"
        _make_dataset(source)
        _prepare_archives(source, archive_root)
        results = []
        for case in expand_cases(source, archive_root):
            print(f"{case.tool} {case.format} {case.operation} t={case.threads}", flush=True)
            results.append(run_case(case, args.runs).to_json())
        report = {"schema_version": 1, "runs": args.runs, "warmup_runs": 1, "results": results}
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
