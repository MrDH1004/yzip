"""yzip GUI (PySide6). Usage: yzip_gui.py [archive.zip] | --archive FILES... | --extract-here ARCHIVE | --extract-folder ARCHIVE"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from datetime import datetime

import json

from PySide6.QtCore import QEvent, QEventLoop, QObject, QSize, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QAction, QFont, QFontDatabase, QKeySequence
from PySide6.QtWidgets import (QApplication, QBoxLayout, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFileIconProvider, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu,
                               QMessageBox, QLayout, QProgressBar, QPushButton, QRadioButton, QScrollArea, QSizePolicy, QSlider,
                               QSpinBox, QStackedWidget, QToolBar, QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

import formats
import ziplib
import ziprepair
import appcore
import appearance
from preferences import PreferencesDialog as SettingsDialog
from appcore import load_settings, log_exception, log_path, outcome, read_list, save_setting, set_low_priority  # noqa: F401
from version import __version__

APP = "yzip"
from ui_theme import MAIN_QSS, PROGRESS_QSS, DIALOG_QSS



def fmt_size(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:,.0f} {u}" if u == "B" else f"{n:,.1f} {u}"
        n /= 1024


def open_path(path: str):
    """Reveal a result in Explorer/associated app. Tests set YZIP_NO_OPEN to avoid launching anything."""
    if not os.environ.get("YZIP_NO_OPEN"):
        os.startfile(path)


class Worker(QThread):
    progress = Signal(object, object, str)  # int would wrap above 2 GiB

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self._cancel = False
        self.ok = False
        self.result = None  # job return value, or (kind, message) on failure

    def cancel(self):
        self._cancel = True

    def run(self):
        # The outcome lives on the instance (not in queued signals) so it is always there once the thread ends.
        self.ok, self.result = outcome(self.fn, lambda d, t, n: self.progress.emit(d, t, n), lambda: self._cancel)


def reveal_in_explorer(path: str):
    """Open the folder holding path with the file selected (just the folder if it is not there yet)."""
    if os.environ.get("YZIP_NO_OPEN"):
        return
    import subprocess
    if os.path.isfile(path):
        subprocess.Popen(f'explorer /select,"{path}"')
    else:
        os.startfile(path if os.path.isdir(path) else os.path.dirname(path))


def fmt_time(secs: float) -> str:
    s = int(secs)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def _icon_font() -> QFont:
    fams = set(QFontDatabase.families())
    f = QFont(next((n for n in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if n in fams), "Segoe UI Symbol"))
    f.setPixelSize(15)
    return f


GLYPH_FOLDER, GLYPH_PIN, GLYPH_MENU = "", "", ""  # Segoe icon font: FolderOpen, Pin, GlobalNav


class ProgressWindow(QDialog):
    """Progress window in the spirit of Bandizip's: bar, file name, stats, and at the bottom left
    folder / keep-open / menu (low priority). When done it can stay open showing '닫기'."""
    canceled = Signal()

    def __init__(self, parent, title, reveal=None, done_text="작업을 완료했습니다."):
        super().__init__(parent)
        self.title, self.reveal, self.done_text = title, reveal, done_text
        self.setObjectName("progressWindow")
        self._canceled = self._done = False
        self.start, self.last, self._painted = time.time(), (0, 0), 0.0
        self.setWindowTitle(f"{title} - {APP}")
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setMinimumWidth(460)
        self.resize(500, 180)
        self.setStyleSheet(PROGRESS_QSS)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 16, 12)
        lay.setSpacing(10)
        self.label = QLabel()
        self.label.setTextFormat(Qt.RichText)
        lay.addWidget(self.label)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        lay.addWidget(self.bar)
        hint = QLabel("중지를 누르면 현재 작업을 중단합니다.")
        hint.setObjectName("progressHint")
        lay.addWidget(hint)
        self.progress_hint = hint
        row = QHBoxLayout()
        row.setSpacing(2)
        settings = load_settings()
        self.folder_btn = self._tool(GLYPH_FOLDER, "폴더 열기")
        self.folder_btn.setEnabled(bool(reveal))
        self.folder_btn.clicked.connect(lambda: reveal and reveal_in_explorer(reveal))
        self.keep_btn = self._tool(GLYPH_PIN, "작업 완료 후 창을 닫지 않기")
        self.keep_btn.setCheckable(True)
        self.keep_btn.setChecked(bool(settings.get("keep_open")))
        self.menu_btn = self._tool(GLYPH_MENU, "메뉴")
        menu = QMenu(self)
        self.keep_act = menu.addAction("작업 완료 후 창을 닫지 않기")
        self.keep_act.setCheckable(True)
        self.keep_act.setChecked(self.keep_btn.isChecked())
        self.low_act = menu.addAction("낮은 우선순위")
        self.low_act.setCheckable(True)
        self.low_act.setChecked(bool(settings.get("low_priority")))
        self.keep_btn.toggled.connect(self._set_keep)
        self.keep_act.toggled.connect(self._set_keep)
        self.low_act.toggled.connect(self._set_low)
        self.menu_btn.setMenu(menu)
        self.menu_btn.setPopupMode(QToolButton.InstantPopup)
        for b in (self.folder_btn, self.keep_btn, self.menu_btn):
            row.addWidget(b)
        row.addStretch()
        self.stop_btn = QPushButton("중지")
        self.stop_btn.clicked.connect(self._on_button)
        row.addWidget(self.stop_btn)
        lay.addLayout(row)
        self._show_stats(0, 0, "")

    def _tool(self, glyph, tip):
        b = QToolButton()
        b.setIcon(glyph_icon(glyph))
        b.setIconSize(QSize(16, 16))
        b.setToolTip(tip)
        b.setAccessibleName(tip)
        b.setAutoRaise(True)
        b.setFixedSize(32, 30)
        return b

    def _set_keep(self, on):
        for w in (self.keep_btn, self.keep_act):
            if w.isChecked() != on:
                w.setChecked(on)
        save_setting("keep_open", on)

    def _set_low(self, on):
        set_low_priority(on)
        save_setting("low_priority", on)

    # ----- progress -----
    @Slot(object, object, str)
    def on_progress(self, d, t, n):  # a slot on a GUI-thread QObject, so worker signals are queued here
        self.last = (d, t)
        now = time.time()
        if now - self._painted >= 0.05 or (t and d >= t):  # thousands of small files: repaint at most 20x/s
            self._painted = now
            self._show_stats(d, t, n)

    def _show_stats(self, d, t, n):
        from html import escape
        pct = int(d * 100 / t) if t else 0
        self.bar.setValue(pct)
        self.setWindowTitle(f"{pct}% {self.title} - {APP}")
        el = max(time.time() - self.start, 0.001)
        name = os.path.basename(n.rstrip("/")) or self.title
        name = name if len(name) <= 48 else name[:22] + "…" + name[-22:]
        # total from the average rate so far; too noisy to show in the first second or before 1%
        total = fmt_time(el * t / d) if d and t and el >= 1 and d * 100 >= t else "--:--"
        sep = "&nbsp;&nbsp;·&nbsp;&nbsp;"
        self.label.setText(
            f"<div style='font-size:10.5pt; font-weight:600; color:#202D35'>{escape(name)}</div>"
            f"<div style='font-size:9pt; color:#64737D; margin-top:3px'>{pct}%{sep}{fmt_size(int(d / el))}/s{sep}"
            f"{fmt_time(el)} / {total}</div>")

    def show_done(self):
        from html import escape
        self._done = True
        d, t = self.last
        el = time.time() - self.start
        self.bar.setValue(100)
        self.setWindowTitle(f"100% {self.title} - {APP}")
        self.label.setText(
            f"<div style='font-size:10.5pt; font-weight:600; color:#202D35'>{escape(self.done_text)}</div>"
            f"<div style='font-size:9pt; color:#64737D; margin-top:3px'>{fmt_size(t)}&nbsp;&nbsp;·&nbsp;&nbsp;"
            f"걸린 시간 {fmt_time(el)}</div>")
        self.progress_hint.setText("작업을 마쳤습니다. 폴더를 열거나 창을 닫으세요." if self.reveal
                                   else "작업을 완료했습니다.")
        self.stop_btn.setText("닫기")
        self.stop_btn.setEnabled(True)
        self.stop_btn.setDefault(True)
        self.stop_btn.setFocus()

    # ----- stop / close -----
    def wasCanceled(self):
        return self._canceled

    def cancel(self):
        if self._done:
            self.accept()
            return
        if not self._canceled:
            self._canceled = True
            self.stop_btn.setText("중지하는 중…")
            self.stop_btn.setEnabled(False)
            self.canceled.emit()

    def _on_button(self):
        self.cancel()

    def reject(self):  # Esc
        self.cancel()

    def closeEvent(self, e):
        if self._done:
            e.accept()
        else:
            self.cancel()  # the job has to stop first; run_job closes the window afterwards
            e.ignore()


MIN_SHOWN = 0.6  # seconds a finished progress window stays visible


def run_job(parent, title, fn, reveal=None, done_text="작업을 완료했습니다.", job=None):
    """Run fn(progress, cancelled) in a thread behind a progress window. Returns (ok, result_or_(kind, msg)).
    reveal: path the folder button opens. The window shows at once, also for a tiny job, and a finished one stays
    up for MIN_SHOWN so it can be seen; with '창을 닫지 않기' it stays open after success until the user closes
    it. job: an appcore.Job already running (started before Qt was loaded); the window then follows its state
    instead of starting a Worker."""
    dlg = ProgressWindow(parent, title, reveal, done_text)
    dlg.setWindowModality(Qt.WindowModal)
    dlg.show()
    shown = time.time()
    if job is None:
        set_low_priority(dlg.low_act.isChecked())
        w = Worker(fn)
        w.progress.connect(dlg.on_progress)
        dlg.canceled.connect(w.cancel)
        w.start()
        while not w.wait(30):
            QApplication.processEvents()
            if dlg.wasCanceled():
                w.cancel()
        QApplication.processEvents()  # flush progress signals queued by the last chunk
        ok, result = w.ok, w.result
    else:
        dlg.start = job.started
        dlg.canceled.connect(lambda: setattr(job, "cancelled", True))
        while not job.wait(0.03):
            QApplication.processEvents()
            dlg.on_progress(*job.state)
        dlg.on_progress(*job.state)
        ok, result = job.ok, job.result
    if ok and dlg.keep_btn.isChecked() and not os.environ.get("YZIP_AUTOACCEPT"):
        dlg.show_done()
        dlg.show()
        loop = QEventLoop()
        dlg.finished.connect(loop.quit)
        if dlg.isVisible():
            loop.exec()
    elif ok:  # a job done in a blink still shows its full bar for a moment instead of a flicker
        dlg.bar.setValue(100)
        dlg.setWindowTitle(f"100% {dlg.title} - {APP}")
        while time.time() - shown < MIN_SHOWN and dlg.isVisible():
            QApplication.processEvents()
            time.sleep(0.01)
    dlg._done = True
    dlg.close()
    dlg.deleteLater()
    return ok, result


class CompressDialog(QDialog):
    def __init__(self, parent, default_path: str, paths=None):
        super().__init__(parent)
        self.setWindowTitle("압축 파일 만들기")
        settings = load_settings()
        self.setMinimumWidth(520)
        self.setStyleSheet(DIALOG_QSS)
        lay = QFormLayout(self)
        lay.setContentsMargins(24, 24, 24, 20)
        lay.setHorizontalSpacing(18)
        lay.setVerticalSpacing(12)
        lay.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        title = QLabel("압축 파일 만들기")
        title.setObjectName("dialogTitle")
        lay.addRow(title)
        self.fmt = QComboBox()
        one_file = bool(paths) and len(paths) == 1 and os.path.isfile(paths[0])
        self.one_file = os.path.basename(paths[0]) if one_file else None
        for f in formats.FORMATS:
            self.fmt.addItem(f.label, f.id)
            if f.single and not one_file:  # gz/bz2/xz/zst hold exactly one file
                self.fmt.model().item(self.fmt.count() - 1).setEnabled(False)
        lay.addRow("형식", self.fmt)
        if settings.get("compress_dir"):
            default_path = unique_path(os.path.join(settings["compress_dir"], os.path.basename(default_path)))
        self.path = QLineEdit(default_path)
        b = QPushButton("찾아보기")
        b.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.path)
        row.addWidget(b)
        lay.addRow("파일 이름", row)
        self.level = QSlider(Qt.Horizontal)
        self.level.setRange(0, 9)
        self.level.setValue(int(settings.get("compress_level", 6)))
        self.level_lbl = QLabel("저장(압축 안 함)" if self.level.value() == 0 else str(self.level.value()))
        self.level.valueChanged.connect(lambda v: self.level_lbl.setText("저장(압축 안 함)" if v == 0 else str(v)))
        row = QHBoxLayout()
        row.addWidget(self.level)
        row.addWidget(self.level_lbl)
        lay.addRow("압축 수준", row)
        self.split = QComboBox()
        self.split.setEditable(True)
        self.split.addItems(["분할 안 함", "100MB", "650MB", "700MB", "1440KB", "4GB"])
        self.split.setCurrentText(str(settings.get("compress_split", "분할 안 함")))
        lay.addRow("분할 압축", self.split)
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.Password)
        self.pw2 = QLineEdit()
        self.pw2.setEchoMode(QLineEdit.Password)
        self.show_pw = QCheckBox("암호 표시")
        self.show_pw.toggled.connect(lambda c: [e.setEchoMode(QLineEdit.Normal if c else QLineEdit.Password)
                                                for e in (self.pw, self.pw2)])
        lay.addRow("암호", self.pw)
        lay.addRow("암호 확인", self.pw2)
        lay.addRow("", self.show_pw)
        self.pw_note = QLabel("암호를 입력하면 AES-256으로 암호화됩니다.")
        self.pw_note.setObjectName("dialogHint")
        self.pw_note.setWordWrap(True)
        lay.addRow("", self.pw_note)
        self.excl = QLineEdit(".tmp, Thumbs.db, .DS_Store")
        lay.addRow("제외할 이름 끝", self.excl)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("압축하기")
        bb.button(QDialogButtonBox.Cancel).setText("취소")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)
        self.fmt.currentIndexChanged.connect(self._format_changed)
        preferred = formats.BY_ID.get(settings.get("compress_format", "zip"), formats.BY_ID["zip"])
        if preferred.single and not one_file:
            preferred = formats.BY_ID["zip"]
        self.fmt.setCurrentIndex(self.fmt.findData(preferred.id))
        self._format_changed()

    def current_format(self) -> "formats.Fmt":
        return formats.BY_ID[self.fmt.currentData()]

    def _format_changed(self):
        f = self.current_format()
        cur = self.path.text()
        name = formats.stem(cur)
        if f.single:  # a stream keeps the file's own name inside: b.bin -> b.bin.xz (as gzip does)
            name = self.one_file or name
        elif self.one_file and name == self.one_file:  # back from b.bin.xz to an archive: b.zip
            name = os.path.splitext(name)[0]
        base = os.path.join(os.path.dirname(cur), name)
        out, i = base + f.ext, 2
        while os.path.exists(out) and out != cur:  # switching format must not land on an existing file
            out, i = f"{base} ({i}){f.ext}", i + 1
        self.path.setText(out)
        for w in (self.pw, self.pw2, self.show_pw):
            w.setEnabled(f.password)
        if not f.password:
            self.pw.clear()
            self.pw2.clear()
        self.pw_note.setText({"zip": "암호를 입력하면 AES-256으로 암호화됩니다.",
                              "7z": "암호를 입력하면 AES-256으로 암호화되고 파일 이름도 숨겨집니다."}.get(
            f.id, f"{f.label.split(' ')[0]} 형식은 암호를 지원하지 않습니다."))
        self.split.setEnabled(f.split)
        if not f.split:
            self.split.setCurrentText("분할 안 함")
        self.level.setEnabled(f.id != "tar")

    def _browse(self):
        f = self.current_format()
        p, _ = QFileDialog.getSaveFileName(self, "저장", self.path.text(), f"{f.label} (*{f.ext})")
        if p:
            self.path.setText(p)

    def _ok(self):
        if self.pw.text() != self.pw2.text():
            QMessageBox.warning(self, APP, "암호가 서로 다릅니다.")
            return
        if self.split_bytes() < 0:
            QMessageBox.warning(self, APP, "분할 크기를 이해할 수 없습니다. 예: 100MB, 1440KB (최소 64KB)")
            return
        out = self.options()["out_path"]
        import appcore
        if not appcore.can_write(os.path.dirname(os.path.abspath(out))):
            QMessageBox.warning(self, APP, f"{os.path.dirname(os.path.abspath(out))}\n\n이 위치에는 관리자 권한이 "
                                           "있어야 파일을 만들 수 있습니다. 다른 위치를 선택해 주세요.")
            return
        if os.path.exists(out) and QMessageBox.question(
                self, APP, f"{os.path.basename(out)} 파일이 이미 있습니다. 덮어쓸까요?") != QMessageBox.Yes:
            return
        self.accept()

    def split_bytes(self) -> int:
        t = self.split.currentText().strip()
        if t in ("", "분할 안 함"):
            return 0
        from yzip_cli import parse_size
        try:
            n = parse_size(t)
        except Exception:
            return -1
        return n if n >= 64 << 10 else -1  # "1" would mean one-byte volumes

    def options(self) -> dict:
        f = self.current_format()
        path = self.path.text().strip()
        if not path.lower().endswith(f.ext):
            path += f.ext
        return dict(fmt=f.id, out_path=path, level=max(1, self.level.value()), store_only=self.level.value() == 0,
                    password=(self.pw.text() or None) if f.password else None,
                    split_size=self.split_bytes() if f.split else 0,
                    exclude=[e.strip() for e in self.excl.text().split(",") if e.strip()])


class ExtractDialog(QDialog):
    def __init__(self, parent, default_dir: str, n_selected: int):
        super().__init__(parent)
        self.setWindowTitle("압축 풀기")
        settings = load_settings()
        self.setMinimumWidth(520)
        self.setStyleSheet(DIALOG_QSS)
        lay = QFormLayout(self)
        lay.setContentsMargins(24, 24, 24, 20)
        lay.setHorizontalSpacing(18)
        lay.setVerticalSpacing(12)
        lay.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        title = QLabel("압축 풀기")
        title.setObjectName("dialogTitle")
        lay.addRow(title)
        self.dest = QLineEdit(settings.get("extract_dir") or default_dir)
        b = QPushButton("찾아보기")
        b.clicked.connect(lambda: self.dest.setText(QFileDialog.getExistingDirectory(self, "폴더", self.dest.text()) or self.dest.text()))
        row = QHBoxLayout()
        row.addWidget(self.dest)
        row.addWidget(b)
        lay.addRow("풀 위치", row)
        self.sel_only = QCheckBox(f"선택한 {n_selected}개 항목만")
        self.sel_only.setChecked(n_selected > 0)
        self.sel_only.setEnabled(n_selected > 0)
        if n_selected:  # nothing selected: everything is extracted, no need to say so
            lay.addRow("", self.sel_only)
        else:
            self.sel_only.setParent(self)
            self.sel_only.hide()
        # what happens to existing files and the smart folder: sensible defaults, folded away
        self.more = QToolButton()
        self.more.setText("옵션 더 보기")
        self.more.setArrowType(Qt.DownArrow)
        self.more.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.more.setCheckable(True)
        self.more.setObjectName("extractOptions")
        lay.addRow("", self.more)
        opts = QWidget()
        box = QVBoxLayout(opts)
        box.setContentsMargins(0, 0, 0, 0)
        self.mode_ask = QRadioButton("이미 있으면 확인")
        self.mode_over = QRadioButton("모두 덮어쓰기")
        self.mode_skip = QRadioButton("이미 있으면 건너뛰기")
        self.mode_ren = QRadioButton("이름 자동 변경: name (2).txt")
        modes = {"ask": self.mode_ask, "overwrite": self.mode_over, "skip": self.mode_skip, "rename": self.mode_ren}
        modes.get(settings.get("extract_mode", "ask"), self.mode_ask).setChecked(True)
        self.smart = QCheckBox("스마트 풀기: 최상위 항목이 여럿이면 압축 파일 이름 폴더에 풀기")
        self.smart.setChecked(bool(settings.get("extract_smart", True)))
        self.smart.setText("스마트 풀기: 항목이 여럿이면 하위 폴더 생성")
        for w in (self.mode_ask, self.mode_over, self.mode_skip, self.mode_ren, self.smart):
            box.addWidget(w)
        opts.setVisible(False)
        lay.addRow("", opts)

        def fold(on):
            opts.setVisible(on)
            self.more.setArrowType(Qt.UpArrow if on else Qt.DownArrow)
            self.adjustSize()
        self.more.toggled.connect(fold)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("풀기")
        bb.button(QDialogButtonBox.Cancel).setText("취소")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)

    def _ok(self):
        import appcore
        if not appcore.can_write(self.dest.text()):
            QMessageBox.warning(self, APP, f"{self.dest.text()}\n\n이 위치에는 관리자 권한이 있어야 파일을 만들 수 "
                                           "있습니다. 다른 위치를 선택해 주세요.")
            return
        self.accept()


def unique_path(p: str) -> str:
    if not os.path.exists(p):
        return p
    base, ext = os.path.splitext(p)
    i = 2
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


def writable_target(parent, target: str, elevate_args, pick, folder: str | None = None):
    """target (a file to create, or a folder to extract into) lies where only administrators may write (C:\\,
    Program Files). Like Bandizip, yzip then runs the same job as administrator right away (Windows asks in its
    UAC prompt); only if that is refused, offer another place. elevate_args() -> argv for the elevated yzip;
    pick(parent) -> new target or "". Returns the target to use, or None when there is nothing more to do here."""
    import appcore
    folder = folder or os.path.dirname(os.path.abspath(target))
    if os.environ.get("YZIP_AUTOACCEPT"):  # unattended tests: never raise a UAC prompt
        return target if appcore.can_write(folder) else None
    if not appcore.can_write(folder) and appcore.run_elevated(elevate_args()):
        return None
    while not appcore.can_write(folder):
        box = QMessageBox(QMessageBox.Warning, APP, f"{folder}\n\n이 위치에는 관리자 권한이 있어야 파일을 만들 수 "
                                                    "있습니다. 다른 위치에 저장할까요?", parent=parent)
        other = box.addButton("다른 위치에 저장...", QMessageBox.AcceptRole)
        box.addButton("취소", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not other:
            return None
        target = pick(parent)
        if not target:
            return None
        folder = target if os.path.isdir(target) else os.path.dirname(os.path.abspath(target))
    return target


def _desktop() -> str:
    d = os.path.join(os.path.expanduser("~"), "Desktop")
    return d if os.path.isdir(d) else os.path.expanduser("~")


def ask_password(parent, archive, retry=False):
    pw, ok = QInputDialog.getText(parent, "암호 필요", ("암호가 틀렸습니다. 다시 입력:" if retry else "암호 입력:"),
                                  QLineEdit.Password)
    return pw if ok else None


def unique_dir(p: str) -> str:
    """A folder name not taken yet, Bandizip style: "name", else "name (2)", "name (3)", ..."""
    i, q = 2, p
    while os.path.exists(q):
        q, i = f"{p} ({i})", i + 1
    return q


def smart_dest(archive, dest: str, arc_path: str) -> str:
    tops = {e.name.replace("\\", "/").split("/")[0] for e in archive.entries() if e.name}
    if len(tops) <= 1:
        return dest
    return unique_dir(os.path.join(dest, formats.stem(arc_path)))


# Segoe Fluent Icons / MDL2 code points
G_EXTRACT, G_OPEN, G_ADD, G_MORE, G_UP, G_LOCK, G_TEST, G_DELETE, G_HOME = (
    "", "", "", "", "", "", "", "", "")
_FMT_COLOR = {"ZIP": "#2f6fed", "7Z": "#1a7f37", "TAR": "#8250df", "TAR.GZ": "#8250df", "TAR.BZ2": "#8250df",
              "TAR.XZ": "#8250df", "TAR.ZST": "#8250df"}


def glyph_icon(ch: str, color: str = "#64737D", px: int = 16):
    """Icon drawn from the system icon font, crisp at any DPI and free of image assets."""
    from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
    scale = 2
    pm = QPixmap(px * scale, px * scale)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    f = _icon_font()
    f.setPixelSize(int(px * scale * 0.9))
    p.setFont(f)
    p.setPen(QColor(color))
    p.drawText(pm.rect(), Qt.AlignCenter, ch)
    p.end()
    pm.setDevicePixelRatio(scale)
    return QIcon(pm)


class _Bridge(QObject):
    done = Signal(object, object)  # (result, error) from a worker thread, delivered on the GUI thread


class AssocDialog(QDialog):
    """Which archive types open in yzip. Shows what Windows really uses per type (a choice the user made in
    Windows stays in force until changed in Settings, which the second button opens)."""

    def __init__(self, parent):
        import assoc
        super().__init__(parent)
        self.assoc = assoc
        self.setWindowTitle("압축 파일 연결")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.setSpacing(10)
        head = QLabel("더블클릭하면 yzip으로 열 압축 파일 형식을 고르세요.")
        head.setStyleSheet("font-size: 10.5pt; font-weight: 600; color: #1f2328;")
        lay.addWidget(head)
        self.list = QTreeWidget()
        self.list.setHeaderLabels(["형식", "지금 여는 프로그램"])
        self.list.setRootIsDecorated(False)
        self.list.setColumnWidth(0, 170)
        self.list.setMinimumHeight(300)
        lay.addWidget(self.list)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #6e7781; font-size: 9pt;")
        lay.addWidget(self.note)
        row = QHBoxLayout()
        b_all = QPushButton("모두 선택")
        b_all.clicked.connect(lambda: self._check_all(True))
        row.addWidget(b_all)
        row.addStretch()
        b_set = QPushButton("기본 앱 설정 열기")
        b_set.clicked.connect(assoc.open_default_apps)
        self.b_apply = QPushButton("적용")
        self.b_apply.setDefault(True)
        self.b_apply.clicked.connect(self.apply)
        b_close = QPushButton("닫기")
        b_close.clicked.connect(self.accept)
        for b in (b_set, self.b_apply, b_close):
            row.addWidget(b)
        lay.addLayout(row)
        self.refresh()

    def _check_all(self, on):
        for i in range(self.list.topLevelItemCount()):
            self.list.topLevelItem(i).setCheckState(0, Qt.Checked if on else Qt.Unchecked)

    def refresh(self):
        st = self.assoc.status()
        registered = {e for e in self.assoc.TYPES if self.assoc._get(rf"Software\Classes\{self.assoc.progid(e)}")}
        self.list.clear()
        for ext, (mine, name) in st.items():
            it = QTreeWidgetItem([ext, "yzip" if mine else name])
            it.setCheckState(0, Qt.Checked if ext in registered else Qt.Unchecked)
            it.setData(0, Qt.UserRole, ext)
            if mine:
                it.setForeground(1, Qt.darkGreen)
            self.list.addTopLevelItem(it)
        others = [e for e, (mine, _) in st.items() if e in registered and not mine]
        self.note.setText(
            "Windows에 다른 프로그램이 기본으로 지정된 형식이 있습니다: " + ", ".join(others) +
            ". 이 형식은 '기본 앱 설정 열기'에서 yzip을 직접 골라야 바뀝니다 (Windows 정책)." if others else
            "체크한 형식은 '연결 프로그램' 목록에 yzip이 추가되고, 다른 기본 앱이 없으면 yzip으로 열립니다.")

    def apply(self):
        on = [self.list.topLevelItem(i).data(0, Qt.UserRole) for i in range(self.list.topLevelItemCount())
              if self.list.topLevelItem(i).checkState(0) == Qt.Checked]
        off = [e for e in self.assoc.TYPES if e not in on]
        if on:
            self.assoc.register(on)
        if off:
            self.assoc.unregister(off)
        self.refresh()


class _Row(QTreeWidgetItem):
    """Folders first, then by the column's raw value (sizes as numbers, not text)."""

    def __lt__(self, other):
        col = self.treeWidget().sortColumn() if self.treeWidget() else 0
        desc = self.treeWidget() and self.treeWidget().header().sortIndicatorOrder() == Qt.DescendingOrder
        a_dir, b_dir = self.data(0, Main.IS_DIR), other.data(0, Main.IS_DIR)
        if a_dir != b_dir:
            return (a_dir and not desc) or (b_dir and desc)  # folders stay on top in both directions
        ka, kb = self.data(col, Main.SORT), other.data(col, Main.SORT)
        if ka is None or kb is None:
            return self.text(col).lower() < other.text(col).lower()
        return ka < kb


class _ResponsiveRow(QWidget):
    """Keep natural control sizes, stacking only when the row cannot fit."""

    def __init__(self, margins=(0, 0, 0, 0), spacing=12):
        super().__init__()
        self._pending_reflow = False
        self.row = QBoxLayout(QBoxLayout.LeftToRight, self)
        self.row.setContentsMargins(*margins)
        self.row.setSpacing(spacing)
        self.row.setSizeConstraints(QLayout.SetNoConstraint, QLayout.SetMinimumSize)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def event(self, event):
        result = super().event(event)
        if event.type() in (QEvent.Resize, QEvent.LayoutRequest, QEvent.StyleChange, QEvent.FontChange) \
                and hasattr(self, "row") and not self._pending_reflow:
            self._pending_reflow = True
            QTimer.singleShot(0, self._reflow)
        return result

    def _reflow(self):
        self._pending_reflow = False
        margins = self.row.contentsMargins()
        widths = [self.row.itemAt(i).widget().sizeHint().width()
                  for i in range(self.row.count()) if self.row.itemAt(i).widget() is not None]
        required = sum(widths) + self.row.spacing() * max(0, len(widths) - 1)
        direction = (QBoxLayout.TopToBottom if required > self.width() - margins.left() - margins.right()
                     else QBoxLayout.LeftToRight)
        if self.row.direction() != direction:
            self.row.setDirection(direction)
        self.updateGeometry()


class Main(QMainWindow):
    """Browse an archive like a folder and extract it, or make a new one. The rarely used (add, delete,
    test, details, Explorer menu setup) sits in the '더 보기' menu and the list's right-click menu."""
    COLS = ["이름", "크기", "수정한 날짜", "압축 크기", "방식", "CRC", "암호"]
    DETAIL_COLS = (3, 4, 5, 6)  # hidden unless '자세한 정보 보기' is on
    NAME, IS_DIR, SORT = Qt.UserRole, Qt.UserRole + 1, Qt.UserRole + 2

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP} {__version__}")
        self.resize(640, 454)
        self.setMinimumSize(640, 420)
        self.setAcceptDrops(True)
        self.setStyleSheet(MAIN_QSS)
        self.arc_path: str | None = None
        self.password: str | None = None
        self.entries: list = []
        self.cwd = ""  # folder shown, "" = archive root, else "a/b/"
        self._icons = QFileIconProvider()
        self._icon_cache: dict = {}
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(self.COLS)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setIconSize(QSize(20, 20))
        self.tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.AscendingOrder)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for c, wdt in ((1, 96), (2, 150), (3, 96), (4, 80), (5, 86), (6, 50)):
            self.tree.setColumnWidth(c, wdt)
        self.tree.itemActivated.connect(self._activate)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._list_menu)
        self.tree.itemSelectionChanged.connect(self._selection_changed)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._home())
        self.pages.addWidget(self._list_page())
        central = QWidget()
        cl = QVBoxLayout(central)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addWidget(self._app_header())
        cl.addWidget(self._update_bar())
        cl.addWidget(self.pages, 1)
        self.setCentralWidget(central)
        self.release = None
        self.status = self.statusBar()
        self.status.setSizeGripEnabled(False)
        self._actions()
        self._show_details(bool(load_settings().get("show_details")))
        self._set_page(False)
        appearance.apply_appearance(QApplication.instance())

    # ----- pages -----
    def _app_header(self) -> QWidget:
        head = _ResponsiveRow(margins=(24, 12, 24, 12))
        head.setObjectName("appHeader")
        row = head.row
        branding = QWidget()
        brand_row = QHBoxLayout(branding)
        brand_row.setContentsMargins(0, 0, 0, 0)
        brand = QLabel("yzip")
        brand.setObjectName("brand")
        brand_row.addWidget(brand)
        version = QLabel(__version__)
        version.setObjectName("appVersion")
        brand_row.addWidget(version)
        brand_row.addStretch()
        row.addWidget(branding)
        actions = _ResponsiveRow(spacing=6)
        actions.setObjectName("headerActions")
        self.home_btn = QPushButton("홈")
        self.home_btn.setObjectName("homeNav")
        self.home_btn.clicked.connect(self.go_home)
        self.settings_btn = QPushButton("환경설정")
        self.settings_btn.setObjectName("settingsButton")
        self.settings_btn.clicked.connect(lambda: self.preferences())
        self.update_check_btn = QPushButton("업데이트 확인")
        self.update_check_btn.setObjectName("updateCheckButton")
        self.update_check_btn.clicked.connect(lambda: self.start_update_check(manual=True))
        for button in (self.home_btn, self.settings_btn, self.update_check_btn):
            actions.row.addWidget(button)
        row.addWidget(actions, 1)
        return head

    def go_home(self):
        """Close the archive view so home drops always create a new archive."""
        self.search.clear()
        self.tree.clearSelection()
        self.arc_path = self.password = None
        self.entries = []
        self.dirs = {}
        self.cwd = ""
        self.tree.clear()
        self.setWindowTitle(f"{APP} {__version__}")
        self._set_page(False)

    def _home(self) -> QWidget:
        scroll = self.home_scroll = QScrollArea()
        scroll.setObjectName("homeScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.NoFrame)
        page = QWidget()
        page.setObjectName("home")
        outer = QVBoxLayout(page)
        outer.setSizeConstraints(QLayout.SetNoConstraint, QLayout.SetMinimumSize)
        outer.setContentsMargins(24, 16, 24, 12)
        outer.setSpacing(8)
        outer.setAlignment(Qt.AlignTop)
        title = self.homeTitle = QLabel("압축 파일 작업, 간편하게")
        title.setObjectName("homeTitle")
        title.setWordWrap(True)
        outer.addWidget(title)
        intro = QLabel("압축 파일을 열고 풀거나, 파일과 폴더를 하나로 모으세요.")
        intro.setObjectName("homeHint")
        intro.setWordWrap(True)
        outer.addWidget(intro)
        box = self.drop_zone = QWidget()
        box.setObjectName("homeDrop")
        box.setAttribute(Qt.WA_StyledBackground, True)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(8)
        drop_title = QLabel("파일을 여기로 끌어다 놓으세요")
        drop_title.setObjectName("dropTitle")
        drop_title.setWordWrap(True)
        hint = QLabel("압축 파일은 바로 열고, 다른 파일·폴더는 새로 압축합니다")
        hint.setObjectName("homeHint")
        hint.setWordWrap(True)
        lay.addWidget(drop_title)
        lay.addWidget(hint)
        actions = _ResponsiveRow()
        actions.setObjectName("homeActions")
        row = actions.row
        self.home_open_btn = QPushButton("압축 파일 열기")
        self.home_open_btn.setObjectName("homeMain")
        self.home_open_btn.clicked.connect(lambda: self.open_dialog())
        self.home_new_btn = QPushButton("새로 압축하기")
        self.home_new_btn.setObjectName("homeBtn")
        self.home_new_btn.clicked.connect(lambda: self.new_archive())
        for button, text in ((self.home_open_btn, "내용 보기·풀기  ·  Ctrl+O"),
                             (self.home_new_btn, "파일·폴더 압축  ·  Ctrl+N")):
            column = QWidget()
            col = QVBoxLayout(column)
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(4)
            col.addWidget(button)
            note = QLabel(text)
            note.setObjectName("homeActionHint")
            note.setWordWrap(False)
            col.addWidget(note)
            row.addWidget(column)
        row.addStretch()
        lay.addSpacing(4)
        lay.addWidget(actions)
        outer.addWidget(box)
        self.homeFoot = QLabel("열기·풀기: ZIP · 7Z · RAR · EGG · ALZ · TAR · ISO · CAB · ZIPX · LZH · ZPAQ")
        self.homeFoot.setObjectName("homeFoot")
        self.homeFoot.setWordWrap(True)
        outer.addWidget(self.homeFoot)
        create = QLabel("만들기: ZIP · 7Z · TAR 계열 · GZ · BZ2 · XZ" +
                        (" · ZST" if "zst" in formats.BY_ID else ""))
        create.setObjectName("homeCreateFormats")
        create.setWordWrap(True)
        outer.addWidget(create)
        bottom = QHBoxLayout()
        self.formats_btn = QPushButton("상세 지원 형식")
        self.formats_btn.setObjectName("formatsLink")
        self.formats_btn.clicked.connect(lambda: self.preferences(section="associations"))
        bottom.addWidget(self.formats_btn)
        bottom.addStretch()
        version = QLabel(f"yzip {__version__}")
        version.setObjectName("homeVersion")
        bottom.addWidget(version)
        outer.addLayout(bottom)
        scroll.setWidget(page)
        return scroll

    def _list_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("listPage")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        # archive card: format badge, name, one line of facts, lock pill when encrypted
        head = QWidget()
        head.setObjectName("archHead")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(18, 14, 18, 12)
        hl.setSpacing(14)
        self.badge = QLabel()
        self.badge.setObjectName("archBadge")
        self.badge.setFixedSize(46, 46)
        self.badge.setAlignment(Qt.AlignCenter)
        hl.addWidget(self.badge)
        txt = QVBoxLayout()
        txt.setSpacing(2)
        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        self.arch_name = QLabel()
        self.arch_name.setObjectName("archName")
        self.arch_name.setTextFormat(Qt.PlainText)
        self.arch_name.setWordWrap(True)
        self.lock = QLabel("암호")
        self.lock.setObjectName("lockPill")
        name_row.addWidget(self.arch_name)
        name_row.addWidget(self.lock)
        name_row.addStretch()
        self.arch_meta = QLabel()
        self.arch_meta.setObjectName("archMeta")
        self.arch_meta.setWordWrap(True)
        txt.addLayout(name_row)
        txt.addWidget(self.arch_meta)
        hl.addLayout(txt, 1)
        lay.addWidget(head)
        # breadcrumb: up, archive root, folders
        bar = QWidget()
        bar.setObjectName("crumbBar")
        self.crumbs = QHBoxLayout(bar)
        self.crumbs.setContentsMargins(12, 5, 12, 5)
        self.crumbs.setSpacing(2)
        self.up_btn = QToolButton()
        self.up_btn.setIcon(glyph_icon(G_UP))
        self.up_btn.setToolTip("상위 폴더 (Backspace)")
        self.up_btn.clicked.connect(self.go_up)
        lay.addWidget(bar)
        search_bar = QWidget()
        search_bar.setObjectName("searchBar")
        search_row = QHBoxLayout(search_bar)
        search_row.setContentsMargins(18, 8, 18, 8)
        self.search = QLineEdit()
        self.search.setObjectName("archiveSearch")
        self.search.setPlaceholderText("현재 폴더에서 파일 이름 검색")
        self.search.setAccessibleName("현재 폴더에서 파일 이름 검색")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter_items)
        search_row.addWidget(self.search, 1)
        self.search_summary = QLabel()
        self.search_summary.setObjectName("searchSummary")
        search_row.addWidget(self.search_summary)
        lay.addWidget(search_bar)
        lay.addWidget(self.tree, 1)
        return page

    def _set_page(self, has_archive: bool):
        self.pages.setCurrentIndex(1 if has_archive else 0)
        self.toolbar.setVisible(has_archive)
        for a in self.archive_acts:
            a.setEnabled(has_archive)
        self.a_extract.setEnabled(has_archive and bool(self._extraction_scope()[0] or not self.search.text().strip()))
        self.a_repair.setEnabled(has_archive and bool(self.arc_path) and formats.is_zip(self.arc_path))
        self.status.setVisible(has_archive)
        if not has_archive:
            self.status.showMessage("")

    # ----- actions -----
    def _act(self, text, fn, key=None, glyph=None):
        a = QAction(text, self)
        a.triggered.connect(fn)
        if key:
            a.setShortcut(QKeySequence(key))
        if glyph:
            a.setIcon(glyph_icon(glyph))
        self.addAction(a)  # shortcuts work even when the action only lives in a hidden menu
        return a

    def _actions(self):
        self.a_extract = self._act("풀기", self.extract, "Ctrl+E")
        self.a_extract.setIcon(glyph_icon(G_EXTRACT, "#ffffff"))
        self.a_open = self._act("열기", self.open_dialog, "Ctrl+O", G_OPEN)
        self.a_new = self._act("새로 압축", lambda: self.new_archive(), "Ctrl+N", G_ADD)
        self.a_add = self._act("파일 추가…", self.add_files, "Ctrl+Shift+A", G_ADD)
        self.a_delete = self._act("선택 항목 삭제", self.delete_selected, "Del", G_DELETE)
        self.a_test = self._act("압축 파일 검사", self.test, "Ctrl+T", G_TEST)
        self.a_repair = self._act("손상된 ZIP 복구…", self.repair_archive)
        self.a_details = self._act("자세한 정보 보기", lambda on: self._show_details(on, save=True))
        self.a_details.setCheckable(True)
        self.a_up = self._act("상위 폴더", self.go_up, "Backspace")
        self.archive_acts = [self.a_extract, self.a_add, self.a_delete, self.a_test, self.a_repair]

        tb = self.toolbar = QToolBar("main")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setIconSize(QSize(16, 16))
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tb.setContextMenuPolicy(Qt.PreventContextMenu)
        self.centralWidget().layout().insertWidget(2, tb)
        tb.addAction(self.a_extract)
        tb.widgetForAction(self.a_extract).setObjectName("primary")
        tb.addAction(self.a_open)
        tb.addAction(self.a_new)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        more = QMenu(self)
        for a in (self.a_add, self.a_delete, self.a_test, self.a_repair):
            more.addAction(a)
        more.addSeparator()
        more.addAction(self.a_details)
        more.addSeparator()
        more.addAction(self._act("환경설정…", self.preferences))
        more.addAction(self._act("압축 파일 연결…", self.associations))
        more.addAction(self._act("탐색기 우클릭 메뉴 등록", self.register_shell))
        more.addAction(self._act("탐색기 우클릭 메뉴 제거", self.unregister_shell))
        more.addSeparator()
        more.addAction(self._act("업데이트 확인", lambda: self.start_update_check(manual=True)))
        self.a_auto_update = self._act("업데이트 자동 확인", lambda on: save_setting("auto_update", on))
        self.a_auto_update.setCheckable(True)
        self.a_auto_update.setChecked(bool(load_settings().get("auto_update", True)))
        more.addAction(self.a_auto_update)
        more.addAction(self._act("yzip 정보", self.about))
        self.more_btn = QToolButton()
        self.more_btn.setObjectName("more")
        self.more_btn.setText("더 보기")
        self.more_btn.setIcon(glyph_icon(G_MORE))
        self.more_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.more_btn.setAccessibleName("더 보기")
        self.more_btn.setMenu(more)
        self.more_btn.setPopupMode(QToolButton.InstantPopup)
        tb.addWidget(self.more_btn)

    def _show_details(self, on: bool, save: bool = False):
        self.a_details.setChecked(on)
        for c in self.DETAIL_COLS:
            self.tree.setColumnHidden(c, not on)
        if save:
            save_setting("show_details", on)

    def _list_menu(self, pos):
        has = bool(self.tree.selectedItems())
        m = QMenu(self)
        a = m.addAction("열기", self.open_selected)
        a.setEnabled(has)
        m.addAction(glyph_icon(G_EXTRACT), "선택 항목 풀기…" if has else "모두 풀기…", self.extract)
        m.addSeparator()
        m.addAction(self.a_add)
        a = m.addAction(glyph_icon(G_DELETE), "삭제", self.delete_selected)
        a.setEnabled(has)
        m.exec(self.tree.viewport().mapToGlobal(pos))

    # ----- updates -----
    def _update_bar(self) -> QWidget:
        bar = self.upd_bar = QWidget()
        bar.setObjectName("updBar")
        bar.setAttribute(Qt.WA_StyledBackground, True)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 8, 12, 8)
        self.upd_text = QLabel()
        self.upd_text.setObjectName("updText")
        lay.addWidget(self.upd_text, 1)
        self.upd_btn = QPushButton("지금 업데이트")
        self.upd_btn.setObjectName("updBtn")
        self.upd_btn.setCursor(Qt.PointingHandCursor)
        self.upd_btn.clicked.connect(self.do_update)
        later = QToolButton()
        later.setText("나중에")
        later.setObjectName("updLater")
        later.clicked.connect(bar.hide)
        lay.addWidget(self.upd_btn)
        lay.addWidget(later)
        bar.hide()
        return bar

    def start_update_check(self, manual: bool = False):
        """Check in the background on every app launch when enabled; manual checks always run."""
        import threading
        import updater
        if not manual and not updater.due(load_settings()):
            return
        if not manual and not (getattr(sys, "frozen", False) or os.environ.get("YZIP_UPDATE_URL")):
            return  # running from source: no automatic checks
        bridge = self._upd_bridge = _Bridge()
        bridge.done.connect(lambda rel, err: self._update_result(rel, err, manual))

        def work():
            try:
                bridge.done.emit(updater.check(), None)
            except Exception as e:
                bridge.done.emit(None, e)
        threading.Thread(target=work, daemon=True).start()

    def _update_result(self, rel, err, manual):
        from version import __version__
        checked = datetime.now().astimezone().isoformat(timespec="seconds")
        result = (f"업데이트 확인 실패: {err}" if err is not None else
                  f"새 버전: {rel.version}" if rel is not None else f"최신 버전: {__version__}")
        appcore.save_setting("update_last_checked", checked)
        appcore.save_setting("update_last_result", result)
        self.status.showMessage(result, 10000)
        if err is not None:
            log_exception(err)
        if rel is not None:
            self.release = rel
            self.upd_text.setText(f"새 버전 <b>yzip {rel.version}</b>이(가) 나왔습니다. 지금 버전: {__version__}")
            self.upd_bar.show()
            if not manual:
                answer = QMessageBox.question(
                    self,
                    "yzip 업데이트",
                    f"새 버전 yzip {rel.version}이 있습니다.\n지금 업데이트하시겠습니까?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.Yes,
                )
                if answer == QMessageBox.Yes:
                    self.do_update()
        elif manual:
            if err is not None:
                QMessageBox.warning(self, APP, f"업데이트를 확인하지 못했습니다.\n{err}")
            else:
                QMessageBox.information(self, APP, f"최신 버전입니다. (yzip {__version__})")

    def do_update(self):
        import updater
        rel = self.release
        if rel is None:
            return
        ok, r = run_job(self, f"업데이트 내려받는 중 ({rel.version})", lambda p, c: updater.download(rel, p, c),
                        done_text="내려받기를 마쳤습니다.")
        if not ok:
            self._report(r)
            return
        updater.launch_installer(r)
        QApplication.quit()  # the installer closes and replaces yzip, then starts the new version

    def about(self):
        from version import REPO, __version__
        QMessageBox.about(self, "yzip 정보", f"<b>yzip {__version__}</b><br>가볍고 빠른 압축 프로그램<br><br>"
                                             f"<a href='https://github.com/{REPO}'>github.com/{REPO}</a>")

    def associations(self):
        AssocDialog(self).exec()

    def preferences(self, section=None):
        dlg = SettingsDialog(self)
        if section == "associations":
            dlg.tabs.setCurrentIndex(1)
        if dlg.exec() != QDialog.Accepted:
            return
        try:
            if dlg.apply() is False:
                return
            appearance.apply_appearance(QApplication.instance())
            appcore.apply_runtime_settings()
            if load_settings().get("shell_enabled", True):
                shell_register()
            else:
                shell_unregister()
            self.a_auto_update.setChecked(dlg.auto_update_check.isChecked())
        except Exception as exc:
            log_exception(exc)
            QMessageBox.warning(self, APP, f"환경설정을 적용하지 못했습니다.\n{exc}")

    # ----- drag & drop -----
    def _hot(self, on: bool):
        self.drop_zone.setProperty("hot", on)
        self.drop_zone.style().unpolish(self.drop_zone)
        self.drop_zone.style().polish(self.drop_zone)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._hot(True)

    def dragLeaveEvent(self, e):
        self._hot(False)

    def dropEvent(self, e):
        self._hot(False)
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if not paths:
            return
        if len(paths) == 1 and paths[0].lower().endswith(tuple(formats.READ_EXTS) + (".001",)) \
                and os.path.isfile(paths[0]):
            self.open_archive(paths[0])
        elif self.arc_path:
            if self._editable():  # ZIP only; says why otherwise
                self.add_paths(paths)
        else:
            self.new_archive(paths)

    # ----- open / list -----
    def open_dialog(self):
        p, _ = QFileDialog.getOpenFileName(self, "압축 파일 열기", "", formats.OPEN_FILTER)
        if p:
            self.open_archive(p)

    def _open(self, path, pw):
        """Any supported format. A 7z with encrypted names cannot even be listed without the password, so
        that case asks for it here (None if the user gives up)."""
        while True:
            try:
                return formats.open_any(path, pw), pw
            except ziplib.BadPassword:
                pw = ask_password(self, path, retry=pw is not None)
                if pw is None:
                    return None, None

    def open_archive(self, path: str) -> bool:
        try:
            a, pw = self._open(path, None)
        except Exception as e:
            candidate = False
            if os.path.isfile(path) and os.path.splitext(path)[1].lower() == ".zip":
                try:
                    with open(path, "rb") as file:
                        candidate = file.read(4) == b"PK\x03\x04"
                except OSError:
                    pass
            if candidate and QMessageBox.question(
                    self, APP, f"ZIP 파일을 정상적으로 열 수 없습니다.\n{e}\n\n복구를 시도할까요?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) == QMessageBox.Yes:
                self.repair_archive(path)
            else:
                QMessageBox.critical(self, APP, f"열 수 없습니다: {e}")
            return False
        if a is None:
            return False
        with a:
            entries = a.entries()
            enc = a.needs_password()
        same = path == self.arc_path
        if not same:
            self.search.clear()
        self.arc_path, self.password, self.entries = path, pw, entries
        # folders the archive implies but does not list ("a/b/c.txt" alone still has folders a/ and a/b/)
        self.dirs = {}
        for e in entries:
            parts = e.name.rstrip("/").split("/")
            for i in range(1, len(parts) + (1 if e.is_dir else 0)):
                self.dirs.setdefault("/".join(parts[:i]) + "/", None)
            if e.is_dir:
                self.dirs[e.name] = e
        if not same or self.cwd not in self.dirs:
            self.cwd = ""
        tot = sum(e.size for e in entries)
        packed = os.path.getsize(path) if os.path.exists(path) else sum(e.csize for e in entries)
        files = sum(1 for e in entries if not e.is_dir)
        label = (entries[0].method if entries and entries[0].method in _FMT_COLOR else
                 "ZIP" if formats.is_zip(path) else os.path.splitext(path)[1].lstrip(".").upper() or "?")
        self.badge.setText(label.split(".")[-1] if len(label) > 4 else label)
        self.badge.setStyleSheet("background: #16776A;")
        self.arch_name.setText(os.path.basename(path))
        ratio = f" ({packed * 100 // tot}%)" if tot else ""
        self.arch_meta.setText(f"{label}  ·  파일 {files:,}개  ·  {fmt_size(tot)} → {fmt_size(packed)}{ratio}")
        self.lock.setVisible(bool(enc))
        self.setWindowTitle(f"{os.path.basename(path)} - {APP} {__version__}")
        self._fill()
        self._set_page(True)
        self.status.showMessage("")
        return True

    def _icon(self, name: str, is_dir: bool):
        from PySide6.QtCore import QFileInfo
        key = "/" if is_dir else os.path.splitext(name)[1].lower()
        if key not in self._icon_cache:
            self._icon_cache[key] = self._icons.icon(QFileIconProvider.Folder) if is_dir else \
                self._icons.icon(QFileInfo("x" + key))
        return self._icon_cache[key]

    def _fill(self):
        """Show the direct children of self.cwd: folders first, each with the system icon of its type."""
        cwd = self.cwd
        kids_dirs = sorted(d for d in self.dirs if d != cwd and d.startswith(cwd) and "/" not in d[len(cwd):-1])
        kids_files = [e for e in self.entries if not e.is_dir and e.name.startswith(cwd)
                      and "/" not in e.name[len(cwd):]]
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        for d in kids_dirs:
            inside = [e for e in self.entries if not e.is_dir and e.name.startswith(d)]
            e = self.dirs.get(d)
            mt = e.mtime if e else max((x.mtime for x in inside), default=(1980, 1, 1, 0, 0, 0))
            it = _Row([d[len(cwd):-1], f"{len(inside):,}개 항목", "%04d-%02d-%02d %02d:%02d" % mt[:5],
                       "", "", "", ""])
            it.setData(1, self.SORT, sum(x.size for x in inside))
            it.setData(2, self.SORT, tuple(mt))
            it.setData(0, self.NAME, d)
            it.setData(0, self.IS_DIR, True)
            it.setIcon(0, self._icon(d, True))
            self.tree.addTopLevelItem(it)
        for e in kids_files:
            it = _Row([e.name[len(cwd):], fmt_size(e.size), "%04d-%02d-%02d %02d:%02d" % e.mtime[:5],
                       fmt_size(e.csize), e.method, f"{e.crc:08X}", "예" if e.encrypted else ""])
            it.setData(1, self.SORT, e.size)
            it.setData(2, self.SORT, tuple(e.mtime))
            it.setData(3, self.SORT, e.csize)
            it.setData(0, self.NAME, e.name)
            it.setData(0, self.IS_DIR, False)
            it.setIcon(0, self._icon(e.name, False))
            for c in (1, 3):
                it.setTextAlignment(c, Qt.AlignRight | Qt.AlignVCenter)
            self.tree.addTopLevelItem(it)
        for i in range(len(kids_dirs)):
            self.tree.topLevelItem(i).setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(self.tree.sortColumn() if self.tree.sortColumn() >= 0 else 0,
                               self.tree.header().sortIndicatorOrder())
        self._crumbs()
        self._filter_items()

    def _filter_items(self, _text=None):
        """Filter direct children only; hidden rows cannot remain selected."""
        query = self.search.text().strip().casefold()
        count = 0
        self.tree.blockSignals(True)
        try:
            for index in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(index)
                visible = query in item.text(0).casefold()
                if not visible:
                    item.setSelected(False)
                item.setHidden(not visible)
                count += visible
        finally:
            self.tree.blockSignals(False)
        self.search_summary.setText(f"{count:,} / {self.tree.topLevelItemCount():,}")
        self._selection_changed()

    def _crumbs(self):
        while self.crumbs.count():
            w = self.crumbs.takeAt(0).widget()
            if w is not None and w is not self.up_btn:
                w.deleteLater()
        self.crumbs.addWidget(self.up_btn)
        self.up_btn.setEnabled(bool(self.cwd))
        self.a_up.setEnabled(bool(self.cwd))
        parts = [p for p in self.cwd.split("/") if p]
        stops = [("", os.path.basename(self.arc_path))] + [("/".join(parts[:i + 1]) + "/", p) for i, p in enumerate(parts)]
        for i, (target, label) in enumerate(stops):
            if i:
                sep = QLabel("›")
                sep.setObjectName("crumbSep")
                self.crumbs.addWidget(sep)
            b = QToolButton()
            b.setText(label)
            if i == len(stops) - 1:
                b.setObjectName("crumbLast")
            if i == 0:
                b.setIcon(glyph_icon(G_HOME))
                b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            b.clicked.connect(lambda _=False, t=target: self.go_to(t))
            self.crumbs.addWidget(b)
        self.crumbs.addStretch()

    def go_to(self, folder: str):
        if folder == "" or folder in self.dirs:
            self.cwd = folder
            self.search.clear()
            self._fill()

    def go_up(self):
        if self.cwd:
            parts = self.cwd.rstrip("/").split("/")[:-1]
            self.go_to("/".join(parts) + "/" if parts else "")

    def _selection_changed(self):
        items = self.tree.selectedItems()
        if not self.arc_path:
            return
        self.a_extract.setEnabled(self.pages.currentIndex() == 1 and
                                  bool(self._extraction_scope()[0] or not self.search.text().strip()))
        if items:
            n = len(self.selected_names())
            self.status.showMessage(f"{len(items)}개 선택 ({n:,}개 파일 포함)" if n != len(items) else f"{len(items)}개 선택")
        else:
            self.status.showMessage(f"{self.tree.topLevelItemCount():,}개 항목")

    def selected_names(self) -> list[str]:
        """Archive names under the selection: a selected folder stands for everything inside it."""
        return self._names_for_items(self.tree.selectedItems())

    def _names_for_items(self, items) -> list[str]:
        out = []
        for it in items:
            if it.isHidden():
                continue
            name = it.data(0, self.NAME)
            if it.data(0, self.IS_DIR):
                out += [e.name for e in self.entries if e.name.startswith(name)]
                if name not in out and self.dirs.get(name) is not None:
                    out.append(name)
            else:
                out.append(name)
        return list(dict.fromkeys(out))

    def _extraction_scope(self):
        """Return selected names, or visible search results, and their source."""
        names = self.selected_names()
        from_search = bool(self.search.text().strip()) and not names
        if from_search:
            names = self._names_for_items(self.tree.topLevelItem(i)
                                          for i in range(self.tree.topLevelItemCount()))
        return names, from_search

    def _activate(self, it, col=0):
        if it.data(0, self.IS_DIR):
            self.go_to(it.data(0, self.NAME))
        else:
            self.open_selected()

    def open_selected(self):
        items = self.tree.selectedItems()
        if not items or not self.arc_path:
            return
        if items[0].data(0, self.IS_DIR):
            self.go_to(items[0].data(0, self.NAME))
            return
        name = items[0].data(0, self.NAME)
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="yzip_"))
        if self._do_extract(tmp, [name], "overwrite"):
            p = ziplib.safe_target(tmp, name)  # where the extractor wrote it; never a path the name points to
            if p and os.path.isfile(p):
                open_path(p)

    # ----- create -----
    def new_archive(self, paths=None, standalone=False, out=None, ask=None):
        """standalone: launched from the Explorer menu, no main window. ask: show the compress window (format,
        password, ...); by default only when not standalone, so "<name>.zip으로 압축하기" compresses at once.
        out: output chosen by the shell extension, which already showed that name in the menu."""
        parent = None if standalone else self
        ask = (not standalone) if ask is None else ask
        if not paths:
            paths = QFileDialog.getOpenFileNames(parent, "압축할 파일 선택")[0]
        if not paths:
            return
        settings = load_settings()
        if standalone and len(paths) > 1 and not out and settings.get("shell_multi", "combined") == "separate":
            for path in paths:
                self.new_archive([path], standalone=True, ask=ask)
            return
        first = os.path.abspath(paths[0])
        if len(paths) > 1:
            base = os.path.join(os.path.dirname(first), os.path.basename(os.path.dirname(first)) or "archive")
        else:
            base = first if os.path.isdir(first) else os.path.splitext(first)[0]  # folder "v1.2" stays "v1.2"
        default_path = base + ".zip"
        if settings.get("compress_dir"):
            default_path = os.path.join(settings["compress_dir"], os.path.basename(default_path))
        default_path = unique_path(default_path)
        if not ask:  # like Bandizip's "<name>.zip으로 압축하기": no options, just a progress window
            defaults = CompressDialog(parent, default_path, paths)
            if out:  # the shell menu explicitly promised this ZIP name and location
                defaults.fmt.setCurrentIndex(defaults.fmt.findData("zip"))
                defaults.path.setText(out)
                defaults.split.setCurrentText(str(settings.get("compress_split", "분할 안 함")))
            o = defaults.options()
            out = o.pop("out_path")
            defaults.deleteLater()
            import appcore
            first_out = out
            out = writable_target(
                parent, out, lambda: ["--archive-list", appcore.write_list(paths), "--out", first_out],
                lambda w: QFileDialog.getSaveFileName(w, "다른 위치에 저장", unique_path(
                    os.path.join(_desktop(), os.path.basename(first_out))), "ZIP 파일 (*.zip)")[0])
            if not out:
                return
        else:
            dlg = CompressDialog(parent, default_path, paths)
            auto = os.environ.get("YZIP_AUTOACCEPT")
            if auto:  # unattended end-to-end tests of the built exe; a format id ("7z") also picks that format
                if auto in formats.BY_ID:
                    dlg.fmt.setCurrentIndex(dlg.fmt.findData(auto))
                QTimer.singleShot(50, dlg.accept)
            if dlg.exec() != QDialog.Accepted:
                return
            o = dlg.options()
            out = o.pop("out_path")
        fmt = o.pop("fmt")
        def create(prog, canc):
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            return formats.create(fmt, paths, out, progress=prog, cancel=canc, **o)

        ok, r = run_job(parent, f"압축하기 {os.path.basename(out)}",
                        create,
                        reveal=out, done_text="압축 작업을 완료했습니다.")
        if not ok:
            self._report(r)
        else:
            if settings.get("compress_open_folder", False):
                open_path(os.path.dirname(os.path.abspath(out)))
            if not standalone:
                self.status.showMessage("완료: " + ", ".join(os.path.basename(x) for x in r), 8000)
                self.open_archive(r[0])

    def _editable(self) -> bool:
        """Adding / deleting rebuilds the archive with the ZIP writer, so only plain ZIP files qualify."""
        if not self.arc_path:
            return False
        if ziplib.is_split_first(self.arc_path):
            QMessageBox.information(self, APP, "분할 압축은 수정할 수 없습니다. 먼저 하나로 합쳐 주세요.")
            return False
        if not formats.is_zip(self.arc_path):
            QMessageBox.information(self, APP, "파일 추가·삭제는 ZIP 형식에서만 됩니다. 다른 형식은 풀어서 새로 압축해 주세요.")
            return False
        return True

    def add_files(self):
        if not self._editable():
            return
        paths = QFileDialog.getOpenFileNames(self, "추가할 파일")[0]
        if paths:
            self.add_paths(paths)

    def add_paths(self, paths):
        arc, into = self.arc_path, self.cwd

        def job(prog, canc):  # existing entries are copied as they are; same names are replaced
            # self.password is read here, after _modify asked for it
            ziplib.edit_zip(arc, add=paths, password=self.password, progress=prog, cancel=canc, into=into)
        self._modify("추가 중...", job)

    def delete_selected(self):
        names = self.selected_names()
        if not names or not self._editable():
            return
        if QMessageBox.question(self, APP, f"{len(names)}개 항목을 압축 파일에서 삭제할까요?") != QMessageBox.Yes:
            return
        arc, doomed = self.arc_path, tuple(names)

        def job(prog, canc):  # the other entries are copied byte for byte, nothing is decompressed
            ziplib.edit_zip(arc, drop=doomed, progress=prog, cancel=canc)
        self._modify("삭제 중...", job)

    def _modify(self, title, job):
        if self.arc_path and not self._ensure_password():
            return
        ok, r = run_job(self, title, job)
        if ok:
            self.open_archive(self.arc_path)
        else:
            self._report(r)

    # ----- extract / test -----
    def _ensure_password(self) -> bool:
        if self.password:
            return True
        try:
            with formats.open_any(self.arc_path) as z:
                need = z.needs_password()
        except ziplib.BadPassword:  # 7z with encrypted names
            need = True
        if need:
            pw = ask_password(self, self.arc_path)
            if pw is None:
                return False
            self.password = pw
        return True

    def extract(self):
        if not self.arc_path:
            return
        names, from_search = self._extraction_scope()
        if from_search and not names:
            QMessageBox.information(self, APP, "검색 결과가 없습니다.")
            return
        dlg = ExtractDialog(self, os.path.dirname(os.path.abspath(self.arc_path)), len(names))
        if from_search:
            dlg.sel_only.setText(f"검색 결과 {len(names):,}개 항목만")
        if dlg.exec() != QDialog.Accepted:
            return
        mode = ("ask" if dlg.mode_ask.isChecked() else "skip" if dlg.mode_skip.isChecked()
                else "rename" if dlg.mode_ren.isChecked() else "overwrite")
        dest = dlg.dest.text()
        if dlg.smart.isChecked():
            if not self._ensure_password():
                return
            with formats.open_any(self.arc_path, self.password) as z:
                dest = smart_dest(z, dest, self.arc_path)
        if self._do_extract(dest, names if dlg.sel_only.isChecked() else None, mode):
            self.status.showMessage(f"풀기 완료: {dest}", 8000)
            if load_settings().get("extract_open_folder", False):
                open_path(dest)

    def _collision_mode(self, collisions):
        """Ask on the GUI thread before any extraction worker is started."""
        box = QMessageBox(QMessageBox.Question, APP,
                          f"같은 이름의 대상 {len(collisions)}개가 있습니다.\n어떻게 처리할까요?",
                          QMessageBox.Yes | QMessageBox.No | QMessageBox.Retry | QMessageBox.Cancel, self)
        choices = {QMessageBox.Yes: "overwrite", QMessageBox.No: "skip", QMessageBox.Retry: "rename"}
        for button, label in ((QMessageBox.Yes, "덮어쓰기"), (QMessageBox.No, "건너뛰기"),
                              (QMessageBox.Retry, "이름변경"), (QMessageBox.Cancel, "취소")):
            box.button(button).setText(label)
        box.setDefaultButton(QMessageBox.Cancel)
        box.setEscapeButton(QMessageBox.Cancel)
        box.exec()
        # Qt can recreate button widgets when applying style/font changes on show.
        return choices.get(box.standardButton(box.clickedButton()))

    def _extract_collisions(self, dest, names):
        root = os.path.realpath(dest)
        wanted = None if names is None else set(names)
        collisions = []
        with formats.open_any(self.arc_path, self.password) as archive:
            for entry in archive.entries():
                if wanted is not None and entry.name not in wanted:
                    continue
                target = ziplib.safe_target(root, entry.name)
                if target is not None and os.path.lexists(target) and (not entry.is_dir or not os.path.isdir(target)):
                    collisions.append(target)
        return collisions

    def _do_extract(self, dest, names, mode) -> bool:
        if not self._ensure_password():
            return False
        while True:
            arc, pw = self.arc_path, self.password

            if mode == "ask":
                try:
                    collisions = self._extract_collisions(dest, names)
                except ziplib.BadPassword:
                    pw2 = ask_password(self, arc, retry=True)
                    if pw2 is None:
                        return False
                    self.password = pw2
                    continue
                except Exception as exc:
                    log_exception(exc)
                    self._report(("error", str(exc)))
                    return False
                chosen = self._collision_mode(collisions) if collisions else "overwrite"
                if chosen is None:
                    return False
            else:
                chosen = mode

            def job(prog, canc):
                with formats.open_any(arc, pw) as z:
                    z.extract(dest, names, progress=prog, cancel=canc,
                              overwrite="rename" if chosen == "rename" else chosen != "skip")
            ok, r = run_job(self, "압축 푸는 중...", job, reveal=dest, done_text="압축 풀기를 완료했습니다.")
            if ok:
                return True
            if r and r[0] == "password":
                pw2 = ask_password(self, arc, retry=True)
                if pw2 is None:
                    return False
                self.password = pw2
                continue
            self._report(r)
            return False

    def repair_archive(self, path: str | None = None):
        arc = path or self.arc_path
        if not arc:
            arc, _ = QFileDialog.getOpenFileName(self, "복구할 ZIP 선택", "", "ZIP (*.zip)")
            if not arc:
                return
        if os.path.splitext(arc)[1].lower() != ".zip":
            QMessageBox.information(self, APP, "현재 복구 기능은 ZIP 형식만 지원합니다.")
            return
        base, _ = os.path.splitext(os.path.abspath(arc))
        default = unique_path(base + "_recovered.zip")
        out, _ = QFileDialog.getSaveFileName(self, "복구 ZIP 저장", default, "ZIP (*.zip)")
        if not out:
            return
        if os.path.realpath(arc) == os.path.realpath(out):
            QMessageBox.warning(self, APP, "원본 ZIP과 다른 이름으로 저장해 주세요.")
            return

        def job(prog, canc):
            return ziprepair.repair_zip(arc, out, progress=prog, cancel=canc)

        ok, result = run_job(self, "손상된 ZIP 복구 중...", job, reveal=out,
                             done_text="ZIP 복구를 완료했습니다.")
        if not ok:
            self._report(result)
            return
        if not result.output:
            QMessageBox.warning(self, APP, "복구 가능한 정상 파일을 찾지 못했습니다.")
            return
        message = f"{len(result.recovered):,}개 파일을 복구했습니다."
        if result.skipped:
            message += f"\n{len(result.skipped):,}개 항목은 손상되었거나 지원하지 않아 건너뛰었습니다."
            details = "\n".join(f"• {item.name}: {item.reason}" for item in result.skipped[:10])
            if details:
                message += "\n\n" + details
        QMessageBox.information(self, APP, message)

    def test(self):
        if not self.arc_path or not self._ensure_password():
            return
        arc, pw = self.arc_path, self.password

        def job(prog, canc):
            with formats.open_any(arc, pw) as z:
                return z.test(progress=prog, cancel=canc)
        ok, r = run_job(self, "무결성 검사 중...", job, done_text="검사를 완료했습니다.")
        if not ok:
            self._report(r)
        elif r:
            QMessageBox.warning(self, APP, "손상된 항목:\n" + "\n".join(r[:30]))
        else:
            QMessageBox.information(self, APP, "이상 없음: 모든 항목이 정상입니다.")

    def _report(self, r):
        kind, msg = r if r else ("error", "알 수 없는 오류")
        if kind == "cancel":
            self.status.showMessage("취소됨", 5000)
        elif kind == "password":
            self.password = None  # ask again next time instead of reusing the wrong one
            QMessageBox.warning(self, APP, "암호가 틀렸습니다.")
        else:
            QMessageBox.critical(self, APP, msg)

    def register_shell(self):
        shell_register()
        QMessageBox.information(self, APP, "등록했습니다. (사용자 계정 범위, 관리자 권한 불필요)\n"
                                           "Windows 11에서는 우클릭 후 '더 많은 옵션 표시'(Shift+F10)에 나타납니다.")

    def unregister_shell(self):
        shell_unregister()
        QMessageBox.information(self, APP, "제거했습니다.")


# ----- Explorer integration (HKCU only, no admin needed) -----
SHELL_VERBS = [(r"Software\Classes\*\shell\yzip.Compress", "ZIP으로 압축하기 - &yzip", "--archive"),
               (r"Software\Classes\Directory\shell\yzip.Compress", "ZIP으로 압축하기 - &yzip", "--archive")]
for _ext in formats.READ_EXTS:  # extract verbs for every format yzip reads
    SHELL_VERBS += [(rf"Software\Classes\SystemFileAssociations\{_ext}\shell\yzip.Here", "여기에 압축 풀기", "--extract-here"),
                    (rf"Software\Classes\SystemFileAssociations\{_ext}\shell\yzip.Folder", "폴더에 압축 풀기", "--extract-folder")]
# keys written by the pre-rename build ("ZipKit"); removed on register/unregister so they cannot point at a dead exe
LEGACY_SHELL_KEYS = [k.replace("yzip.", "ZipKit.") for k, _, _ in SHELL_VERBS]


def shell_command(flag: str) -> str:
    exe = sys.executable
    if getattr(sys, "frozen", False):
        return f'"{exe}" {flag} "%1"'
    return f'"{exe}" "{os.path.abspath(__file__)}" {flag} "%1"'


def _delete_key_tree(key: str):
    import winreg
    for sub in (key + r"\command", key):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
        except FileNotFoundError:
            pass


# The compress item comes from shell\yzipshell.dll (an IContextMenu handler) so it can say "<name>.zip으로 압축하기".
# Explorer keeps a loaded handler locked, so a versioned copy is installed outside dist (rebuilds stay possible).
SHELLEX_CLSID = "{7E3B1C52-9A4D-4F1E-B6C2-3D8A5F0E9C71}"
# Explorer lists handlers class by class (* and Directory before AllFilesystemObjects), each class in key-name order.
# Bandizip registers "AABdzCtx" under * and Directory; "AAAyzip" sorts just before it, so our items join the
# block of Windows' "압축 풀기(T)..." and Bandizip's items instead of starting their own block below them.
SHELLEX_KEYS = [rf"Software\Classes\{c}\shellex\ContextMenuHandlers\AAAyzip" for c in ("*", "Directory")]
# where the handler was registered before; removed on register/unregister so the items don't show twice
OLD_SHELLEX_KEYS = [r"Software\Classes\AllFilesystemObjects\shellex\ContextMenuHandlers\yzip"] + [
    rf"Software\Classes\{c}\shellex\ContextMenuHandlers\AAyzip" for c in ("*", "Directory")]
APP_KEY = r"Software\yzip"


def resource(name: str) -> str:
    """A file shipped next to the code (source run) or inside the PyInstaller bundle."""
    return os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), name)


def shell_dll_source() -> str:
    for p in (resource("yzipshell.dll"), os.path.join(os.path.dirname(os.path.abspath(__file__)), "shell", "build", "yzipshell.dll")):
        if os.path.exists(p):
            return p
    return ""


def _real_local_appdata() -> str:
    """The real per-user folder even when LOCALAPPDATA is redirected (tests do that for error.log)."""
    import ctypes
    buf = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 0x1C, None, 0, buf) == 0:  # CSIDL_LOCAL_APPDATA
        return buf.value
    return os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))


def install_shell_dll(src: str) -> str:
    import hashlib
    with open(src, "rb") as f:
        data = f.read()
    d = os.path.join(_real_local_appdata(), "yzip", "shell")
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, f"yzipshell_{hashlib.sha256(data).hexdigest()[:12]}.dll")
    if not os.path.exists(dst):
        with open(dst + ".tmp", "wb") as f:
            f.write(data)
        os.replace(dst + ".tmp", dst)
    for old in os.listdir(d):  # older copies; one still loaded by Explorer stays until it unloads
        if old.endswith(".dll") and os.path.join(d, old) != dst:
            try:
                os.remove(os.path.join(d, old))
            except OSError:
                pass
    return dst


def _app_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}"'


def _delete_tree(root, path: str):
    import winreg
    try:
        with winreg.OpenKey(root, path) as k:
            subs = []
            i = 0
            while True:
                try:
                    subs.append(winreg.EnumKey(k, i))
                except OSError:
                    break
                i += 1
        for s in subs:
            _delete_tree(root, path + "\\" + s)
        winreg.DeleteKey(root, path)
    except FileNotFoundError:
        pass


def shell_register():
    import winreg
    settings = load_settings()
    if not settings.get("shell_enabled", True):
        shell_unregister()
        return
    H = winreg.HKEY_CURRENT_USER
    for key in LEGACY_SHELL_KEYS:
        _delete_key_tree(key)
    for key in OLD_SHELLEX_KEYS:
        _delete_tree(H, key)
    icon = sys.executable if getattr(sys, "frozen", False) else resource("yzip.ico")
    with winreg.CreateKey(H, APP_KEY) as k:  # read by the shell extension
        winreg.SetValueEx(k, "Command", 0, winreg.REG_SZ, _app_command())
        winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, icon)
        winreg.SetValueEx(k, "ArchiveExts", 0, winreg.REG_SZ,
                          ";".join(e for e in formats.READ_EXTS if e not in formats.IMAGE_EXTS))
        winreg.SetValueEx(k, "ImageExts", 0, winreg.REG_SZ, ";".join(formats.IMAGE_EXTS))
        menu_mask = (int(settings.get("shell_compress", True)) |
                     int(settings.get("shell_extract_here", True)) << 1 |
                     int(settings.get("shell_extract_folder", True)) << 2)
        winreg.SetValueEx(k, "MenuMask", 0, winreg.REG_DWORD, menu_mask)
        winreg.SetValueEx(k, "Language", 0, winreg.REG_SZ, settings.get("language", "ko"))
        winreg.SetValueEx(k, "SeparateArchives", 0, winreg.REG_DWORD,
                          int(settings.get("shell_multi", "combined") == "separate"))
    dll_src = shell_dll_source()
    for key, label, flag in SHELL_VERBS:
        option = {"--archive": "shell_compress", "--extract-here": "shell_extract_here",
                  "--extract-folder": "shell_extract_folder"}.get(flag)
        if dll_src or (option and not settings.get(option, True)):
            # the shell extension shows these items: with the real names, and the extract ones in the menu's
            # extract section (a static verb always lands in the top group)
            _delete_key_tree(key)
            continue
        with winreg.CreateKey(H, key) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, label)
            winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, icon)
        with winreg.CreateKey(H, key + r"\command") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, shell_command(flag))
    if dll_src:
        dll = install_shell_dll(dll_src)
        with winreg.CreateKey(H, rf"Software\Classes\CLSID\{SHELLEX_CLSID}") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "yzip context menu")
        with winreg.CreateKey(H, rf"Software\Classes\CLSID\{SHELLEX_CLSID}\InprocServer32") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, dll)
            winreg.SetValueEx(k, "ThreadingModel", 0, winreg.REG_SZ, "Apartment")
        for key in SHELLEX_KEYS:
            with winreg.CreateKey(H, key) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, SHELLEX_CLSID)


def shell_unregister():
    import winreg
    for key, _, _ in SHELL_VERBS:
        _delete_key_tree(key)
    for key in LEGACY_SHELL_KEYS:
        _delete_key_tree(key)
    for key in SHELLEX_KEYS + OLD_SHELLEX_KEYS:
        _delete_tree(winreg.HKEY_CURRENT_USER, key)
    _delete_tree(winreg.HKEY_CURRENT_USER, rf"Software\Classes\CLSID\{SHELLEX_CLSID}")
    _delete_tree(winreg.HKEY_CURRENT_USER, APP_KEY)


def quick_extract(win: Main, arc: str, folder: bool, into: str | None = None):
    """'여기에 / 폴더에 압축 풀기'. into: the folder the shell extension's menu named ("Kings (2)")."""
    here = os.path.dirname(os.path.abspath(into or arc))
    dest = writable_target(None, here, lambda: ["--extract-folder" if folder else "--extract-here", arc],
                           lambda w: QFileDialog.getExistingDirectory(w, "풀 위치", _desktop()), folder=here)
    if not dest:
        return
    if folder:
        # an existing folder is not mixed into: "Kings (2)", like Bandizip; "b.bin (2)" for b.bin.xz, not "b (2).bin"
        dest = into if into and dest == here and not os.path.exists(into) else unique_dir(
            os.path.join(dest, formats.stem(arc)))
    try:
        z, pw = win._open(arc, None)
    except Exception as e:
        QMessageBox.critical(None, APP, f"열 수 없습니다: {os.path.basename(arc)}\n{e}")
        return
    if z is None:
        return
    win.arc_path, win.password = arc, pw
    with z:
        if not folder:
            dest = smart_dest(z, dest, arc)
    if win._do_extract(dest, None, "rename") and load_settings().get("extract_open_folder", False):
        open_path(dest)


def _created_at(pid: int):
    """Process creation time in 100 ns units, or None."""
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32")
    k.OpenProcess.restype = ctypes.c_void_p
    k.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        c, e, kt, u = (wintypes.FILETIME() for _ in range(4))
        if not k.GetProcessTimes(h, c, e, kt, u):
            return None
        return (c.dwHighDateTime << 32) | c.dwLowDateTime
    finally:
        k.CloseHandle(h)


def _sibling_count():
    """Other live yzip.exe processes started by the same parent (Explorer) within 3 s of this one: the rest of
    one multi-select, possibly still booting. A long-running yzip window from the same Explorer is not counted.
    Frozen exe only; None when it cannot tell (callers then fall back to the plain quiet-period wait)."""
    if not getattr(sys, "frozen", False) or os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class PE(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_wchar * 260)]
        k = ctypes.windll.kernel32
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snap = k.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
        pe = PE()
        pe.dwSize = ctypes.sizeof(PE)
        me, procs = os.getpid(), []
        ok = k.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            procs.append((pe.th32ProcessID, pe.th32ParentProcessID, pe.szExeFile.lower()))
            ok = k.Process32NextW(snap, ctypes.byref(pe))
        k.CloseHandle(snap)
        mine = next((p for p in procs if p[0] == me), None)
        t0 = _created_at(me)
        if not mine or t0 is None:
            return None
        n = 0
        for pid, ppid, exe in procs:
            if pid != me and ppid == mine[1] and exe == mine[2]:
                t = _created_at(pid)
                if t is not None and abs(t - t0) <= 30_000_000:  # 3 s
                    n += 1
        return n
    except Exception:
        return None


def _claim_mutex(name: str):
    """Handle if this process created the named mutex first, else None."""
    import ctypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.restype = ctypes.c_void_p
    h = k.CreateMutexW(None, False, name)
    if not h:
        return None
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        k.CloseHandle(ctypes.c_void_p(h))
        return None
    return h


def _release_mutex(h):
    if h:
        import ctypes
        ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(h))


def gather_paths(verb: str, paths: list[str], first_wait_ms: int = 1500, quiet_ms: int = 1000,
                 max_ms: int = 10000):
    """Explorer starts one process per selected item for a "%1" verb. The first process becomes the collector:
    it listens on a local socket until no new paths arrive for quiet_ms. Later processes forward their paths
    and return None (they should exit). Returns the combined, sorted path list for the collector."""
    import getpass
    import json
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    scale = float(os.environ.get("YZIP_GATHER_SCALE", "1"))  # tests shrink the waits
    first_wait_ms, quiet_ms, max_ms = first_wait_ms * scale, quiet_ms * scale, max_ms * scale
    name =f"yzip-{verb}-{getpass.getuser()}"
    _dbg = os.environ.get("YZIP_DEBUG_GATHER")
    if _dbg:
        open(_dbg, "a").write(f"{time.time():.3f} pid{os.getpid()} start {paths}\n")
    def forward() -> bool:
        s = QLocalSocket()
        s.connectToServer(name)
        if not s.waitForConnected(200):
            return False
        s.write(json.dumps(paths).encode("utf-8") + b"\n")
        s.flush()
        s.waitForBytesWritten(2000)
        s.disconnectFromServer()
        return True

    # Election: a named mutex, because two QLocalServers can both listen() on one Windows pipe name.
    mutex = None
    for _ in range(60):
        if forward():
            return None
        mutex = _claim_mutex("Local\\" + name)
        if mutex:
            break
        time.sleep(0.1)  # the collector holds the mutex but is not listening yet
    QLocalServer.removeServer(name)
    server = QLocalServer()
    if not mutex or not server.listen(name):
        _release_mutex(mutex)
        return list(paths)  # cannot coordinate; handle just our own path
    collected = list(paths)
    clients = []
    last = {"t": time.time()}

    def pump():
        while server.hasPendingConnections():
            clients.append(server.nextPendingConnection())
        for c in clients:
            while c.canReadLine():
                try:
                    collected.extend(json.loads(bytes(c.readLine()).decode("utf-8")))
                    last["t"] = time.time()
                except ValueError:
                    pass
    began = time.time()
    while True:
        QApplication.processEvents()
        pump()
        now = time.time()
        waited = (now - last["t"]) * 1000
        if (now - began) * 1000 >= max_ms:
            break
        # Explorer creates all N processes at once, so once ours is up the others already exist: with no
        # sibling yzip.exe alive there is nobody to wait for (a single item no longer costs the full wait)
        if scale >= 1 and (now - began) * 1000 >= 400 and waited >= 200 and _sibling_count() == 0:
            break
        if waited >= quiet_ms and (now - began) * 1000 >= first_wait_ms:
            if scale < 1 or not _sibling_count():  # stragglers still starting up? keep listening
                break
            last["t"] = now  # re-check after another quiet period
        time.sleep(0.02)
    pump()
    server.close()
    _release_mutex(mutex)
    if _dbg:
        open(_dbg, "a").write(f"{time.time():.3f} pid{os.getpid()} collector done {collected} siblings={_sibling_count()}\n")
    return sorted(set(collected), key=str.lower)


def _app():
    app = QApplication.instance() or QApplication(sys.argv)
    appcore.apply_runtime_settings(strict=False)
    appearance.apply_appearance(app)
    if os.path.exists(resource("yzip.ico")):
        from PySide6.QtGui import QIcon
        app.setWindowIcon(QIcon(resource("yzip.ico")))
    sys.excepthook = lambda t, e, tb: (log_exception(e), sys.__excepthook__(t, e, tb))
    import threading
    threading.excepthook = lambda a: log_exception(a.exc_value)
    return app


def attach_job(job) -> int:
    """The progress window for a job yzip_main started before loading Qt."""
    _app()
    ok, r = run_job(None, job.title, None, reveal=job.reveal, done_text=job.done_text, job=job)
    if not ok:
        kind, msg = r if r else ("error", "알 수 없는 오류")
        if kind == "password":
            QMessageBox.warning(None, APP, "암호가 틀렸습니다.")
        elif kind != "cancel":
            QMessageBox.critical(None, APP, msg)
    elif r and load_settings().get("compress_open_folder", False):
        open_path(os.path.dirname(os.path.abspath(r[0])))
    return 0


def main():
    args = sys.argv[1:]
    if args[:1] in (["--register-shell"], ["--unregister-shell"]):  # UI-free, for scripts and installers
        (shell_register if args[0] == "--register-shell" else shell_unregister)()
        return 0
    if args[:1] == ["--auto-update"] and len(args) > 1:  # installer: the user's choice on the tasks page
        save_setting("auto_update", args[1].lower() in ("on", "1", "true"))
        return 0
    if args[:1] in (["--associate"], ["--unassociate"], ["--unregister-all"]):  # installer / uninstaller
        import assoc
        if args[0] == "--associate":
            assoc.register()
        else:
            assoc.unregister()
            if args[0] == "--unregister-all":
                shell_unregister()
        return 0
    if args[:1] == ["--updated"] and os.environ.get("YZIP_UPDATE_TEST_MARKER"):
        from version import __version__  # update test: prove the new version started, without a window
        with open(os.environ["YZIP_UPDATE_TEST_MARKER"], "w", encoding="utf-8") as f:
            f.write(__version__)
        return 0
    if args[:1] == ["--update-now"]:  # unattended update: check, download, verify, run the installer
        import updater
        try:
            rel = updater.check()
            if rel:
                updater.launch_installer(updater.download(rel))
        except Exception as e:
            log_exception(e)
            return 1
        return 0
    app = _app()
    win = Main()
    if args[:1] == ["--updated"]:
        from version import __version__
        win.show()
        win.status.show()
        win.status.showMessage(f"yzip {__version__}(으)로 업데이트했습니다.", 10000)
        return app.exec()
    if args[:1] in (["--archive-list"], ["--compress-dialog-list"]) and len(args) >= 2:
        # from the shell extension: every selected item in one UTF-8 list file.
        # --archive-list LIST --out ZIP: compress at once to the name the menu showed (the exe normally takes
        # this one in yzip_main, before Qt loads).
        # --compress-dialog-list LIST: "yzip으로 압축하기...", the compress window (format, password, ...) only.
        paths = read_list(args[1])
        if paths and args[0] == "--archive-list" and len(args) >= 4 and args[2] == "--out":
            win.new_archive(paths, standalone=True, out=args[3])
        elif paths and args[0] == "--compress-dialog-list":
            win.new_archive(paths, standalone=True, ask=True)
        return 0
    if args[:1] == ["--extract-list"] and len(args) >= 3 and args[1] in ("here", "folder"):
        # from the shell extension: --extract-list here|folder LIST [--out DIR]; DIR is the folder its menu
        # named ("Kings (2)\에 압축 풀기"), given for a single archive only
        paths = []
        for p in read_list(args[2]):  # several volumes of one set (x.part1.rar, x.part2.rar): extract it once
            if formats.first_volume(p).lower() not in [formats.first_volume(q).lower() for q in paths]:
                paths.append(p)
        into = args[4] if len(args) >= 5 and args[3] == "--out" and len(paths) == 1 else None
        for arc in paths:
            quick_extract(win, arc, args[1] == "folder", into)
        return 0
    if args and args[0] == "--archive" and len(args) > 1:
        paths = gather_paths("archive", args[1:])
        if paths is None:
            return 0
        win.new_archive(paths, standalone=True)
        return 0
    elif args and args[0] in ("--extract-here", "--extract-folder") and len(args) > 1:
        paths = gather_paths(args[0].lstrip("-"), args[1:])
        if paths is None:
            return 0
        for arc in paths:
            quick_extract(win, arc, args[0] == "--extract-folder")
        return 0
    elif args:
        win.show()
        if win.open_archive(args[0]):
            win.start_update_check()
    else:
        win.show()
        win.start_update_check()
    if os.environ.get("YZIP_AUTOACCEPT"):
        QTimer.singleShot(500, app.quit)
    return app.exec()


def finish(code: int):
    """Drop Qt objects deterministically, then leave without interpreter teardown. Exiting with deleteLater'd
    dialogs still pending made Qt access freed memory during process shutdown (0xC0000005) after delete/extract."""
    import gc
    from PySide6.QtCore import QCoreApplication, QEvent
    app = QApplication.instance()
    if app is not None:
        app.processEvents()
        # processEvents() outside a running loop skips DeferredDelete, so flush those explicitly
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    gc.collect()
    sys.stdout and sys.stdout.flush()
    sys.stderr and sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    finish(main() or 0)
