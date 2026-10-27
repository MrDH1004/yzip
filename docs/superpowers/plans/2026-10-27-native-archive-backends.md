# Native Archive Backends Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace external extractor processes for RAR, ISO/CAB and EGG/ALZ with in-process backends while preserving yzip's common archive API and extraction safety behavior.

**Architecture:** Add a Python adapter that exposes the existing `Entry`/archive contract. Use a pinned libarchive DLL through an in-process C API for RAR/RAR5, ISO9660, CAB and other supported read-only formats; use one Rust cdylib containing the existing MIT `unegg` and `unalz` libraries for EGG/ALZ. Native readers always stage output and pass it through yzip's existing safe planner.

**Tech Stack:** Python 3.14, ctypes/libarchive C API, Rust 1.99, `unegg` 0.2.1, `unalz` 0.2.1, PyInstaller, unittest.

**Spec:** `docs/design/native-archive-backends.md`

## Global Constraints

- RAR remains read/list/test/extract only for unencrypted archives; encrypted RAR is detected and rejected without writing because the selected in-process reader does not implement RAR decryption. No RAR creation.
- No native backend may write directly into the user's destination directory.
- No child process may be used for a native-backed format after the migration.
- Existing ZIP/7z/TAR behavior must remain unchanged.
- Every native library and license must be pinned and bundled reproducibly.
- A missing backend must produce a clear application error, not a silent fallback.

## Review Focus

- RAR5 encrypted, multi-volume, and damaged archives.
- ISO9660/Joliet/Rock Ridge and CAB Unicode paths.
- EGG solid/encrypted/split archives and authentication failures.
- ALZ CP949/encrypted/split archives.
- Missing DLL/module and child-process regression detection.

---

### Task 1: Define the backend contract and failing routing tests

**Files:**
- Create: `nativearchive.py`
- Modify: `formats.py`
- Test: `test_formats.py`

**Interfaces:**
- `nativearchive.open_archive(path: str, kind: str, password: Optional[str]) -> ArchiveLike`
- `nativearchive.available(kind: str) -> bool`
- `formats.open_any()` routes `rar/cab/iso/egg/alz` to `nativearchive` before legacy `cliarchive`.

- [ ] **Step 1: Write failing tests** for routing a RAR, ISO, CAB, EGG, and ALZ fixture to the native adapter and for rejecting a missing backend with a named error.
- [ ] **Step 2: Run the focused tests** and confirm they fail because the adapter and routing do not exist.
- [ ] **Step 3: Add only the adapter protocol and routing seam**, with a deliberate `NativeBackendUnavailable` error for unimplemented kinds; do not add format parsing yet.
- [ ] **Step 4: Run the focused tests** and confirm routing assertions pass while capability tests remain red.

### Task 2: Add the pinned libarchive in-process reader

**Files:**
- Create: `nativearchive_libarchive.py`
- Create: `nativearchive_ffi.py`
- Modify: `requirements.txt`
- Modify: `fetch_tools.ps1`
- Modify: `.gitignore`
- Test: `test_nativearchive.py`

**Interfaces:**
- `LibArchive(path: str, password: Optional[str])` with `entries()`, `needs_password()`, `extract()`, `test()`, and `close()`.
- `fetch_tools.ps1` downloads a pinned self-contained `libarchive.dll` and verifies SHA-256.

- [x] **Step 1: Add real fixture tests** covering RAR/RAR5, ISO9660, CAB, Unicode names, path traversal, encrypted RAR, multi-volume RAR, damaged input, and no child-process invocation.
- [x] **Step 2: Run them and confirm the expected failure** is missing `libarchive.dll`/reader implementation.
- [x] **Step 3: Implement the minimal ctypes FFI** for reader initialization, support-format/filter registration, header iteration, data reads, password registration, error retrieval, and cleanup. Keep handles opaque and check every return code.
- [x] **Step 4: Implement `LibArchive`** so it maps headers to `Entry`, streams to yzip part files, and defers final placement to `_Planner`.
- [x] **Step 5: Run the focused native tests**, then the existing ZIP/7z/TAR tests.

### Task 3: Add the in-process EGG/ALZ Rust module

**Files:**
- Create: `nativearchive\Cargo.toml`
- Create: `nativearchive\src\lib.rs`
- Create: `nativearchive\src\lib.rs`
- Modify: `requirements.txt`
- Modify: `build.ps1`
- Modify: `.github\workflows\release.yml`
- Modify: `fetch_tools.ps1`
- Test: `test_nativearchive.py`

**Interfaces:**
- Export a stable C ABI from `yzip_native.dll`: version, EGG/ALZ list-to-JSON, extract-to-staging, test, error-string, and free-string functions.
- Python adapter loads the DLL with `ctypes` and never invokes `unegg.exe` or `unalz.exe`.

- [x] **Step 1: Add failing no-subprocess tests** for EGG/ALZ normal, encrypted, split, CP949, corrupt, and path traversal fixtures.
- [x] **Step 2: Run tests and confirm they fail** because the native DLL is absent.
- [x] **Step 3: Add Rust dependencies** pinned to the reviewed `unegg`/`unalz` commits and expose only the narrow C ABI needed by Python.
- [x] **Step 4: Make Rust extraction target a staging directory** and return structured metadata/errors; do not expose raw destination writes to Python.
- [x] **Step 5: Build the DLL locally and bundle it in PyInstaller/CI.**
- [x] **Step 6: Run the EGG/ALZ focused tests and confirm no child process is created.**

### Task 4: Remove legacy process paths and update format capabilities

**Files:**
- Modify: `formats.py`
- Modify: `cliarchive.py`
- Modify: `fetch_tools.ps1`
- Modify: `build.ps1`
- Modify: `README.md`
- Modify: `installer\INFO.txt`
- Test: `test_moreformats.py`, `test_formats.py`

- [x] **Step 1: Add regression tests** that fail if native-backed formats call `subprocess.Popen`; legacy executable presence is not required for native-backed tests.
- [x] **Step 2: Route all migrated kinds to native backends** and keep `cliarchive.py` only for a temporary compatibility list not yet covered by libarchive.
- [x] **Step 3: Update format labels** to distinguish read-only native support from writable formats.
- [x] **Step 4: Remove obsolete extractor downloads only after the native fixture suite passes.**
- [x] **Step 5: Update documentation and third-party license notices.**

### Task 5: Build, security, and full verification

**Files:**
- Modify: `.github\workflows\release.yml`
- Modify: `run_headless_tests.py`
- Modify: `README.md`
- Test: all existing suites plus `test_nativearchive.py`

- [x] **Step 1: Add CI checks** for native DLL presence, architecture, SHA-256, and dependency loading before tests.
- [x] **Step 2: Run compile checks and focused native tests.**
- [x] **Step 3: Run `python run_headless_tests.py` with `PYTHONUTF8=1` and record every suite result.
- [x] **Step 4: Run the local PyInstaller app build and verify the bundled native binaries.
- [x] **Step 5: Inspect the final diff for TODOs, placeholders, skipped tests, `.only`, and accidental subprocess paths.
- [x] **Step 6: Run the full verification command one final time and report exact counts and any remaining limitations.
