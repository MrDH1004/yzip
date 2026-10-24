import os
import subprocess
import sys
import tempfile

os.environ["QT_QPA_PLATFORM"] = "offscreen"
HERE = os.path.dirname(os.path.abspath(__file__))
CLI = [sys.executable, os.path.join(HERE, "yzip_cli.py")]


def cli(*a):
    r = subprocess.run(CLI + list(a), capture_output=True, text=True, encoding="utf-8")
    return r.returncode, r.stdout + r.stderr


with tempfile.TemporaryDirectory() as t:
    src = os.path.join(t, "문서")
    os.makedirs(src)
    for n in ("a.txt", "b.txt"):
        with open(os.path.join(src, n), "w", encoding="utf-8") as f:
            f.write(n * 500)
    z = os.path.join(t, "t.zip")
    assert cli("c", z, src, "-p", "pw1")[0] == 0
    assert cli("t", z, "-p", "pw1")[0] == 0
    assert cli("t", z, "-p", "bad")[0] == 3, "wrong password must exit 3"
    rc, out = cli("l", z, "-p", "pw1")
    assert rc == 0 and "문서/a.txt" in out, out
    z2 = os.path.join(t, "plain.zip")
    assert cli("c", z2, src)[0] == 0
    assert cli("rn", z2, "문서/a.txt", "문서/c.txt")[0] == 0
    assert cli("d", z2, "문서/b.txt")[0] == 0
    rc, out = cli("l", z2)
    assert "문서/c.txt" in out and "b.txt" not in out, out
    assert cli("a", z2, os.path.join(src, "b.txt"))[0] == 0
    rc, out = cli("l", z2)
    assert "b.txt" in out and "c.txt" in out, out
    dest = os.path.join(t, "out")
    assert cli("x", z2, "-o", dest)[0] == 0 and os.path.isfile(os.path.join(dest, "문서", "c.txt"))
    flat = os.path.join(t, "flat")
    assert cli("e", z2, "-o", flat)[0] == 0 and os.path.isfile(os.path.join(flat, "c.txt"))
    sp = os.path.join(t, "s.zip")
    with open(os.path.join(src, "big.bin"), "wb") as f:
        f.write(os.urandom(500_000))
    assert cli("c", sp, src, "-l", "0", "-v", "200KB")[0] == 0
    assert os.path.exists(sp + ".001") and os.path.exists(sp + ".002")
    assert cli("t", sp + ".001")[0] == 0

    # GUI smoke
    sys.path.insert(0, HERE)
    from PySide6.QtWidgets import QApplication
    import yzip_gui
    app = QApplication([])
    w = yzip_gui.Main()
    w.open_archive(z2)
    assert len(w.entries) >= 3 and w.tree.topLevelItemCount() >= 1, len(w.entries)  # list shows one folder level
    w.open_archive(sp + ".001")
    assert len(w.entries) >= 3
print("SMOKE OK")
