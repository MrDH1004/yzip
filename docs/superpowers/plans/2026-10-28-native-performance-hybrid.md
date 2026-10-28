# yzip Native Performance Hybrid Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep yzip's fast 7z compression while accelerating plain ZIP compression/extraction and plain 7z extraction through an in-process native DLL, enabling only paths that prove faster and equally safe.

**Architecture:** A small MSVC C++ bridge (`yzip_accel.dll`) loads the pinned official `7z.dll` and exposes a narrow C ABI. Python owns archive policy, safe destinations, transactions, fallback, cancellation, and progress polling; native code owns only codec-heavy work. Existing Python engines remain the compatibility and failure fallback.

**Tech Stack:** Python 3.14, C++17/MSVC x64 `/O2 /GL /MT`, official 7-Zip 26.05 DLL interfaces, ctypes, unittest, PowerShell build scripts.

**Spec:** `docs/superpowers/specs/2026-10-28-native-performance-hybrid-design.md`

## Global Constraints

- Windows 10 1809 x64 minimum; no external `7z.exe` execution.
- Keep current 7z compression path and reject regressions greater than 5% in its 8-thread median.
- Native acceleration is optional; load, ABI, unsupported method, AES, split archive, and mixed-method failures fall back to the existing engine.
- Source archives and existing outputs remain untouched on cancellation, CRC failure, write failure, or native error.
- ZIP compression output at the same level may not exceed current yzip by more than `max(2%, 64 KiB)` before default enablement.
- Benchmark gate: level 5, 8 threads, five dataset classes, warm-up plus at least seven measured runs, median and p95, cross-extraction SHA-256 verification.
- Every production behavior change follows RED→GREEN TDD.

## Review Focus

- A crafted archive causes the native bridge to report an error without writing outside the supplied staging directory.
- Cancellation during a native callback leaves original archives and pre-existing outputs byte-identical.
- An archive containing one unsupported or encrypted member selects one engine for the whole operation and never publishes a partial mixed result.
- ABI/version mismatch or missing `7z.dll` falls back without changing normal Python behavior.
- 32/64-bit sizes, very long paths, zero-byte files, duplicate case-insensitive names, and output limits preserve existing policies.

---

### Task 1: Reproducible Bandizip Benchmark Harness

**Files:**
- Create: `benchmark_native.py`
- Create: `test_benchmark_native.py`
- Create: `docs/performance/native-benchmark.md`

**Interfaces:**
- Produces: `BenchmarkCase`, `BenchmarkResult`, `run_case(case, runs=7)`, JSON report containing wall median, p95, CPU time, peak RSS, archive size, requested threads, command, and SHA-256 tree result.
- Consumes: `runtimeconfig.configure(threads)`, installed `bz.exe`, current yzip CLI.

- [ ] **Step 1: Write failing tests** for deterministic case expansion, warm-up exclusion, median/p95 calculation, requested-thread propagation, and hash mismatch failure.
- [ ] **Step 2: Run** `python -m unittest test_benchmark_native -v`; expect failures because the harness does not exist.
- [ ] **Step 3: Implement the harness** with subprocess wall/CPU/RSS collection and separate temporary output roots; never clear OS caches or alter installed applications.
- [ ] **Step 4: Run** `python -m unittest test_benchmark_native -v`; expect all tests to pass.
- [ ] **Step 5: Capture baseline** for ZIP/7z compression and extraction at 1/4/8/16 threads, level 5, with seven measured runs; write raw JSON under `.omc/artifacts/native-performance-baseline/` and summarize it in the task ledger.
- [ ] **Step 6: Commit** `test: add reproducible Bandizip performance harness`.

### Task 2: Native DLL ABI, Loader, Cancellation, and Fallback

**Files:**
- Create: `nativeaccel/yzip_accel.h`
- Create: `nativeaccel/yzip_accel.cpp`
- Create: `nativeaccel/build.ps1`
- Create: `archive_accel.py`
- Create: `test_archive_accel.py`
- Modify: `fetch_tools.ps1`
- Modify: `build.ps1`

**Interfaces:**
- Produces C ABI: `yz_accel_abi_version() -> uint32_t`, `yz_accel_capabilities() -> uint64_t`, `yz_accel_last_error(wchar_t*, size_t) -> size_t`, `yz_accel_cancel(volatile uint32_t*)` convention, and opaque operation entry points added by Tasks 3–5.
- Produces Python: `AccelStatus`, `load_accelerator() -> Accelerator | None`, `Accelerator.capabilities`, `CancellationFlag`, `disabled_reason() -> str | None`.
- Consumes: pinned `tools/7z.dll`, official headers copied/downloaded at build time with verified source revision.

- [ ] **Step 1: Write failing loader tests** for missing DLL, wrong ABI, capability mask, Unicode error retrieval, and atomic cancellation flag.
- [ ] **Step 2: Run** `python -m unittest test_archive_accel -v`; expect missing module/API failures.
- [ ] **Step 3: Implement the minimal stub DLL and ctypes loader**, checking every Win32/DLL call and exact-width type; exceptions never cross the C ABI.
- [ ] **Step 4: Run** `nativeaccel\build.ps1` and `python -m unittest test_archive_accel -v`; expect pass.
- [ ] **Step 5: Add build/fetch integration** so the release contains `yzip_accel.dll`, official license text, and pinned 7-Zip binary/header hashes.
- [ ] **Step 6: Run** `python -m unittest test_archive_accel test_nativearchive -v`; expect pass.
- [ ] **Step 7: Commit** `feat: add optional native acceleration bridge`.

### Task 3: Transactional Native ZIP Extraction

**Files:**
- Modify: `nativeaccel/yzip_accel.h`
- Modify: `nativeaccel/yzip_accel.cpp`
- Modify: `archive_accel.py`
- Modify: `ziplib.py`
- Create: `test_zip_native.py`
- Modify: `test_ziplib.py`

**Interfaces:**
- Produces: `Accelerator.extract_zip(source, jobs, progress, cancel) -> NativeResult`; `jobs` contains source index, sanitized staging path, expected size, CRC.
- Consumes: Task 2 loader/cancellation, existing `safe_target`, collision planning, `part_path`, and `finish_part`.

- [ ] **Step 1: Write failing selection tests** proving plain Store/Deflate uses native when available and AES, split, unsupported, or mixed-method archives use Python for the whole operation.
- [ ] **Step 2: Write failing transaction tests** for cancellation, CRC error, native error, pre-existing output preservation, zip-slip/device names, duplicate case-insensitive names, zero-byte and >4GiB metadata.
- [ ] **Step 3: Run** `python -m unittest test_zip_native -v`; expect native extraction API/selection failures.
- [ ] **Step 4: Implement ZIP extraction callbacks** from the official 7-Zip interface into supplied staging files, with output-limit accounting and throttled progress counters.
- [ ] **Step 5: Integrate in `ziplib.Archive.extract`** behind capability/eligibility checks; publish only after all selected members validate.
- [ ] **Step 6: Run** `python -m unittest test_zip_native test_ziplib test_zip_extract_perf -v`; expect pass.
- [ ] **Step 7: Benchmark** five dataset classes and enable by default only where median improves and integrity passes.
- [ ] **Step 8: Commit** `feat: accelerate transactional ZIP extraction`.

### Task 4: Native Plain 7z Extraction with Whole-Archive Fallback

**Files:**
- Modify: `nativeaccel/yzip_accel.h`
- Modify: `nativeaccel/yzip_accel.cpp`
- Modify: `archive_accel.py`
- Modify: `formats.py`
- Create: `test_7z_native.py`
- Modify: `test_formats.py`

**Interfaces:**
- Produces: `Accelerator.inspect_7z(source) -> NativeArchiveInfo`; `Accelerator.extract_7z(source, jobs, progress, cancel) -> NativeResult` for plain LZMA/LZMA2.
- Consumes: Task 2 bridge, existing `_Planner`, overwrite rules, staging/commit logic; unsupported coder and AES return structured fallback codes before output starts.

- [ ] **Step 1: Write failing eligibility tests** for plain LZMA/LZMA2, AES fallback, unsupported coder fallback, solid/non-solid, selected members, duplicates, and long paths.
- [ ] **Step 2: Write failing safety tests** for cancellation, corruption, output limits, symlink metadata, existing output preservation, and no partial fallback after native output begins.
- [ ] **Step 3: Run** `python -m unittest test_7z_native -v`; expect API/selection failures.
- [ ] **Step 4: Implement 7z inspection/extraction** through `IInArchive` and staged outputs; return `UNSUPPORTED_BEFORE_START` only before any write.
- [ ] **Step 5: Integrate in `SevenZArchive.extract`** and keep `fast_plan`/py7zr as whole-operation fallback.
- [ ] **Step 6: Run** `python -m unittest test_7z_native test_formats test_seven_io_performance -v`; expect pass.
- [ ] **Step 7: Benchmark** five datasets and record native/fallback selection plus median/p95.
- [ ] **Step 8: Commit** `feat: accelerate plain 7z extraction`.

### Task 5: Native Plain ZIP Compression

**Files:**
- Modify: `nativeaccel/yzip_accel.h`
- Modify: `nativeaccel/yzip_accel.cpp`
- Modify: `archive_accel.py`
- Modify: `zipwriter.py`
- Create: `test_zip_compress_native.py`
- Modify: `test_zipwriter.py`

**Interfaces:**
- Produces: `Accelerator.create_zip(items, output_part, level, threads, progress, cancel) -> NativeResult` for new plain Store/Deflate ZIPs.
- Consumes: normalized item metadata and Task 2 cancellation/progress; AES, split, append/edit remain in `zipwriter.write_zip`.

- [ ] **Step 1: Write failing selection tests** for plain new archives versus AES, split, edit/copy, and unavailable accelerator fallback.
- [ ] **Step 2: Write failing compatibility/transaction tests** for Unicode/CP949 names, empty files/directories, ZIP64 metadata, changing source, cancellation, old output preservation, and Bandizip/Python cross-read.
- [ ] **Step 3: Run** `python -m unittest test_zip_compress_native -v`; expect API/selection failures.
- [ ] **Step 4: Implement `IOutArchive` callbacks** with level/thread properties, ordered metadata, unique temporary output, and structured errors.
- [ ] **Step 5: Integrate in `zipwriter.write_zip`** only for eligible new archives; verify result before atomic publication.
- [ ] **Step 6: Run** `python -m unittest test_zip_compress_native test_zipwriter test_zipwriter_performance -v`; expect pass.
- [ ] **Step 7: Compare seven-run medians and output sizes**; default-enable only cases meeting speed, integrity, and `max(2%, 64 KiB)` size gates.
- [ ] **Step 8: Commit** `feat: accelerate plain ZIP compression`.

### Task 6: Packaging, Kill Switch, Full Benchmark, and Release Evidence

**Files:**
- Modify: `.github/workflows/release.yml`
- Modify: `build.ps1`
- Modify: `fetch_tools.ps1`
- Modify: `README.md`
- Create: `docs/performance/native-results.md`
- Modify: relevant installer/package manifests discovered during implementation.

**Interfaces:**
- Consumes all Tasks 1–5.
- Produces: packaged `yzip_accel.dll`, licenses, `YZIP_DISABLE_ACCEL=1` kill switch, benchmark report and raw-result location.

- [ ] **Step 1: Write failing package tests** asserting DLL/license inclusion, ABI smoke load from staged app, and kill-switch fallback.
- [ ] **Step 2: Run package tests**; expect missing staged DLL/kill-switch behavior.
- [ ] **Step 3: Implement packaging and CI checks**, including MSVC release build and clean-runner load test.
- [ ] **Step 4: Run full Python tests, native build tests, shell tests, and staged-app smoke tests**; expect zero failures.
- [ ] **Step 5: Run the full Bandizip matrix** at 1/4/8/16 threads and seven measured runs; verify cross-extraction hashes and current 7z compression regression gate.
- [ ] **Step 6: Write `native-results.md`** with raw numbers, conditions, wins, misses, memory, archive sizes, and unapplied paths; never claim parity for a missed condition.
- [ ] **Step 7: Run `git diff --check` and scan for TODO/FIXME/test.skip/.only**; expect clean output.
- [ ] **Step 8: Commit** `docs: record native acceleration results and packaging`.

## Self-Review Record

- Spec coverage: benchmark, DLL ABI, ZIP extraction, 7z extraction, ZIP compression, fallback, safety, packaging, licensing, and reporting all map to Tasks 1–6.
- Shared interfaces: Task 2 owns loader/cancellation; Tasks 3–5 consume it; Task 6 consumes all public behavior. Names match in every task.
- Review focus tests are assigned to Tasks 2–5 rather than left only for final review.
- 7z AES native acceleration is intentionally excluded and tested as fallback.
