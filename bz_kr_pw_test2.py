import os
import subprocess
import tempfile

import pyzipper

BZ = r"C:\Program Files\Bandizip\bz.exe"
PW = "p@ss한글"
with tempfile.TemporaryDirectory() as t:
    for enc in ("utf-8", "cp949", "utf-16-le"):
        z = os.path.join(t, enc + ".zip")
        with pyzipper.AESZipFile(z, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as zf:
            zf.setpassword(PW.encode(enc))
            zf.writestr("f.txt", b"hello" * 100)
        r = subprocess.run([BZ, "t", "-y", f"-p:{PW}", z], capture_output=True, timeout=30)
        print(enc, "->", "All OK" in r.stdout.decode("cp949", "replace"))
