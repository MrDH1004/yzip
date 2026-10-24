"""In-process extract timing (no interpreter start-up), to separate start-up cost from real decompression speed."""
import os
import subprocess
import sys
import tempfile
import time

import bench
import ziplib

with tempfile.TemporaryDirectory(prefix="yzdata_") as data:
    big = os.path.join(data, "big")
    bench.make_big(big)
    z = os.path.join(data, "a.zip")
    subprocess.run([bench.BZ, "c", "-fmt:zip", "-l:5", "-y", z, big], capture_output=True)
    t = time.perf_counter()
    subprocess.run([sys.executable, "-c", "pass"])
    print(f"python start-up alone: {time.perf_counter() - t:.2f}s")
    t = time.perf_counter()
    subprocess.run([sys.executable, "-W", "ignore", "-c", "import ziplib"], cwd=os.path.dirname(os.path.abspath(__file__)))
    print(f"python + import ziplib: {time.perf_counter() - t:.2f}s")
    for i in range(3):
        out = os.path.join(data, f"o{i}")
        t = time.perf_counter()
        with ziplib.Archive(z) as a:
            a.extract(out)
        print(f"in-process extract run {i}: {time.perf_counter() - t:.2f}s")
    out = os.path.join(data, "bz")
    t = time.perf_counter()
    subprocess.run([bench.BZ, "x", "-y", f"-o:{out}", z], capture_output=True)
    print(f"bandizip extract: {time.perf_counter() - t:.2f}s")
