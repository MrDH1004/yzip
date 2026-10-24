"""Run the REAL entry points (no test_gui_flows patches) repeatedly and require exit code 0.

- extract verbs: yzip_gui.py --extract-here / --extract-folder
- delete flow: real Qt event loop around Main.delete_selected(), then the real finish()
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ziplib  # noqa: E402

ENV = dict(os.environ, QT_QPA_PLATFORM="offscreen", YZIP_NO_OPEN="1", YZIP_GATHER_SCALE="0.05")

DELETE_DRIVER = r"""
import os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, {here!r})
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication, QMessageBox
import yzip_gui
app = QApplication([])
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
w = yzip_gui.Main()
w.show()
w.open_archive({zip!r})
w.tree.topLevelItem(0).setSelected(True)
w.delete_selected()
assert w.tree.topLevelItemCount() == {expect}, w.tree.topLevelItemCount()
QTimer.singleShot(200, app.quit)
rc = app.exec()
yzip_gui.finish(rc)
"""

bad = 0


def report(name, codes):
    global bad
    ok = all(c == 0 for c in codes)
    bad += not ok
    print(("OK   " if ok else "FAIL ") + f"{name}: exit codes {codes}")


for verb in ("--extract-here", "--extract-folder"):
    codes = []
    for _ in range(5):
        with tempfile.TemporaryDirectory(prefix="yzr_") as t:
            src = os.path.join(t, "in")
            os.makedirs(os.path.join(src, "sub"))
            for n in ("a.txt", os.path.join("sub", "b.txt")):
                with open(os.path.join(src, n), "w") as f:
                    f.write(n * 200)
            z = os.path.join(t, "x.zip")
            ziplib.create_zip([os.path.join(src, "a.txt"), os.path.join(src, "sub")], z)
            r = subprocess.run([sys.executable, "-W", "ignore", os.path.join(HERE, "yzip_gui.py"), verb, z],
                               env=ENV, capture_output=True, timeout=120)
            extracted = any(f == "a.txt" for _, _, fs in os.walk(t) for f in fs if _ != src)
            codes.append(r.returncode if extracted else f"{r.returncode}(no output files)")
    report(f"real {verb} x5", codes)

codes = []
for _ in range(5):
    with tempfile.TemporaryDirectory(prefix="yzd_") as t:
        a = os.path.join(t, "a.txt")
        b = os.path.join(t, "b.txt")
        for p in (a, b):
            with open(p, "w") as f:
                f.write(p * 100)
        z = os.path.join(t, "d.zip")
        ziplib.create_zip([a, b], z)
        r = subprocess.run([sys.executable, "-W", "ignore", "-c", DELETE_DRIVER.format(here=HERE, zip=z, expect=1)],
                           env=ENV, capture_output=True, timeout=120)
        codes.append(r.returncode if r.returncode else 0)
        if r.returncode:
            print(r.stderr.decode("utf-8", "replace")[-400:])
report("real delete flow + real event loop x5", codes)
sys.exit(1 if bad else 0)
