"""Explorer launches one process per selected file for a "%1" verb. Verify that separate processes
are merged into ONE job: exactly one collector gets all paths, the rest exit with None."""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CHILD = r"""
import os, sys, json
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, {here!r})
from PySide6.QtWidgets import QApplication
import yzip_gui
app = QApplication([])
r = yzip_gui.gather_paths("aggtest", [sys.argv[1]])
print("RESULT " + json.dumps(r))
"""


def run(n, stagger):
    env = dict(os.environ, YZIP_GATHER_SCALE="0.6")
    procs = []
    for i in range(n):
        procs.append(subprocess.Popen([sys.executable, "-W", "ignore", "-c", CHILD.format(here=HERE), f"C:\\files\\f{i}.txt"],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True))
        time.sleep(stagger)
    outs = []
    for p in procs:
        o, e = p.communicate(timeout=60)
        line = [l for l in o.splitlines() if l.startswith("RESULT ")]
        outs.append(json.loads(line[0][7:]) if line else ("NO OUTPUT", e[-300:]))
    return outs


ok = True
for n, stagger in ((3, 0.05), (5, 0.15), (1, 0)):
    outs = run(n, stagger)
    collectors = [o for o in outs if o is not None]
    expected = sorted(f"C:\\files\\f{i}.txt" for i in range(n))
    good = len(collectors) == 1 and sorted(collectors[0]) == expected
    print(("PASS" if good else "FAIL"), f"{n} processes -> {len(collectors)} collector(s)", "" if good else outs)
    ok &= good
sys.exit(0 if ok else 1)
