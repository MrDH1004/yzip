"""yzip_main: '<name>.zip으로 압축하기' finishes small jobs without loading Qt, and hands longer or failed jobs
to a progress window / message box. Each case runs in a fresh interpreter so 'Qt was loaded' is observable."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DRIVER = r'''
import json, os, sys, time
sys.argv = ["yzip"] + json.loads(os.environ["ARGS"])
shown = []
if os.environ.get("SLOW"):  # a job that outlives the 0.3 s grace period
    import formats
    real = formats.create
    def slow(*a, **k):
        for i in range(8):
            time.sleep(0.1)
            k["progress"](i, 8, "x")
        return real(*a, **k)
    formats.create = slow
if os.environ.get("WATCH_QT"):
    import yzip_gui
    from PySide6.QtWidgets import QMessageBox
    QMessageBox.critical = staticmethod(lambda *a, **k: shown.append(["critical", a[2]]))
    QMessageBox.warning = staticmethod(lambda *a, **k: shown.append(["warning", a[2]]))
    orig = yzip_gui.ProgressWindow.show
    yzip_gui.ProgressWindow.show = lambda self: (shown.append(["window", self.title]), orig(self))[1]
    yzip_gui.finish = lambda code: None
    done = yzip_gui.ProgressWindow.show_done
    def show_done(self):  # '창을 닫지 않기': record it, then close as the user would
        shown.append(["done", self.title])
        done(self)
        yzip_gui.QTimer.singleShot(100, self.accept)
    yzip_gui.ProgressWindow.show_done = show_done
import yzip_main
yzip_main.main()
print("RESULT" + json.dumps({"loaded_qt": "PySide6" in sys.modules, "shown": shown}))
'''


class QuickEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.src = os.path.join(self.root, "memo.txt")
        with open(self.src, "w", encoding="utf-8") as f:
            f.write("hello " * 1000)

    def tearDown(self):
        self.tmp.cleanup()

    def run_entry(self, out, **env):
        lst = os.path.join(self.root, "list.txt")
        with open(lst, "w", encoding="utf-8") as f:
            f.write(self.src + "\n")
        e = dict(os.environ, ARGS=json.dumps(["--archive-list", lst, "--out", out]), QT_QPA_PLATFORM="offscreen",
                 YZIP_NO_OPEN="1", YZIP_SETTINGS=os.path.join(self.root, "settings.json"), **env)
        r = subprocess.run([sys.executable, "-W", "ignore", "-c", DRIVER], cwd=HERE, env=e, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=60)
        line = next((ln for ln in r.stdout.splitlines() if ln.startswith("RESULT")), None)
        self.assertIsNotNone(line, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(lst), "the list file is removed")
        return json.loads(line[6:])

    def test_small_job_never_loads_qt(self):
        out = os.path.join(self.root, "memo.zip")
        res = self.run_entry(out)
        self.assertFalse(res["loaded_qt"])
        with zipfile.ZipFile(out) as z:
            self.assertEqual(z.read("memo.txt"), b"hello " * 1000)

    def test_long_job_gets_the_progress_window(self):
        out = os.path.join(self.root, "slow.zip")
        res = self.run_entry(out, SLOW="1", WATCH_QT="1")
        self.assertIn(["window", "압축하기 slow.zip"], res["shown"])
        self.assertTrue(zipfile.is_zipfile(out))

    def test_failure_is_reported(self):
        out = os.path.join(self.root, "no such dir", "x.zip")
        res = self.run_entry(out, WATCH_QT="1")
        self.assertEqual([s[0] for s in res["shown"]], ["critical"])
        self.assertFalse(os.path.exists(out))

    def test_keep_open_setting_shows_the_window(self):
        with open(os.path.join(self.root, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"keep_open": True}, f)
        out = os.path.join(self.root, "keep.zip")  # a quick job, but the window has to stay open afterwards
        res = self.run_entry(out, WATCH_QT="1")
        self.assertIn(["done", "압축하기 keep.zip"], res["shown"])
        self.assertTrue(zipfile.is_zipfile(out))


if __name__ == "__main__":
    unittest.main()
