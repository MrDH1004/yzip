"""Run every headless suite, each GUI group in its own process. Exit code 0 only if everything passes."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
env = dict(os.environ, YZIP_GATHER_SCALE="0.05", PYTHONUTF8="1")
GUI_GROUPS = ["test_run_job", "test_archive_verb", "test_extract_verbs", "test_main_window_ops", "test_drop_and_corrupt",
              "test_formats_gui", "test_simple_window", "test_settings", "test_update_prompt", "test_update_check_scope"]

jobs = [("core unit tests", [sys.executable, "-W", "ignore", "-m", "unittest", "test_ziplib"]),
        ("zip writer (zip64, AES, store, bz.exe/tar interop)", [sys.executable, "-W", "ignore", "-m", "unittest", "test_zipwriter"]),
        ("other formats (7z, tar.*, gz/bz2/xz/zst, bz.exe interop)", [sys.executable, "-W", "ignore", "-m", "unittest", "test_formats"]),
        ("native archives (RAR/ISO/CAB/EGG/ALZ)", [sys.executable, "-W", "ignore", "-m", "unittest", "test_nativearchive"]),
        ("right-click quick compress entry (Qt only when needed)", [sys.executable, "-W", "ignore", "-m", "unittest", "test_quick_entry"]),
        ("updater (local fake GitHub API, SHA-256 check)", [sys.executable, "-W", "ignore", "-m", "unittest", "test_updater"]),
        ("file associations (throwaway .yzt)", [sys.executable, "-W", "ignore", "-m", "unittest", "test_assoc"]),
        ("cli smoke", [sys.executable, "-W", "ignore", "smoke_test.py"]),
        ("right-click aggregation", [sys.executable, "-W", "ignore", "test_aggregation.py"])]
jobs += [(f"gui: {g}", [sys.executable, "-W", "ignore", "test_gui_flows.py", g]) for g in GUI_GROUPS]

bad = 0
for name, cmd in jobs:
    r = subprocess.run(cmd, cwd=HERE, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout + r.stderr)
    fails = [l for l in out.splitlines() if l.startswith("FAIL")]
    status = "OK  " if r.returncode == 0 and not fails else "FAIL"
    if status == "FAIL":
        bad += 1
    print(f"{status} {name}  (exit {r.returncode})")
    for l in fails:
        print("     " + l)
    if status == "FAIL" and not fails:
        print("     " + out[-600:].replace("\n", "\n     "))
print("\nALL PASSED" if not bad else f"\n{bad} suite(s) FAILED")
sys.exit(1 if bad else 0)
