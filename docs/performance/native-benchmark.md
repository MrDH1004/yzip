# Native archive benchmark

`benchmark_native.py` compares the installed Bandizip console tool with the current yzip CLI on Windows. It covers ZIP and 7z compression and extraction at level 5 with 1, 4, 8, and 16 requested threads.

## Run

From the repository root:

```powershell
python benchmark_native.py --runs 7 --output .omc\artifacts\native-performance-baseline\baseline.json
```

The harness requires `C:\Program Files\Bandizip\bz.exe`. It generates a deterministic input tree in a temporary directory and prepares extraction archives with Bandizip. Every case gets one unreported warm-up followed by the requested number of measured runs. Every iteration uses a separate temporary output root; the harness neither clears operating-system caches nor changes installed applications.

Bandizip receives `-t:N`. The yzip subprocess calls `runtimeconfig.configure(N, temporary_root)` before invoking `yzip_cli.main`, so the requested worker limit applies inside the measured process.

## Report contract

The JSON document contains `schema_version`, `runs`, `warmup_runs`, and a `results` array. Each result records:

- the tool, format, operation, level, source, archive, and requested thread count;
- measured wall-time median and nearest-rank p95;
- median process CPU time and maximum peak resident working set;
- archive size, exact command, measured-run count, and SHA-256 tree result.

The tree digest includes each relative path, file size, and file content in sorted path order. Every extracted tree must equal the source digest; a mismatch aborts the run.

## 2026-10-28 baseline ledger

Raw artifact: `.omc/artifacts/native-performance-baseline/baseline.json` (git-ignored)

All 32 cases completed seven measured runs after one warm-up and produced the same source-tree digest, `48778bb31ba4…`. Wall medians in seconds:

| Tool | Format | Operation | t1 | t4 | t8 | t16 |
|---|---|---:|---:|---:|---:|---:|
| Bandizip | ZIP | compress | 0.0463 | 0.0381 | 0.0616 | 0.0452 |
| Bandizip | ZIP | extract | 0.0408 | 0.0393 | 0.0404 | 0.0424 |
| Bandizip | 7z | compress | 0.0291 | 0.0308 | 0.0404 | 0.0304 |
| Bandizip | 7z | extract | 0.0330 | 0.0366 | 0.0434 | 0.0377 |
| yzip | ZIP | compress | 0.2129 | 0.1717 | 0.1669 | 0.1858 |
| yzip | ZIP | extract | 0.2333 | 0.2107 | 0.2251 | 0.1937 |
| yzip | 7z | compress | 0.2412 | 0.2409 | 0.2263 | 0.3004 |
| yzip | 7z | extract | 0.4457 | 0.3779 | 0.3714 | 0.4102 |

The generated dataset is intentionally small and deterministic. These measurements are dominated by process startup and establish a reproducible comparison foundation; they are not representative of large production archives.
