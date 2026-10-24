"""Performance check of yzip against Bandizip (bz.exe) and Windows tar on identical generated data.

Measures wall time, CPU time, peak memory (of the tool's own process), archive size; every archive is
cross-checked by extracting it with a DIFFERENT tool and comparing SHA-256 of every file with the source.
usage: python perf_suite.py [--quick] [--json out.json]
"""
import hashlib
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile
import ctypes
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
BZ = r"C:\Program Files\Bandizip\bz.exe"
CLI = [sys.executable, "-W", "ignore", os.path.join(HERE, "yzip_cli.py")]
EXE = os.path.join(HERE, "dist", "yzip", "yzip.exe")
QUICK = "--quick" in sys.argv
RUNS = 1 if QUICK else 3
MB = 1024 * 1024


# ---------- process measurement (wall, CPU, peak working set) ----------
class PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


_k32 = ctypes.WinDLL("kernel32")
_psapi = ctypes.WinDLL("psapi")
_k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
_psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]


def run(cmd, env=None, timeout=3600):
    t = time.perf_counter()
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    p.wait(timeout)
    wall = time.perf_counter() - t
    h = wintypes.HANDLE(int(p._handle))
    c, e, k, u = (wintypes.FILETIME() for _ in range(4))
    _k32.GetProcessTimes(h, c, e, k, u)
    ft = lambda f: ((f.dwHighDateTime << 32) | f.dwLowDateTime) / 1e7  # noqa: E731
    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    _psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
    return {"wall": wall, "cpu": ft(k) + ft(u), "peak_mb": pmc.PeakWorkingSetSize / MB, "rc": p.returncode}


# ---------- datasets ----------
def make_text(d, total_mb):
    os.makedirs(d)
    rnd = random.Random(1)
    words = ["w%x" % rnd.randrange(1 << 20) for _ in range(8000)]
    per = total_mb // 3
    for i in range(3):
        with open(os.path.join(d, f"server{i}.log"), "w", encoding="utf-8") as f:
            written = 0
            while written < per * MB:
                block = "".join(f"2026-10-{rnd.randrange(1, 29):02d} INFO {' '.join(rnd.choices(words, k=9))}\n"
                                for _ in range(20000))
                f.write(block)
                written += len(block)


def make_photos(d, n, size_mb):
    os.makedirs(d)
    for i in range(n):  # already-compressed data (JPEG/MP4-like): nothing left to squeeze
        with open(os.path.join(d, f"IMG_{i:04d}.jpg"), "wb") as f:
            f.write(os.urandom(size_mb * MB))


def make_small(d):
    # any source tree with thousands of small files; YZIP_SMALL_SRC overrides the default
    src = os.environ.get("YZIP_SMALL_SRC") or os.path.join(os.path.dirname(HERE), "rea")
    shutil.copytree(src, d, ignore=shutil.ignore_patterns(".git", "node_modules"))


def make_huge(d, gb):
    os.makedirs(d)
    rnd = random.Random(2)
    text = "".join(f"line {i} {' '.join(str(rnd.random()) for _ in range(6))}\n" for i in range(20000)).encode()
    text = (text * (MB // len(text) + 1))[:MB]
    with open(os.path.join(d, "disk.img"), "wb") as f:
        for i in range(int(gb * 1024)):
            f.write(os.urandom(MB) if i % 3 == 0 else text)


def tree_size(p):
    return sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(p) for f in fs)


def manifest(root):
    out = {}
    for dp, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(dp, f)
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for b in iter(lambda: fh.read(4 * MB), b""):
                    h.update(b)
            out[os.path.relpath(p, root).replace("\\", "/")] = h.hexdigest()
    return out


# ---------- tools ----------
def env_exe(t):
    return dict(os.environ, QT_QPA_PLATFORM="offscreen", YZIP_NO_OPEN="1", LOCALAPPDATA=os.path.join(t, "local"))


def tool_cmds(name, src, z, out, t, level):
    parent, base = os.path.dirname(src), os.path.basename(src)
    if name == "Bandizip":
        return [BZ, "c", "-fmt:zip", f"-l:{level}", "-y", z, src], [BZ, "x", "-y", f"-o:{out}", z], None
    if name == "yzip CLI":
        return CLI + ["c", z, src, "-l", str(level)], CLI + ["x", z, "-o", out], None
    if name == "yzip 우클릭(exe)":
        lst = os.path.join(t, "sel.txt")
        with open(lst, "w", encoding="utf-8") as f:
            f.write(src + "\n")
        # real right-click path: shell extension starts the exe with a list file; extract via '폴더에 압축 풀기'
        os.makedirs(out, exist_ok=True)
        zz = os.path.join(out, os.path.basename(z))
        return [EXE, "--archive-list", lst, "--out", z], [EXE, "--extract-folder", zz], zz
    if name == "Windows tar":
        return (["tar", "-a", "-c", "-f", z, "-C", parent, base],
                ["tar", "-x", "-f", z, "-C", out], None)
    raise ValueError(name)


def extract_root(out, base):
    """Where the extracted copy of the source folder ended up."""
    for cand in (os.path.join(out, base), os.path.join(out, "a", base), os.path.join(out, "a")):
        if os.path.isdir(cand):
            return cand
    return out


def cross_check(z, base, src_manifest, t, maker):
    """Extract with a different tool than the one that made z, compare every file hash."""
    chk = os.path.join(t, "chk")
    shutil.rmtree(chk, ignore_errors=True)
    os.makedirs(chk)
    if maker == "Bandizip":
        subprocess.run(CLI + ["x", z, "-o", chk], capture_output=True)
        by = "yzip"
    else:
        subprocess.run([BZ, "x", "-y", f"-o:{chk}", z], capture_output=True)
        by = "Bandizip"
    got = manifest(extract_root(chk, base))
    ok = got == src_manifest
    shutil.rmtree(chk, ignore_errors=True)
    return ok, by


def bench_dataset(label, src, tools, level=5):
    base = os.path.basename(src)
    size = tree_size(src)
    nfiles = sum(len(fs) for _, _, fs in os.walk(src))
    print(f"\n=== {label}: {size / MB:,.0f} MB, {nfiles:,} files (runs={RUNS})", flush=True)
    src_manifest = manifest(src)
    rows = []
    for name in tools:
        comp, ext, zsize, ok_cross, ok_ext, by = [], [], 0, None, None, ""
        for i in range(RUNS if size < 2 * 1024 * MB else 1):
            with tempfile.TemporaryDirectory(prefix="yzp_") as t:
                z = os.path.join(t, "a.zip")
                out = os.path.join(t, "out")
                c_cmd, x_cmd, z_moved = tool_cmds(name, src, z, out, t, level)
                env = env_exe(t) if "exe" in name else None
                rc = run(c_cmd, env)
                if rc["rc"] != 0 or not os.path.exists(z):
                    print(f"  {name}: compress FAILED rc={rc['rc']}")
                    break
                comp.append(rc)
                zsize = os.path.getsize(z)
                if i == 0:
                    ok_cross, by = cross_check(z, base, src_manifest, t, name)
                if z_moved:
                    shutil.move(z, z_moved)
                    z = z_moved
                os.makedirs(out, exist_ok=True)
                rx = run(x_cmd, env)
                ext.append(rx)
                if i == 0:
                    root = extract_root(out, base)
                    ok_ext = rx["rc"] == 0 and manifest(root) == src_manifest
        if not comp:
            rows.append({"tool": name, "failed": True})
            continue
        med = lambda xs, k: statistics.median(x[k] for x in xs)  # noqa: E731
        row = {"tool": name, "c_wall": med(comp, "wall"), "c_cpu": med(comp, "cpu"), "c_mem": max(x["peak_mb"] for x in comp),
               "x_wall": med(ext, "wall") if ext else None, "x_cpu": med(ext, "cpu") if ext else None,
               "x_mem": max(x["peak_mb"] for x in ext) if ext else None,
               "zip_mb": zsize / MB, "ratio": zsize / size * 100, "cross_ok": ok_cross, "cross_by": by,
               "extract_ok": ok_ext, "src_mb": size / MB}
        rows.append(row)
        print(f"  {name:16s} 압축 {row['c_wall']:6.2f}s ({size / MB / row['c_wall']:6.0f} MB/s, CPU {row['c_cpu']:5.1f}s, "
              f"메모리 {row['c_mem']:5.0f}MB)  풀기 {row['x_wall']:6.2f}s ({size / MB / row['x_wall']:6.0f} MB/s, "
              f"메모리 {row['x_mem']:5.0f}MB)  크기 {row['ratio']:5.1f}%  "
              f"교차검증({by}) {'OK' if ok_cross else 'FAIL'}  풀기검증 {'OK' if ok_ext else 'FAIL'}", flush=True)
    return {"label": label, "mb": size / MB, "files": nfiles, "rows": rows}


def bench_password(src):
    print("\n=== 암호 압축 (같은 텍스트 데이터)", flush=True)
    rows = []
    size = tree_size(src)
    for name, mk, ex in (("Bandizip", lambda z: [BZ, "c", "-fmt:zip", "-l:5", "-p:Pw1234", "-y", z, src],
                          lambda z, o: [BZ, "x", "-y", "-p:Pw1234", f"-o:{o}", z]),
                         ("yzip CLI", lambda z: CLI + ["c", z, src, "-l", "5", "-p", "Pw1234"],
                          lambda z, o: CLI + ["x", z, "-o", o, "-p", "Pw1234"])):
        with tempfile.TemporaryDirectory(prefix="yzp_") as t:
            z = os.path.join(t, "a.zip")
            c = run(mk(z))
            with zipfile.ZipFile(z) as zf:
                info = next(i for i in zf.infolist() if not i.is_dir())
                method = "AES-256" if info.compress_type == 99 else ("ZipCrypto" if info.flag_bits & 1 else "없음")
            x = run(ex(z, os.path.join(t, "o")))
            rows.append({"tool": name, "c_wall": c["wall"], "x_wall": x["wall"], "method": method,
                         "ok": c["rc"] == 0 and x["rc"] == 0})
            print(f"  {name:16s} 압축 {c['wall']:6.2f}s ({size / MB / c['wall']:5.0f} MB/s)  풀기 {x['wall']:6.2f}s  "
                  f"방식 {method}  rc {c['rc']}/{x['rc']}", flush=True)
    return rows


def bench_formats(src):
    """Other formats on the same text data: yzip CLI (-fmt) against bz.exe where Bandizip writes that format."""
    print("\n=== 다른 형식 (같은 텍스트 데이터)", flush=True)
    size = tree_size(src)
    rows = []
    for fid, ext, bzfmt in (("7z", ".7z", "7z"), ("tgz", ".tar.gz", "tgz"), ("txz", ".tar.xz", None),
                            ("tzst", ".tar.zst", None), ("tbz", ".tar.bz2", None)):
        for tool in (("Bandizip",) if bzfmt else ()) + ("yzip CLI",):
            with tempfile.TemporaryDirectory(prefix="yzp_") as t:
                z = os.path.join(t, "a" + (".tgz" if tool == "Bandizip" and bzfmt == "tgz" else ext))
                mk = [BZ, "c", f"-fmt:{bzfmt}", "-l:5", "-y", z, src] if tool == "Bandizip" else \
                    CLI + ["c", z, src, "-l", "5", f"-fmt:{fid}"]
                c = run(mk)
                ex = [BZ, "x", "-y", f"-o:{os.path.join(t, 'o')}", z] if tool == "Bandizip" else \
                    CLI + ["x", z, "-o", os.path.join(t, "o")]
                x = run(ex)
                zs = os.path.getsize(z) if os.path.exists(z) else 0
                rows.append({"fmt": fid, "tool": tool, "c_wall": c["wall"], "x_wall": x["wall"], "ratio": zs / size * 100})
                print(f"  {fid:5s} {tool:9s} 압축 {c['wall']:6.2f}s ({size / MB / c['wall']:5.0f} MB/s)  "
                      f"풀기 {x['wall']:5.2f}s  크기 {zs / size * 100:5.1f}%  rc {c['rc']}/{x['rc']}", flush=True)
    return rows


def bench_latency(t):
    """Right-click to finished archive for one small file: what a user feels on every use."""
    p = os.path.join(t, "memo.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("hello " * 1000)
    res = {}
    for name, cmd in (("yzip 우클릭(exe)", lambda z, l: [EXE, "--archive-list", l, "--out", z]),
                      ("Bandizip bz.exe", lambda z, l: [BZ, "c", "-fmt:zip", "-y", z, p])):
        walls = []
        for i in range(5):
            z = os.path.join(t, f"m{i}_{len(res)}.zip")
            lst = os.path.join(t, f"l{i}.txt")
            with open(lst, "w", encoding="utf-8") as f:
                f.write(p + "\n")
            walls.append(run(cmd(z, lst), env_exe(t))["wall"])
        res[name] = statistics.median(walls)
        print(f"  {name:16s} 작은 파일 1개 압축 완료까지 {res[name]:.2f}s (중앙값, 5회)", flush=True)
    return res


def main():
    out_json = sys.argv[sys.argv.index("--json") + 1] if "--json" in sys.argv else None
    report = {"datasets": []}
    tools = ["Bandizip", "yzip CLI", "yzip 우클릭(exe)", "Windows tar"]
    with tempfile.TemporaryDirectory(prefix="yzdata_") as data:
        t0 = time.time()
        make_text(os.path.join(data, "logs"), 120 if QUICK else 360)
        if "--formats-only" in sys.argv:
            report["formats"] = bench_formats(os.path.join(data, "logs"))
            return
        make_photos(os.path.join(data, "photos"), 40 if QUICK else 120, 3)
        make_small(os.path.join(data, "source"))
        print(f"data generated in {time.time() - t0:.0f}s", flush=True)
        report["datasets"].append(bench_dataset("텍스트/로그 (잘 압축됨)", os.path.join(data, "logs"), tools))
        report["datasets"].append(bench_dataset("사진/동영상류 (압축 안 됨)", os.path.join(data, "photos"), tools))
        report["datasets"].append(bench_dataset("작은 파일 다수 (소스 코드)", os.path.join(data, "source"), tools))
        if not QUICK:
            make_huge(os.path.join(data, "huge"), 4.5)
            report["datasets"].append(bench_dataset("4.5GB 단일 파일 (zip64)", os.path.join(data, "huge"),
                                                    ["Bandizip", "yzip CLI"]))
            shutil.rmtree(os.path.join(data, "huge"), ignore_errors=True)
        report["password"] = bench_password(os.path.join(data, "logs"))
        report["formats"] = bench_formats(os.path.join(data, "logs"))
        print("\n=== 우클릭 체감 지연", flush=True)
        with tempfile.TemporaryDirectory(prefix="yzp_") as t:
            report["latency"] = bench_latency(t)
    if out_json:
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
