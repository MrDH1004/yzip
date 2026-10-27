"""Headless (offscreen) end-to-end tests of the GUI flows the Explorer right-click verbs trigger.

Dialogs are driven by a QTimer that clicks OK on the active modal dialog; message boxes and
os.startfile are recorded instead of shown/executed. Run: python test_gui_flows.py
"""
import hashlib
import importlib
import os
import shutil
import sys
import tempfile
import time
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["YZIP_NO_OPEN"] = "1"
os.environ["YZIP_SETTINGS"] = os.path.join(tempfile.mkdtemp(prefix="yzt_set_"), "settings.json")  # never the user's
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
MOD = os.environ.get("GUI_MOD", "yzip_gui")
LIB = os.environ.get("LIB_MOD", "ziplib")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import (QApplication, QDialogButtonBox, QInputDialog, QLabel, QMessageBox, QPushButton)  # noqa: E402

gui = importlib.import_module(MOD)
ziplib = importlib.import_module(LIB)
app = QApplication.instance() or QApplication([])

boxes = []        # (kind, text) of every message box the app tried to show
opened = []       # os.startfile targets
answers = {"password": []}


def _rec(kind, ret):
    def f(parent, title, text, *a, **k):
        boxes.append((kind, str(text)))
        return ret
    return staticmethod(f)


QMessageBox.critical = _rec("critical", QMessageBox.Ok)
QMessageBox.warning = _rec("warning", QMessageBox.Ok)
QMessageBox.information = _rec("information", QMessageBox.Ok)
QMessageBox.question = _rec("question", QMessageBox.Yes)


def fake_get_text(parent, title, label, *a, **k):
    boxes.append(("input", label))
    pw = answers["password"].pop(0) if answers["password"] else None
    return (pw or "", pw is not None)


QInputDialog.getText = staticmethod(fake_get_text)
os.startfile = lambda p: opened.append(p)

dlg_cfg = {"password": "", "split": "분할 안 함", "fmt": "zip", "seen": None}


flags = {"cancel_progress": False, "on_progress": None, "done_seen": []}


def autoclick():
    for top in QApplication.topLevelWidgets():
        if type(top).__name__ != "ProgressWindow" or not top.isVisible():
            continue
        if top._done:  # kept open after success: note what it shows, then press 닫기
            flags["done_seen"].append((top.stop_btn.text(), top.label.text(), top.windowTitle()))
            top.stop_btn.click()
        elif flags["cancel_progress"]:
            (top.stop_btn.click if flags["cancel_progress"] == "button" else top.cancel)()
        elif flags["on_progress"]:
            flags["on_progress"](top)
            flags["on_progress"] = None
    w = QApplication.activeModalWidget()
    if w is None:
        return
    n = type(w).__name__
    if n == "CompressDialog":
        w.fmt.setCurrentIndex(w.fmt.findData(dlg_cfg["fmt"]))
        if dlg_cfg["seen"] is not None:  # what the window offers for this selection / format
            dlg_cfg["seen"].update(path=w.path.text(), pw=w.pw.isEnabled(), split=w.split.isEnabled(),
                                   single_enabled=[w.fmt.model().item(i).isEnabled() for i in range(w.fmt.count())
                                                   if gui.formats.BY_ID[w.fmt.itemData(i)].single])
        w.pw.setText(dlg_cfg["password"])
        w.pw2.setText(dlg_cfg["password"])
        w.split.setCurrentText(dlg_cfg["split"])
        w.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok).click()
    elif n == "ExtractDialog":
        w.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok).click()


timer = QTimer()
timer.timeout.connect(autoclick)
timer.start(40)

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def bad_boxes():
    return [b for b in boxes if b[0] in ("critical", "warning")]


_real_exec = QApplication.exec
QApplication.exec = staticmethod(lambda: 0)  # main() must not block the harness in its event loop
gui.open_path = lambda p: opened.append(p)


def run_main(argv):
    sys.argv = ["yzip"] + argv
    return gui.main()


def make_files(root):
    d = os.path.join(root, "in")
    os.makedirs(os.path.join(d, "sub 폴더"))
    files = {}
    for rel, data in (("a.txt", b"hello" * 3000), ("b.bin", os.urandom(150_000)),
                      (os.path.join("sub 폴더", "한글.txt"), "안녕하세요".encode() * 500)):
        p = os.path.join(d, rel)
        with open(p, "wb") as f:
            f.write(data)
        files[rel] = p
    return d, files


def fresh():
    boxes.clear()
    opened.clear()
    answers["password"].clear()
    dlg_cfg.update(password="", split="분할 안 함")
    t = tempfile.mkdtemp(prefix="yzt_")
    return t


def zip_listing(path):
    with ziplib.Archive(path) as a:
        return sorted(e.name for e in a.entries() if not e.is_dir)


# ---------- run_job ----------
def test_run_job():
    fails = 0
    for _ in range(150):
        ok, r = gui.run_job(None, "t", lambda p, c: 42)
        if not (ok and r == 42):
            fails += 1
    check("run_job: 150 instant jobs all deliver result (race)", fails == 0, f"{fails}/150 lost the result")

    ok, r = gui.run_job(None, "t", lambda p, c: (_ for _ in ()).throw(ziplib.Cancelled()))
    check("run_job: Cancelled -> ('cancel')", (not ok) and r and r[0] == "cancel", repr((ok, r)))
    ok, r = gui.run_job(None, "t", lambda p, c: (_ for _ in ()).throw(ziplib.BadPassword("x")))
    check("run_job: BadPassword -> ('password')", (not ok) and r and r[0] == "password", repr((ok, r)))
    ok, r = gui.run_job(None, "t", lambda p, c: 1 / 0)
    check("run_job: exception -> ('error')", (not ok) and r and r[0] == "error", repr((ok, r)))

    def slow(prog, canc):
        import time
        for i in range(200):
            if canc():
                raise ziplib.Cancelled()
            prog(i, 200, "x")
            time.sleep(0.01)
        return "done"
    boxes.clear()
    ok, r = gui.run_job(None, "t", slow)
    check("run_job: slow job completes when not cancelled", ok and r == "done", repr((ok, r)))

    # cancel the progress window the way a user would
    for how in ("button", "cancel()"):
        flags["cancel_progress"] = how
        t0 = time.time()
        ok, r = gui.run_job(None, "t", slow)
        flags["cancel_progress"] = False
        check(f"run_job: user cancel via {how} mid-job -> ('cancel'), stops early, no error box",
              (not ok) and r and r[0] == "cancel" and time.time() - t0 < 1.8 and not bad_boxes(),
              repr((ok, r, round(time.time() - t0, 2), boxes)))

    # bottom-left tools: folder button, keep-open toggle, menu -> low priority
    revealed = []
    orig_reveal = gui.reveal_in_explorer
    gui.reveal_in_explorer = lambda p: revealed.append(p)
    seen = {}

    def use_tools(win):
        seen["tips"] = [b.toolTip() for b in (win.folder_btn, win.keep_btn, win.menu_btn)]
        seen["menu"] = [a.text() for a in win.menu_btn.menu().actions()]
        win.folder_btn.click()
        win.low_act.trigger()
        seen["prio"] = gui_priority()
        win.keep_btn.click()
        seen["keep_menu_synced"] = win.keep_act.isChecked()
    flags["on_progress"] = use_tools
    flags["done_seen"].clear()
    ok, r = gui.run_job(None, "압축하기 x.zip", slow, reveal=r"C:\some\x.zip", done_text="압축 작업을 완료했습니다.")
    gui.reveal_in_explorer = orig_reveal
    check("progress tools: folder / keep-open / menu buttons with Korean tooltips",
          seen.get("tips") == ["폴더 열기", "작업 완료 후 창을 닫지 않기", "메뉴"], seen)
    check("progress tools: menu has '낮은 우선순위'", "낮은 우선순위" in seen.get("menu", []), seen)
    check("folder button opens the output's folder", revealed == [r"C:\some\x.zip"], revealed)
    check("low priority: process runs at IDLE priority", seen.get("prio") == 0x40, hex(seen.get("prio") or 0))
    check("keep-open: toolbar toggle and menu item stay in sync", seen.get("keep_menu_synced") is True, seen)
    done = flags["done_seen"][:1]
    check("keep-open: after success the window stays with '닫기' and a done message, 100% in title",
          ok and done and done[0][0] == "닫기" and "압축 작업을 완료했습니다." in done[0][1] and done[0][2].startswith("100%"),
          repr((ok, done)))
    s = gui.load_settings()
    check("both choices are remembered for next time", s.get("keep_open") is True and s.get("low_priority") is True, s)
    flags["done_seen"].clear()
    ok, r = gui.run_job(None, "t", lambda p, c: 7)
    check("keep-open also applies to a fast job (window shown done, then closed by 닫기)",
          ok and r == 7 and flags["done_seen"] and flags["done_seen"][0][0] == "닫기", repr((ok, r, flags["done_seen"])))
    gui.save_setting("keep_open", False)
    gui.save_setting("low_priority", False)
    gui.set_low_priority(False)
    check("low priority off: back to NORMAL priority", gui_priority() == 0x20, hex(gui_priority()))


def gui_priority():
    import ctypes
    k = ctypes.WinDLL("kernel32")
    k.GetCurrentProcess.restype = ctypes.c_void_p
    k.GetPriorityClass.argtypes = [ctypes.c_void_p]
    return k.GetPriorityClass(k.GetCurrentProcess())


# ---------- right-click verbs through real main() ----------
def test_archive_verb():
    t = fresh()
    d, files = make_files(t)
    out = os.path.join(d, "a.zip")
    shown, dialogs = [], []
    orig_show, orig_exec = gui.Main.show, gui.CompressDialog.exec
    gui.Main.show = lambda self: (shown.append(1), orig_show(self))
    gui.CompressDialog.exec = lambda self: (dialogs.append(1), orig_exec(self))[1]
    run_main(["--archive", files["a.txt"]])
    gui.Main.show, gui.CompressDialog.exec = orig_show, orig_exec
    check("--archive: main window never shown", not shown, shown)
    check("--archive: compresses at once, no options dialog (Bandizip style)", not dialogs, dialogs)
    check("--archive single file: no error boxes", not bad_boxes(), repr(bad_boxes()))
    check("--archive single file: a.zip created", os.path.exists(out))
    if os.path.exists(out):
        with ziplib.Archive(out) as a:
            check("--archive single file: test() clean", a.test() == [])
            a.extract(os.path.join(t, "x"))
        check("--archive single file: content identical", sha(os.path.join(t, "x", "a.txt")) == sha(files["a.txt"]))

    t = fresh()
    d, files = make_files(t)
    run_main(["--archive", d])
    z = d + ".zip"
    check("--archive folder: no error boxes", not bad_boxes(), repr(bad_boxes()))
    check("--archive folder: in.zip created next to folder", os.path.exists(z), z)
    if os.path.exists(z):
        names = zip_listing(z)
        check("--archive folder: all 3 files incl. Korean subfolder", len(names) == 3 and any("한글.txt" in n for n in names), repr(names))

    run_main(["--archive", d])
    check("--archive again: existing name kept, new one is 'in (2).zip'",
          os.path.exists(os.path.join(t, "in (2).zip")), repr(os.listdir(t)))

    # options (password, split) live in the main window's "new archive" dialog
    t = fresh()
    d, files = make_files(t)
    dlg_cfg["password"] = "pw한글!"
    win = gui.Main()
    win.new_archive([files["b.bin"]])
    z = os.path.join(d, "b.zip")
    check("new archive with password: created, no error boxes", os.path.exists(z) and not bad_boxes(), repr(bad_boxes()))
    if os.path.exists(z):
        with ziplib.Archive(z, "pw한글!") as a:
            check("new archive with password: entries encrypted, opens with password",
                  a.needs_password() and a.test() == [])

    t = fresh()
    d, files = make_files(t)
    dlg_cfg["split"] = "100KB"
    win = gui.Main()
    win.new_archive([files["b.bin"]])
    z = os.path.join(d, "b.zip")
    check("new archive split 100KB: .001 and .002 exist", os.path.exists(z + ".001") and os.path.exists(z + ".002"),
          repr(os.listdir(d)))


def make_zip(t, with_pw=None):
    d, files = make_files(t)
    z = os.path.join(t, "pkg.zip")
    ziplib.create_zip([files["a.txt"], files["b.bin"], os.path.join(d, "sub 폴더")], z, password=with_pw)
    return d, files, z


def test_extract_verbs():
    t = fresh()
    d, files, z = make_zip(t)
    run_main(["--extract-here", z])
    check("--extract-here: no error boxes", not bad_boxes(), repr(bad_boxes()))
    got = os.path.join(t, "a.txt")
    smart = os.path.join(t, "pkg", "a.txt")
    check("--extract-here: files extracted (smart folder 'pkg' since >1 top-level)",
          os.path.exists(smart) or os.path.exists(got), repr(os.listdir(t)))
    check("--extract-here: content identical", os.path.exists(smart) and sha(smart) == sha(files["a.txt"]))
    check("--extract-here: result folder opened", len(opened) == 1, repr(opened))

    t = fresh()
    d, files, z = make_zip(t)
    run_main(["--extract-folder", z])
    p = os.path.join(t, "pkg", "b.bin")
    check("--extract-folder: extracted into <name>/ folder", os.path.exists(p) and sha(p) == sha(files["b.bin"]), repr(os.listdir(t)))
    check("--extract-folder: no error boxes", not bad_boxes(), repr(bad_boxes()))

    t = fresh()
    d, files, z = make_zip(t, with_pw="s3cret")
    answers["password"] = ["s3cret"]
    run_main(["--extract-folder", z])
    p = os.path.join(t, "pkg", "b.bin")
    check("--extract-folder encrypted: asks password then extracts", os.path.exists(p) and sha(p) == sha(files["b.bin"]),
          repr((boxes, os.listdir(t))))

    t = fresh()
    d, files, z = make_zip(t, with_pw="s3cret")
    answers["password"] = ["wrong", "s3cret"]
    run_main(["--extract-folder", z])
    p = os.path.join(t, "pkg", "b.bin")
    check("--extract-folder encrypted: wrong pw -> retry -> success", os.path.exists(p), repr(boxes))

    t = fresh()
    d, files, z = make_zip(t)
    os.makedirs(os.path.join(t, "pkg"))
    with open(os.path.join(t, "pkg", "a.txt"), "w") as f:
        f.write("OLD")
    run_main(["--extract-folder", z])
    check("--extract-folder, folder exists: original untouched", os.listdir(os.path.join(t, "pkg")) == ["a.txt"]
          and open(os.path.join(t, "pkg", "a.txt")).read() == "OLD", repr(os.listdir(os.path.join(t, "pkg"))))
    check("--extract-folder, folder exists: new folder 'pkg (2)' (Bandizip style)",
          os.path.exists(os.path.join(t, "pkg (2)", "a.txt")), repr(os.listdir(t)))


def test_main_window_ops():
    t = fresh()
    d, files, z = make_zip(t)
    w = gui.Main()
    w.open_archive(z)
    check("window: lists entries", w.tree.topLevelItemCount() >= 3, w.tree.topLevelItemCount())

    w.test()
    check("window: test() reports OK, no errors", any("이상 없음" in b[1] for b in boxes) and not bad_boxes(), repr(boxes))

    boxes.clear()
    w.tree.selectAll()
    w.extract()
    check("window: extract dialog -> files extracted", os.path.exists(os.path.join(t, "pkg", "b.bin")) or os.path.exists(os.path.join(t, "b.bin")), repr(os.listdir(t)))
    check("window: extract no error boxes", not bad_boxes(), repr(bad_boxes()))

    boxes.clear()
    extra =os.path.join(t, "extra.txt")
    with open(extra, "w") as f:
        f.write("extra")
    w.add_paths([extra])
    check("window: add_paths keeps old + adds new", "extra.txt" in zip_listing(z) and "a.txt" in zip_listing(z), repr(zip_listing(z)))
    check("window: add_paths no error boxes", not bad_boxes(), repr(bad_boxes()))

    boxes.clear()
    for i in range(w.tree.topLevelItemCount()):
        it = w.tree.topLevelItem(i)
        if it.data(0, gui.Qt.UserRole) == "extra.txt":
            it.setSelected(True)
    del it  # the tree deletes its items on reload; don't keep a wrapper to a destroyed C++ item
    w.delete_selected()
    check("window: delete_selected removes entry", "extra.txt" not in zip_listing(z) and "a.txt" in zip_listing(z), repr(zip_listing(z)))

    boxes.clear()
    t2 = fresh()
    d, files = make_files(t2)
    sp = os.path.join(t2, "sp.zip")
    ziplib.create_zip([files["b.bin"]], sp, split_size=50_000)
    w2 = gui.Main()
    w2.open_archive(sp + ".001")
    check("window: split archive opens", w2.tree.topLevelItemCount() == 1, w2.tree.topLevelItemCount())
    w2.tree.selectAll()
    w2.extract()
    check("window: split archive extracts identically", os.path.exists(os.path.join(t2, "b.bin")) and sha(os.path.join(t2, "b.bin")) == sha(files["b.bin"]), repr(os.listdir(t2)))

    boxes.clear()
    t3 = fresh()
    d, files, z3 = make_zip(t3, with_pw="pw1")
    w3 = gui.Main()
    w3.open_archive(z3)
    answers["password"] = ["bad", "pw1"]
    w3.tree.selectAll()
    w3.extract()
    check("window: encrypted extract w/ wrong then right password", os.path.exists(os.path.join(t3, "pkg", "b.bin")) or os.path.exists(os.path.join(t3, "b.bin")), repr((boxes, os.listdir(t3))))
    for win in (w, w2, w3):
        win.close()
        win.deleteLater()
    app.processEvents()


def test_drop_and_corrupt():
    t = fresh()
    d, files, z = make_zip(t)
    with open(z, "rb") as f:
        data = bytearray(f.read())
    data[len(data) // 3] ^= 0xFF
    bad = os.path.join(t, "bad.zip")
    with open(bad, "wb") as f:
        f.write(data)
    w = gui.Main()
    w.open_archive(bad)
    boxes.clear()
    w.test()
    check("window: corrupt archive test() warns, no crash", any(b[0] == "warning" for b in boxes) or any(b[0] == "critical" for b in boxes), repr(boxes))
    notzip = os.path.join(t, "nz.zip")
    with open(notzip, "w") as f:
        f.write("not a zip")
    boxes.clear()
    w.open_archive(notzip)
    check("window: non-zip file shows error box, no crash", any(b[0] == "critical" for b in boxes), repr(boxes))


def test_formats_gui():
    t = fresh()
    d, files = make_files(t)
    w = gui.Main()
    # 7z with a password through the compress window; names are encrypted, so opening asks for the password
    dlg_cfg.update(fmt="7z", password="pw7", seen={})
    w.new_archive([d])
    z7 = os.path.join(t, "in.7z")
    check("window: format 7z -> in.7z, password allowed, split off",
          os.path.exists(z7) and dlg_cfg["seen"]["path"].endswith("in.7z") and dlg_cfg["seen"]["pw"]
          and not dlg_cfg["seen"]["split"], (os.listdir(t), dlg_cfg["seen"]))
    check("window: a folder cannot pick single-file formats (gz/bz2/xz/zst)",
          dlg_cfg["seen"]["single_enabled"] and not any(dlg_cfg["seen"]["single_enabled"]), dlg_cfg["seen"])
    boxes.clear()
    answers["password"][:] = ["pw7"]
    w2 = gui.Main()
    w2.open_archive(z7)
    check("open 7z with hidden names: asks the password, lists entries",
          any(b[0] == "input" for b in boxes) and len(w2.entries) >= 4, (boxes, len(w2.entries)))
    boxes.clear()
    w2.tree.topLevelItem(0).setSelected(True)
    w2.delete_selected()
    check("delete on a 7z: explained as ZIP-only, archive untouched",
          any("ZIP 형식에서만" in b[1] for b in boxes) and os.path.exists(z7), repr(boxes))

    # tar.gz: path and options follow the format; extract-here through main() opens it by content
    t = fresh()
    d, files = make_files(t)
    dlg_cfg.update(fmt="tgz", password="ignored", seen={})
    w.new_archive([d])
    tgz = os.path.join(t, "in.tar.gz")
    check("window: format TAR.GZ -> in.tar.gz, password disabled (none used)",
          os.path.exists(tgz) and not dlg_cfg["seen"]["pw"] and not gui.formats.open_any(tgz).needs_password(),
          (os.listdir(t), dlg_cfg["seen"]))
    shutil.rmtree(d)
    boxes.clear()
    run_main(["--extract-here", tgz])
    check("right-click extract on .tar.gz restores the files", os.path.exists(os.path.join(t, "in", "a.txt"))
          and sha(os.path.join(t, "in", "a.txt")) == sha_bytes(b"hello" * 3000) and not bad_boxes(),
          (os.listdir(t), boxes))

    # single file -> .xz, then the folder name for "폴더에 압축 풀기" drops the whole format extension
    t = fresh()
    d, files = make_files(t)
    dlg_cfg.update(fmt="xz", password="", seen={})
    w.new_archive([files["b.bin"]])
    xz = os.path.join(d, "b.bin.xz")
    check("window: a single file can pick XZ -> b.bin.xz (keeps its own extension inside)",
          os.path.exists(xz) and any(dlg_cfg["seen"]["single_enabled"]), (os.listdir(d), dlg_cfg["seen"]))
    run_main(["--extract-folder", xz])
    got = os.path.join(d, "b.bin (2)", "b.bin")  # the file b.bin sits where the folder would go
    check("'폴더에 압축 풀기' on b.bin.xz next to b.bin -> folder 'b.bin (2)', identical",
          os.path.exists(got) and sha(got) == sha(files["b.bin"]), os.listdir(d))
    dlg_cfg.update(fmt="zip", password="", seen=None)


def test_simple_window():
    t = fresh()
    d, files, z = make_zip(t)
    w = gui.Main()
    w.show()
    app.processEvents()
    check("simple: starts on the drop page, toolbar hidden, archive actions off",
          w.pages.currentIndex() == 0 and not w.toolbar.isVisible() and not w.a_extract.isEnabled(),
          (w.pages.currentIndex(), w.toolbar.isVisible()))
    check("simple: home page exposes 환경설정", w.settings_btn.text().strip() == "환경설정", w.settings_btn.text())
    check("simple: main window title and home footer show the version",
          w.windowTitle() == f"yzip {gui.__version__}" and f"yzip {gui.__version__}" in w.findChild(QLabel, "homeFoot").text(),
          (w.windowTitle(), w.findChild(QLabel, "homeFoot").text()))
    w.open_archive(z)
    app.processEvents()
    shown = [a.text() for a in w.toolbar.actions() if a.isVisible() and a.text()]
    check("simple: open archive -> list + toolbar with only 풀기/열기/새로 압축 (+ 더 보기)",
          w.pages.currentIndex() == 1 and w.toolbar.isVisible() and shown[:3] == ["풀기", "열기", "새로 압축"]
          and w.more_btn.isVisible(), shown)
    more = [a.text() for a in w.more_btn.menu().actions() if a.text()]
    check("simple: add/delete/test/details/Explorer menu live under 더 보기",
          {"파일 추가…", "선택 항목 삭제", "압축 파일 검사", "자세한 정보 보기", "환경설정…",
           "탐색기 우클릭 메뉴 등록"} <= set(more), more)
    hidden = [w.tree.isColumnHidden(c) for c in range(len(w.COLS))]
    check("simple: only 이름/크기/수정한 날짜 shown by default", hidden == [False] * 3 + [True] * 4, hidden)
    w.a_details.trigger()
    check("simple: 자세한 정보 보기 shows the detail columns and is remembered",
          not any(w.tree.isColumnHidden(c) for c in w.DETAIL_COLS) and gui.load_settings().get("show_details"),
          gui.load_settings())
    w.a_details.trigger()
    rows = [(w.tree.topLevelItem(i).text(0), bool(w.tree.topLevelItem(i).data(0, w.IS_DIR)))
            for i in range(w.tree.topLevelItemCount())]
    check("browse: root shows direct children only, folders first",
          rows[0] == ("sub 폴더", True) and {r[0] for r in rows} == {"sub 폴더", "a.txt", "b.bin"}, rows)
    w.tree.topLevelItem(0).setSelected(True)
    sel = w.selected_names()
    check("browse: a selected folder stands for everything inside it",
          any(n.endswith("한글.txt") for n in sel), sel)
    w._activate(w.tree.topLevelItem(0))
    inside = [w.tree.topLevelItem(i).text(0) for i in range(w.tree.topLevelItemCount())]
    check("browse: opening a folder lists its files with short names; crumbs and up enabled",
          inside == ["한글.txt"] and w.cwd == "sub 폴더/" and w.up_btn.isEnabled(), (inside, w.cwd))
    w.tree.topLevelItem(0).setSelected(True)
    boxes.clear()
    dest = os.path.join(t, "fromsub")
    w._do_extract(dest, w.selected_names(), "overwrite")
    check("browse: extracting inside a folder keeps the archive path",
          os.path.exists(os.path.join(dest, "sub 폴더", "한글.txt")), os.listdir(dest) if os.path.exists(dest) else None)
    w.go_up()
    check("browse: up returns to the root", w.cwd == "" and w.tree.topLevelItemCount() == 3 and not w.up_btn.isEnabled(), w.cwd)
    w.open_archive(z)  # reload (after add/delete) keeps the place; a new archive starts at its root
    check("browse: header shows format, file count and size; no lock for a plain archive",
          w.badge.text() == "ZIP" and "파일 3개" in w.arch_meta.text() and not w.lock.isVisibleTo(w), w.arch_meta.text())
    check("browse: archive window title keeps the version",
          w.windowTitle().endswith(f" - yzip {gui.__version__}"), w.windowTitle())
    keys = {a.shortcut().toString() for a in w.actions()}
    check("simple: shortcuts kept although the actions are hidden", {"Ctrl+E", "Ctrl+O", "Ctrl+N", "Del", "Ctrl+T"} <= keys, keys)
    dlg = gui.ExtractDialog(w, t, 0)
    check("simple: extract dialog folds overwrite/smart options, hides 'selected only' with no selection",
          not dlg.mode_over.isVisibleTo(dlg) and not dlg.sel_only.isVisibleTo(dlg) and dlg.mode_over.isChecked()
          and dlg.smart.isChecked(), "")
    dlg.more.setChecked(True)
    check("simple: 옵션 더 보기 reveals them", dlg.mode_over.isVisibleTo(dlg) and dlg.smart.isVisibleTo(dlg), "")
    dlg.deleteLater()
    w.close()
    w.deleteLater()
    app.processEvents()


def test_settings():
    import assoc
    calls = []
    with mock.patch.object(assoc, "enabled", return_value=False), \
            mock.patch.object(assoc, "register", side_effect=lambda: calls.append("register")), \
            mock.patch.object(assoc, "unregister", side_effect=lambda: calls.append("unregister")):
        dlg = gui.SettingsDialog(None)
        check("settings: file association starts unchecked when yzip is not registered",
              not dlg.assoc_check.isChecked(), dlg.assoc_check.isChecked())
        dlg.assoc_check.setChecked(True)
        dlg.apply()
        check("settings: enabling file association registers all supported types",
              calls == ["register"], calls)
        dlg.assoc_check.setChecked(False)
        dlg.apply()
        check("settings: disabling file association unregisters all supported types",
              calls == ["register", "unregister"], calls)
        dlg.deleteLater()
    app.processEvents()


def test_update_prompt():
    import updater

    w = gui.Main()
    w.show()
    app.processEvents()
    rel = updater.Release("1.0.12", "installer.exe", "installer.exe.sha256", "installer.exe", "", "")
    with mock.patch.object(gui.QMessageBox, "question", return_value=gui.QMessageBox.No) as ask, \
            mock.patch.object(w, "do_update") as do_update:
        w._update_result(rel, None, False)
        check("update: startup asks before downloading", ask.called and not do_update.called,
              (ask.call_args, do_update.call_count))
    with mock.patch.object(gui.QMessageBox, "question", return_value=gui.QMessageBox.Yes) as ask, \
            mock.patch.object(w, "do_update") as do_update:
        w._update_result(rel, None, False)
        check("update: yes starts the installer flow", ask.called and do_update.call_count == 1,
              (ask.call_args, do_update.call_count))
    with mock.patch.object(gui.QMessageBox, "question") as ask, \
            mock.patch.object(w, "do_update") as do_update:
        w._update_result(rel, None, True)
        check("update: manual check keeps the non-modal update bar",
              not ask.called and not do_update.called and w.upd_bar.isVisible(),
              (ask.call_count, do_update.call_count, w.upd_bar.isVisible()))
    w.close()
    w.deleteLater()
    app.processEvents()


def sha_bytes(b):
    return hashlib.sha256(b).hexdigest()


TESTS = {f.__name__: f for f in (test_run_job, test_archive_verb, test_extract_verbs, test_main_window_ops,
                                 test_drop_and_corrupt, test_formats_gui, test_simple_window, test_settings,
                                 test_update_prompt)}

if __name__ == "__main__":
    # One test group per process (see run_headless_tests.py): several shown+destroyed Main windows in a
    # single process crash Qt at teardown, which is a property of this harness, not of a real session.
    selected = [TESTS[a] for a in sys.argv[1:]] or list(TESTS.values())
    for fn in selected:
        try:
            fn()
        except Exception as e:  # a crash is a failure, keep going
            import traceback
            traceback.print_exc()
            check(fn.__name__ + " (no exception)", False, repr(e))
    timer.stop()
    QTimer.singleShot(150, app.quit)
    _real_exec()  # let queued deletions run in a real event loop, like a normal session end
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    gui.finish(1 if failed else 0)  # same exit path as the real app (see yzip_gui.finish)
