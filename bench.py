"""Compare yzip against Bandizip's bz.exe on identical data: compress time, extract time, archive size.
Usage: python bench.py [yzip-cli-args-suffix]   (both tools use deflate, level 5, no password)"""
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BZ = r"C:\Program Files\Bandizip\bz.exe"
YZIP = [sys.executable, "-W", "ignore", os.path.join(HERE, "yzip_cli.py")]
LEVEL = os.environ.get("BENCH_LEVEL", "5")


def make_big(d):
    os.makedirs(d)
    rnd = random.Random(1)
    words = [("w%x" % rnd.randrange(1 << 20)) for _ in range(5000)]
    for i in range(2):  # text-like, compresses well
        with open(os.path.join(d, f"text{i}.log"), "w") as f:
            for _ in range(60 * 1024 * 1024 // 60):
                f.write(" ".join(rnd.choices(words, k=9)) + "\n")
    for i in range(2):  # half incompressible, half repetitive
        with open(os.path.join(d, f"mixed{i}.bin"), "wb") as f:
            for _ in range(30):
                f.write(os.urandom(1024 * 1024))
                f.write((b"ABCDEFGH" * 131072))


def make_small(d):
    """Many small files: copy of the rea repo sources (no .git)."""
    src = os.environ.get("YZIP_SMALL_SRC") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rea")
    shutil.copytree(src, d, ignore=shutil.ignore_patterns(".git", "node_modules"))


def timed(cmd):
    t = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True)
    return time.perf_counter() - t, r.returncode


def size_of(path):
    return os.path.getsize(path) / 1e6


def du(path):
    return sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(path) for f in fs) / 1e6


def bench(name, src):
    print(f"\n== {name}: {du(src):.0f} MB source, {sum(len(fs) for _, _, fs in os.walk(src))} files, level {LEVEL}")
    rows = []
    for tool, mk, ex in (
        ("bandizip", lambda z: [BZ, "c", "-fmt:zip", f"-l:{LEVEL}", "-y", z, src],
         lambda z, o: [BZ, "x", "-y", f"-o:{o}", z]),
        ("yzip", lambda z: YZIP + ["c", z, src, "-l", LEVEL],
         lambda z, o: YZIP + ["x", z, "-o", o]),
    ):
        with tempfile.TemporaryDirectory(prefix="yzb_") as t:
            z = os.path.join(t, "a.zip")
            tc, rc = timed(mk(z))
            if rc != 0 or not os.path.exists(z):
                rows.append((tool, "FAILED compress rc=%s" % rc))
                continue
            out = os.path.join(t, "out")
            te, rx = timed(ex(z, out))
            rows.append((tool, tc, te, size_of(z), du(out), rx))
    for r in rows:
        if len(r) == 2:
            print(f"  {r[0]:9s} {r[1]}")
        else:
            print(f"  {r[0]:9s} compress {r[1]:6.2f}s   extract {r[2]:6.2f}s   zip {r[3]:7.1f} MB   extracted {r[4]:.0f} MB (rc {r[5]})")
    return rows


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="yzdata_") as data:
        big = os.path.join(data, "big")
        make_big(big)
        small = os.path.join(data, "small")
        make_small(small)
        bench("few large files", big)
        bench("many small files", small)
