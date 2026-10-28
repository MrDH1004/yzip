"""Reproducible Bandizip/yzip ZIP and 7z benchmark harness for Windows."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import random
import shutil
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
LARGE_TEXT_BYTES = 8 * MB
INCOMPRESSIBLE_BYTES = 8 * MB
LARGE_FILE_BYTES = 32 * MB
DATASET_NAMES = ("large-text", "incompressible", "small-files", "mixed-project", "large-file")
DEFAULT_BASELINE = ROOT / "docs" / "performance" / "native-benchmark-baseline.json"


@dataclass(frozen=True)
class BenchmarkCase:
    dataset: str
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


def expand_cases(datasets: dict[str, Path], archive_root: Path) -> list[BenchmarkCase]:
    archive_root = Path(archive_root)
    return [
        BenchmarkCase(dataset, tool, archive_format, operation, threads, 5, Path(source),
                      archive_root / dataset / f"{('yzip' if tool == 'bandizip' else 'bandizip')}.{archive_format}")
        for dataset, source in datasets.items()
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


def _extract_command(tool: str, archive: Path, destination: Path, threads: int,
                     temp_root: Path) -> list[str]:
    if tool == "bandizip":
        return [str(BZ_EXE), "x", f"-t:{threads}", "-y", f"-o:{destination}", str(archive)]
    return _yzip_command(["x", str(archive), "-o", str(destination)], threads, temp_root)


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


def _run_unmeasured(command: Sequence[str]) -> None:
    completed = subprocess.run(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if completed.returncode:
        message = completed.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"cross-extraction failed ({completed.returncode}): {message}")


def _extracted_source_root(output_root: Path, source: Path) -> Path:
    extracted = output_root / "extracted"
    nested = extracted / source.name
    return nested if nested.is_dir() else extracted


def _source_root(destination: Path, source: Path) -> Path:
    nested = destination / source.name
    return nested if nested.is_dir() else destination


def verify_generated_archive(case: BenchmarkCase, archive: Path, destination: Path) -> str:
    opposite = "bandizip" if case.tool == "yzip" else "yzip"
    command = _extract_command(opposite, archive, destination, case.threads,
                               destination.parent / "cross-yzip-temp")
    _run_unmeasured(command)
    actual = tree_sha256(_source_root(destination, case.source))
    expected = tree_sha256(case.source)
    if actual != expected:
        raise RuntimeError("cross-extraction SHA-256 mismatch")
    return actual


def _run_iteration(case: BenchmarkCase) -> ProcessSample:
    with tempfile.TemporaryDirectory(prefix="yzip-native-benchmark-") as temp:
        output_root = Path(temp)
        command = build_command(case, output_root)
        wall, cpu, peak_rss, returncode = _measure_process(command)
        archive = output_root / f"result.{case.format}"
        archive_size = archive.stat().st_size if archive.is_file() else case.archive.stat().st_size
        if case.operation == "compress":
            result_hash = verify_generated_archive(case, archive, output_root / "cross-extracted")
        else:
            result_hash = tree_sha256(_extracted_source_root(output_root, case.source))
        return ProcessSample(wall, cpu, peak_rss, returncode, archive_size,
                             result_hash, tuple(command))


def _nearest_rank_p95(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def run_case(case: BenchmarkCase, runs: int = 7) -> BenchmarkResult:
    if runs < 1:
        raise ValueError("runs must be at least 1")
    expected_hash = tree_sha256(case.source)
    warmup = _run_iteration(case)
    if warmup.tree_sha256 != expected_hash:
        raise RuntimeError("warm-up SHA-256 tree mismatch")
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


def _write_repeated(path: Path, block: bytes, size: int) -> None:
    with path.open("wb") as file:
        remaining = size
        while remaining:
            chunk = block[:remaining]
            file.write(chunk)
            remaining -= len(chunk)


def make_datasets(root: Path, project_source: Path = ROOT) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    datasets = {name: root / name for name in DATASET_NAMES}

    datasets["large-text"].mkdir()
    line = b"2026-10-28T12:34:56 INFO worker=8 reproducible native archive benchmark row\n"
    _write_repeated(datasets["large-text"] / "application.log", line * 8192, LARGE_TEXT_BYTES)

    datasets["incompressible"].mkdir()
    random_bytes = random.Random(20261028).randbytes(INCOMPRESSIBLE_BYTES)
    (datasets["incompressible"] / "deterministic.bin").write_bytes(random_bytes)

    datasets["small-files"].mkdir()
    for index in range(1500):
        group = datasets["small-files"] / f"group-{index // 100:02d}"
        group.mkdir(exist_ok=True)
        (group / f"item-{index:04d}.txt").write_text(
            f"small deterministic file {index:04d}\n" * 4, encoding="utf-8"
        )

    shutil.copytree(
        project_source, datasets["mixed-project"],
        ignore=shutil.ignore_patterns(".git", ".omc", ".superpowers", ".worktrees", "tools",
                                      "build", "dist", "target", "__pycache__", "*.pyc"),
    )

    datasets["large-file"].mkdir()
    large_block = (b"large-file-block:" + bytes(range(256))) * 4096
    _write_repeated(datasets["large-file"] / "disk-image.bin", large_block, LARGE_FILE_BYTES)
    return datasets


def _prepare_archives(datasets: dict[str, Path], archive_root: Path) -> None:
    for dataset, source in datasets.items():
        dataset_root = archive_root / dataset
        dataset_root.mkdir(parents=True, exist_ok=True)
        for tool in ("bandizip", "yzip"):
            for archive_format in ("zip", "7z"):
                case = BenchmarkCase(dataset, tool, archive_format, "compress", 8, 5, source,
                                     dataset_root / f"{tool}.{archive_format}")
                with tempfile.TemporaryDirectory(prefix="yzip-native-prepare-") as temp:
                    temp_root = Path(temp)
                    command = build_command(case, temp_root)
                    _run_unmeasured(command)
                    generated = temp_root / f"result.{archive_format}"
                    shutil.move(generated, case.archive)


def _seven_zip_medians(results: Sequence[BenchmarkResult]) -> dict[str, float]:
    return {
        result.case.dataset: result.wall_median_seconds
        for result in results
        if result.case.tool == "yzip" and result.case.format == "7z"
        and result.case.operation == "compress" and result.case.threads == 8
    }


def enforce_7z_regression_gate(current: dict[str, float], baseline: dict[str, float],
                               limit: float = 0.05) -> dict[str, float]:
    if set(current) != set(baseline):
        raise ValueError("7z regression datasets do not match")
    regressions = {name: current[name] / baseline[name] - 1.0 for name in current}
    failed = [(name, value) for name, value in regressions.items() if value > limit + 1e-12]
    if failed:
        name, value = failed[0]
        raise RuntimeError(f"7z 8-thread compression regression for {name}: {value:.2%} > {limit:.2%}")
    return regressions


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(MB), b""):
            digest.update(block)
    return digest.hexdigest()


def _execute_matrix(runs: int) -> tuple[list[BenchmarkResult], float]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="yzip-native-dataset-") as temp:
        workspace = Path(temp)
        datasets = make_datasets(workspace / "datasets")
        archive_root = workspace / "archives"
        _prepare_archives(datasets, archive_root)
        results = []
        for case in expand_cases(datasets, archive_root):
            print(f"{case.dataset} {case.tool} {case.format} {case.operation} t={case.threads}", flush=True)
            results.append(run_case(case, runs))
    return results, time.perf_counter() - started


def _write_summary(path: Path, raw_path: Path, results: Sequence[BenchmarkResult], runs: int,
                   elapsed: float, regressions: dict[str, float]) -> None:
    current_medians = _seven_zip_medians(results)
    dataset_hashes = {
        result.case.dataset: result.sha256_tree
        for result in results
        if result.case.tool == "yzip" and result.case.format == "7z"
        and result.case.operation == "compress" and result.case.threads == 8
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "schema_version": 1,
        "raw_json_path": str(raw_path),
        "raw_json_sha256": _sha256_file(raw_path),
        "dataset_count": len(DATASET_NAMES),
        "case_count": len(results),
        "measured_runs_per_case": runs,
        "warmup_runs_per_case": 1,
        "elapsed_seconds": elapsed,
        "dataset_sha256_trees": dataset_hashes,
        "seven_zip_8_thread_medians_seconds": current_medians,
        "regression_fraction": regressions,
    }
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--regression-baseline", type=Path)
    parser.add_argument("--create-baseline", action="store_true")
    parser.add_argument("--runs", type=int, default=7)
    args = parser.parse_args(argv)
    if not BZ_EXE.is_file():
        parser.error(f"Bandizip console tool not found: {BZ_EXE}")
    if args.create_baseline:
        if args.regression_baseline:
            parser.error("--create-baseline cannot be combined with --regression-baseline")
        if not args.summary:
            parser.error("--create-baseline requires --summary for the new baseline manifest")
        baseline_path = None
    else:
        baseline_path = args.regression_baseline or DEFAULT_BASELINE
        if not baseline_path.is_file():
            parser.error(f"regression baseline not found: {baseline_path}")
    output_resolved = args.output.resolve()
    summary_resolved = args.summary.resolve() if args.summary else None
    baseline_resolved = baseline_path.resolve() if baseline_path else None
    if summary_resolved and output_resolved == summary_resolved:
        parser.error("--output and --summary must use different paths")
    if baseline_resolved and output_resolved == baseline_resolved:
        parser.error("--output must not overwrite the regression baseline")
    if baseline_resolved and summary_resolved == baseline_resolved:
        parser.error("--summary must not overwrite the regression baseline")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    results, elapsed = _execute_matrix(args.runs)
    current_medians = _seven_zip_medians(results)
    if baseline_path:
        baseline_doc = json.loads(baseline_path.read_text(encoding="utf-8"))
        baseline_medians = baseline_doc["seven_zip_8_thread_medians_seconds"]
        regressions = enforce_7z_regression_gate(current_medians, baseline_medians)
    else:
        regressions = {name: 0.0 for name in current_medians}
    report = {"schema_version": 2, "runs": args.runs, "warmup_runs": 1,
              "dataset_count": len(DATASET_NAMES), "regression_fraction": regressions,
              "results": [result.to_json() for result in results]}
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.summary:
        _write_summary(args.summary, args.output, results, args.runs, elapsed, regressions)
    return 0


def cli(argv: Sequence[str] | None = None) -> int:
    try:
        return main(argv)
    except SystemExit as error:
        return int(error.code or 0)
    except (OSError, ValueError, RuntimeError, KeyError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
