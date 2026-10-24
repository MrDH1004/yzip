import os
import subprocess
import tempfile

import ziplib

BZ = r"C:\Program Files\Bandizip\bz.exe"
with tempfile.TemporaryDirectory() as t:
    f = os.path.join(t, "f.txt")
    with open(f, "w") as fh:
        fh.write("hello" * 100)
    z = os.path.join(t, "k.zip")
    ziplib.create_zip([f], z, password="p@ss한글")
    for pw in ("p@ss한글", "p@ss"):
        r = subprocess.run([BZ, "t", "-y", f"-p:{pw}", z], capture_output=True, timeout=30)
        out = r.stdout.decode("cp949", "replace")
        print(pw, "->", "All OK" in out, "| invalid pw" if "Invalid Password" in out else "")
