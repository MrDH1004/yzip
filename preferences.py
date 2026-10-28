"""Section-based preferences; the caller applies an accepted dialog."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import sys
import threading
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QTabWidget,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

import appcore
import assoc
import formats
import updater
from ui_theme import PREFERENCES_QSS
from version import REPO, __version__


# Only these editable preferences cross the import/export boundary. Window state,
# update history and file associations belong to this installation.
PREFERENCE_DEFAULTS = dict(appcore.DEFAULTS)


class PreferencesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.assoc = assoc
        self.setObjectName("settingsDialog")
        self.setStyleSheet(PREFERENCES_QSS)
        self.setWindowTitle("환경설정")
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.resize(920, 650)
        self.setMinimumSize(760, 560)
        self.controls = {}
        self._association_initial = set()
        self._association_items = {}
        self._association_ready = False
        self._update_queue = queue.Queue()
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(80)
        self._update_timer.timeout.connect(self._poll_update)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(16)
        self.title = QLabel("환경설정")
        self.title.setObjectName("settingsTitle")
        self.subtitle = QLabel("yzip의 작업 방식과 사용 환경을 설정하세요.")
        self.subtitle.setObjectName("settingsSubtitle")
        self.subtitle.setWordWrap(True)
        header = QVBoxLayout()
        header.setSpacing(4)
        header.addWidget(self.title)
        header.addWidget(self.subtitle)
        layout.addLayout(header)

        body = QWidget()
        body.setObjectName("settingsBody")
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(16)
        self.nav = QListWidget()
        self.nav.setObjectName("settingsNav")
        self.nav.setAccessibleName("설정 섹션")
        self.nav.setMinimumWidth(156)
        self.nav.setMaximumWidth(180)
        self.nav.setSpacing(3)
        body_layout.addWidget(self.nav)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("settingsPages")
        self.tabs.tabBar().hide()
        body_layout.addWidget(self.tabs, 1)
        layout.addWidget(body, 1)
        self._general_tab()
        self._association_tab()
        self._shell_tab()
        self._compress_tab()
        self._extract_tab()
        self._performance_tab()
        self._update_tab()
        self._management_tab()
        self.nav.currentRowChanged.connect(self.tabs.setCurrentIndex)
        self.tabs.currentChanged.connect(self.nav.setCurrentRow)
        self.nav.setCurrentRow(0)
        layout.addWidget(self._separator())
        footer = QHBoxLayout()
        footer.setSpacing(16)
        note = QLabel("변경한 설정은 저장을 누르면 적용됩니다.")
        note.setObjectName("settingsFooterHint")
        note.setWordWrap(True)
        footer.addWidget(note, 1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                       QDialogButtonBox.StandardButton.Cancel)
        self.buttons.setObjectName("settingsButtons")
        save = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        save.setText("저장")
        save.setObjectName("primaryButton")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        footer.addWidget(self.buttons)
        layout.addLayout(footer)
        # All buttons are parented now; page actions must not take save's default.
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)
        save.setDefault(True)
        self._set_values(appcore.load_settings())
        self._load_associations()
        self._show_update_history(appcore.load_settings())

    @staticmethod
    def _separator():
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setObjectName("settingsDivider")
        return line

    def _tab(self, title, hint):
        # Keep a regular page at tabs.widget(index): callers search its children.
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("settingsScroll")
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        content = QWidget()
        content.setObjectName("settingsPage")
        form = QFormLayout(content)
        form.setContentsMargins(4, 4, 16, 20)
        form.setHorizontalSpacing(24)
        form.setVerticalSpacing(16)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        heading = QLabel(title)
        heading.setObjectName("settingsSectionTitle")
        form.addRow(heading)
        self._hint(form, hint)
        form.addRow(self._separator())
        scroll.setWidget(content)
        layout.addWidget(scroll)
        self.tabs.addTab(page, title)
        self.nav.addItem(title)
        return form

    def _control(self, key, widget):
        self.controls[key] = widget
        widget.setObjectName(key)
        return widget

    def _combo(self, form, key, label, options):
        widget = self._control(key, QComboBox())
        for text, value in options:
            widget.addItem(text, value)
        widget.setAccessibleName(label)
        form.addRow(label, widget)
        return widget

    def _check(self, form, key, label):
        widget = self._control(key, QCheckBox(label))
        widget.setAccessibleName(label)
        form.addRow(widget)
        return widget

    def _spin(self, form, key, label, minimum, maximum):
        widget = self._control(key, QSpinBox())
        widget.setRange(minimum, maximum)
        widget.setAccessibleName(label)
        form.addRow(label, widget)
        return widget

    def _path(self, form, key, label):
        row = QHBoxLayout()
        edit = self._control(key, QLineEdit())
        edit.setAccessibleName(label)
        edit.setPlaceholderText("비워 두면 기본 위치 사용")
        row.addWidget(edit)
        button = QPushButton("찾아보기…")
        button.setAccessibleName(label + " 찾아보기")
        button.clicked.connect(lambda: self._browse_folder(edit))
        row.addWidget(button)
        form.addRow(label, row)

    def _browse_folder(self, edit):
        path = QFileDialog.getExistingDirectory(self, "폴더 선택", edit.text())
        if path:
            edit.setText(path)

    def _hint(self, form, text):
        label = QLabel(text)
        label.setObjectName("settingsSectionHint")
        label.setWordWrap(True)
        form.addRow(label)

    def _button(self, form, text, callback):
        button = QPushButton(text)
        button.clicked.connect(callback)
        form.addRow(button)
        return button

    def _general_tab(self):
        form = self._tab("일반", "언어와 화면 표시를 원하는 방식으로 맞추세요.")
        self._combo(form, "language", "언어", [("한국어", "ko"), ("English", "en")])
        self._combo(form, "theme", "테마", [("시스템 설정", "system"), ("밝게", "light"), ("어둡게", "dark")])
        font = self._control("font_family", QLineEdit())
        font.setPlaceholderText("시스템 기본 글꼴")
        font.setAccessibleName("글꼴 이름")
        form.addRow("글꼴 이름", font)
        self._spin(form, "font_size", "글꼴 크기 (pt)", 1, 96)

    def _association_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.tabs.addTab(page, "파일연결")
        self.nav.addItem("파일연결")
        self.assoc_list = QTreeWidget()
        self.assoc_list.setObjectName("settingsAssocList")
        self.assoc_list.setAccessibleName("연결 가능한 파일 형식 목록")
        self.assoc_list.setToolTip("체크는 yzip 연결 등록 여부입니다. 기본 앱 변경은 Windows 기본 앱 설정을 이용하세요. EXE는 연결 대상에서 제외됩니다.")
        self.assoc_list.setHeaderLabels(["확장자", "파일 형식", "현재 기본 앱"])
        self.assoc_list.setRootIsDecorated(False)
        self.assoc_list.setUniformRowHeights(True)
        self.assoc_list.setColumnWidth(0, 95)
        self.assoc_list.setColumnWidth(1, 235)
        for ext, label in formats.SUPPORTED_TYPES.items():
            item = QTreeWidgetItem([ext, label, ""])
            if ext in assoc.TYPES and ext != ".exe":
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Unchecked)
                self._association_items[ext] = item
            else:
                item.setFlags(item.flags() & ~(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable))
                item.setText(2, "지원(연결 제외)")
            self.assoc_list.addTopLevelItem(item)
        layout.addWidget(self.assoc_list, 1)
        row = QHBoxLayout()
        row.setSpacing(6)
        self._association_selection_buttons = []
        for title, selected in (("전체 선택", True), ("전체 해제", False)):
            button = QPushButton(title)
            button.clicked.connect(lambda checked=False, selected=selected: self._select_associations(selected))
            row.addWidget(button)
            self._association_selection_buttons.append(button)
        button = QPushButton("Windows 기본 앱 열기")
        button.clicked.connect(lambda: self._guard("기본 앱 설정", assoc.open_default_apps))
        row.addWidget(button)
        layout.addLayout(row)

    def _load_associations(self):
        try:
            current = assoc.status(list(self._association_items))
            registered = {ext for ext in self._association_items if assoc.enabled([ext])}
            self._association_initial = registered
            self.assoc_list.blockSignals(True)
            try:
                for ext, item in self._association_items.items():
                    item.setCheckState(0, Qt.CheckState.Checked if ext in registered else Qt.CheckState.Unchecked)
                    is_yzip, name = current.get(ext, (False, "없음"))
                    item.setText(2, "yzip" if is_yzip else name or "없음")
            finally:
                self.assoc_list.blockSignals(False)
            self._association_ready = True
        except Exception as exc:
            for button in self._association_selection_buttons:
                button.setEnabled(False)
            self.assoc_list.setEnabled(False)
            self._error("파일연결 조회", exc)

    def _select_associations(self, selected):
        if not self._association_ready:
            return
        self.assoc_list.blockSignals(True)
        try:
            for item in self._association_items.values():
                item.setCheckState(0, Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked)
        finally:
            self.assoc_list.blockSignals(False)

    def _shell_tab(self):
        form = self._tab("탐색기", "Windows 탐색기에서 사용할 우클릭 메뉴를 선택하세요.")
        master = self._check(form, "shell_enabled", "탐색기 우클릭 메뉴 사용")
        children = [self._check(form, "shell_compress", "압축하기"),
                    self._check(form, "shell_extract_here", "여기에 풀기"),
                    self._check(form, "shell_extract_folder", "폴더에 풀기")]
        multi = self._combo(form, "shell_multi", "여러 파일 선택 시",
                            [("하나로 모아서 처리", "combined"), ("파일별로 처리", "separate")])
        master.toggled.connect(lambda on: [widget.setEnabled(on) for widget in children + [multi]])

    def _compress_tab(self):
        form = self._tab("압축", "새 압축 파일을 만들 때 사용할 기본값입니다.")
        fmt = self._combo(form, "compress_format", "기본 형식", [(f.label, f.id) for f in formats.FORMATS])
        self._spin(form, "compress_level", "압축 수준 (0: 저장, 9: 최대)", 0, 9)
        self._path(form, "compress_dir", "저장 폴더")
        split = self._combo(form, "compress_split", "분할 크기", [(s, s) for s in
                            ("분할 안 함", "100MB", "650MB", "700MB", "1440KB", "4GB")])
        split.setEditable(True)
        self._hint(form, "분할 크기는 직접 입력할 수 있습니다. 예: 250MB (최소 64KB)")
        fmt.currentIndexChanged.connect(lambda: split.setEnabled(bool(
            formats.BY_ID.get(fmt.currentData()) and formats.BY_ID[fmt.currentData()].split)))
        self._check(form, "compress_open_folder", "압축 후 저장 폴더 열기")

    def _extract_tab(self):
        form = self._tab("해제", "압축을 풀 위치와 같은 이름의 파일 처리 방식을 설정하세요.")
        self._path(form, "extract_dir", "해제 폴더")
        self._check(form, "extract_smart", "알아서 풀기 (내용에 맞춰 하위 폴더 생성)")
        self._combo(form, "extract_mode", "같은 이름의 파일", [("물어보기", "ask"),
                    ("덮어쓰기", "overwrite"), ("건너뛰기", "skip"), ("이름 변경", "rename")])
        self._check(form, "extract_open_folder", "해제 후 폴더 열기")

    def _performance_tab(self):
        form = self._tab("성능", "작업에 사용할 시스템 자원과 임시 저장 위치를 관리하세요.")
        self._check(form, "low_priority", "낮은 우선순위로 작업")
        threads = self._spin(form, "worker_threads", "작업 스레드 수", 0, 256)
        threads.setSpecialValueText("자동")
        self._path(form, "temp_dir", "임시 폴더")
        self._button(form, "yzip 임시 파일 정리…", self._cleanup_temp)
        self._hint(form, "정리 전에 대상 파일과 폴더를 확인할 수 있습니다.")

    def _update_tab(self):
        form = self._tab("업데이트", "새 버전을 확인하고 최근 확인 결과를 살펴보세요.")
        form.addRow("현재 버전", QLabel(__version__))
        self.auto_update_check = self._check(form, "auto_update", "업데이트 자동 확인")
        self.check_update_button = self._button(form, "지금 업데이트 확인", self._check_update)
        self.last_checked_label = QLabel()
        self.last_result_label = QLabel()
        self.last_result_label.setWordWrap(True)
        self.last_result_label.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow("마지막 확인", self.last_checked_label)
        form.addRow("마지막 결과", self.last_result_label)

    def _management_tab(self):
        form = self._tab("설정관리", "설정을 백업하거나 복원하고 프로그램 정보를 확인하세요.")
        self._button(form, "설정 초기화…", self._reset)
        self._button(form, "설정 가져오기 (JSON)…", self._import_settings)
        self._button(form, "설정 내보내기 (JSON)…", self._export_settings)
        self._hint(form, "초기화와 가져오기는 저장 버튼을 눌러야 저장됩니다. 파일연결과 업데이트 기록은 포함하지 않습니다.")
        form.addRow(self._separator())
        self._button(form, "오류 로그 폴더 열기", self._open_log_folder)
        self._button(form, "yzip 정보", self._about)
        self._button(form, "라이선스 보기", self._licenses)

    def _set_values(self, values):
        for key, default in PREFERENCE_DEFAULTS.items():
            value = values.get(key, default)
            widget = self.controls[key]
            if isinstance(widget, QCheckBox):
                widget.setChecked(value)
            elif isinstance(widget, QSpinBox):
                widget.setValue(value)
            elif isinstance(widget, QComboBox):
                index = widget.findData(value)
                if widget.isEditable():
                    widget.setCurrentText(value)
                else:
                    widget.setCurrentIndex(max(0, index))
            else:
                widget.setText(value)
        enabled = self.controls["shell_enabled"].isChecked()
        for key in ("shell_compress", "shell_extract_here", "shell_extract_folder", "shell_multi"):
            self.controls[key].setEnabled(enabled)
        fmt = formats.BY_ID.get(self.controls["compress_format"].currentData())
        self.controls["compress_split"].setEnabled(bool(fmt and fmt.split))

    def values(self):
        values = {}
        for key, widget in self.controls.items():
            if isinstance(widget, QCheckBox):
                value = widget.isChecked()
            elif isinstance(widget, QSpinBox):
                value = widget.value()
            elif isinstance(widget, QComboBox):
                value = widget.currentText().strip() if widget.isEditable() else widget.currentData()
            else:
                value = widget.text().strip()
            values[key] = value
        return values

    def apply(self):
        """Persist preferences and association differences; shell sync is the caller's job.

        False means an error was already displayed to the user. Do not report a
        successful apply or synchronize shell settings in that case.
        """
        try:
            values = appcore.normalize_settings(self.values(), strict=True)
            # Probe the proposed workspace without publishing runtime settings.
            # A failed probe must leave both JSON and associations untouched.
            appcore.validate_runtime_settings(values)
            result = appcore.save_settings({key: values[key] for key in PREFERENCE_DEFAULTS})
            if result is False:
                raise OSError("설정을 저장하지 못했습니다.")
            if self._association_ready:
                selected = {ext for ext, item in self._association_items.items()
                            if item.checkState(0) == Qt.CheckState.Checked}
                added = sorted(selected - self._association_initial)
                removed = sorted(self._association_initial - selected)
                if added:
                    assoc.register(added)
                    self._association_initial.update(added)
                if removed:
                    assoc.unregister(removed)
                    self._association_initial.difference_update(removed)
            return True
        except Exception as exc:
            self._error("설정 적용", exc)
            return False

    def accept(self):
        try:
            appcore.normalize_settings(self.values(), strict=True)
        except (ValueError, TypeError) as exc:
            self._error("설정 확인", exc)
            return
        super().accept()

    def _error(self, title, exc):
        appcore.log_exception(exc)
        QMessageBox.critical(self, title, str(exc))

    def _guard(self, title, callback):
        try:
            return callback()
        except Exception as exc:
            self._error(title, exc)
            return None

    def _reset(self):
        if QMessageBox.question(self, "설정 초기화", "환경설정을 기본값으로 되돌릴까요?\n저장을 눌러야 저장됩니다.",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self._set_values(appcore.normalize_settings(dict(PREFERENCE_DEFAULTS)))

    def import_file(self, path):
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError("설정 JSON은 객체 형식이어야 합니다.")
        unknown = sorted(set(data) - set(PREFERENCE_DEFAULTS))
        if unknown:
            raise ValueError("허용되지 않는 설정 키: " + ", ".join(unknown))
        # Core owns the schema; reject values it would coerce or silently reset.
        normalized = appcore.normalize_settings(data, strict=True)
        for key, value in data.items():
            if type(value) is not type(PREFERENCE_DEFAULTS[key]) or normalized.get(key) != value:
                raise ValueError(f"설정 값이 올바르지 않습니다: {key}")
        values = self.values()
        values.update(data)
        self._set_values(values)

    def export_file(self, path):
        normalized = appcore.normalize_settings(self.values(), strict=True)
        with open(path, "w", encoding="utf-8") as stream:
            json.dump({key: normalized[key] for key in PREFERENCE_DEFAULTS}, stream,
                      ensure_ascii=False, indent=2)
            stream.write("\n")

    def _import_settings(self):
        path, _ = QFileDialog.getOpenFileName(self, "설정 가져오기", "", "JSON (*.json)")
        if path:
            self._guard("설정 가져오기", lambda: self.import_file(path))

    def _export_settings(self):
        path, _ = QFileDialog.getSaveFileName(self, "설정 내보내기", "yzip-settings.json", "JSON (*.json)")
        if path:
            self._guard("설정 내보내기", lambda: self.export_file(path))

    def _show_update_history(self, settings):
        checked = settings.get("update_last_checked")
        if type(checked) in (int, float):
            try:
                checked = datetime.fromtimestamp(checked).astimezone().isoformat(timespec="seconds")
            except (ValueError, OverflowError, OSError):
                checked = str(checked)
        self.last_checked_label.setText(str(checked) if checked else "확인 기록 없음")
        result = settings.get("update_last_result")
        self.last_result_label.setText(str(result) if result else "확인 기록 없음")

    def _check_update(self):
        if self._update_timer.isActive():
            return
        self.check_update_button.setEnabled(False)
        self.last_result_label.setText("확인 중…")
        results = self._update_queue

        def work():
            try:
                results.put((updater.check(), None))
            except Exception as exc:
                results.put((None, exc))

        threading.Thread(target=work, daemon=True, name="yzip-preferences-update").start()
        self._update_timer.start()

    def _poll_update(self):
        try:
            rel, error = self._update_queue.get_nowait()
        except queue.Empty:
            return
        self._update_timer.stop()
        self.check_update_button.setEnabled(True)
        checked = datetime.now().astimezone().isoformat(timespec="seconds")
        result = (f"확인 실패: {error}" if error is not None else
                  f"새 버전 {rel.version}" if rel is not None else "최신 버전입니다.")
        history = {"update_last_checked": checked, "update_last_result": result}
        self._show_update_history(history)

        def save_history():
            if appcore.save_settings(history) is False:
                raise OSError("업데이트 확인 기록을 저장하지 못했습니다.")

        self._guard("업데이트 확인 기록", save_history)
        if error is not None:
            self._error("업데이트 확인", error)
        elif rel is not None:
            parent = self.parentWidget()
            callback = getattr(parent, "_update_result", None)
            if callback is not None:
                self._guard("업데이트", lambda: callback(rel, None, False))
            else:
                QMessageBox.information(self, "업데이트", f"새 버전 yzip {rel.version}이 있습니다.\n"
                                        "메인 창의 업데이트 메뉴에서 설치할 수 있습니다.")

    def _cleanup_temp(self):
        self._guard("임시 파일 정리", self._confirm_cleanup)

    def _confirm_cleanup(self):
        candidates = appcore.cleanup_temp(dry_run=True)
        if not candidates:
            QMessageBox.information(self, "임시 파일 정리", "정리할 yzip 임시 파일이 없습니다.")
            return
        box = QMessageBox(self)
        box.setWindowTitle("임시 파일 정리")
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(f"다음 yzip 임시 파일/폴더 {len(candidates)}개를 삭제할까요?")
        box.setInformativeText("자세히 보기를 눌러 전체 경로를 확인하세요.")
        box.setDetailedText("\n".join(str(path) for path in candidates))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() == QMessageBox.StandardButton.Yes:
            result = appcore.cleanup_temp(paths=candidates)
            message = (f"삭제한 폴더: {result['removed']}개\n"
                       f"건너뛴 폴더: {result['skipped']}개\n오류: {result['errors']}개")
            if result["errors"]:
                QMessageBox.warning(self, "임시 파일 정리", message)
            else:
                QMessageBox.information(self, "임시 파일 정리", message)

    def _open_log_folder(self):
        def open_folder():
            path = os.path.dirname(appcore.log_path())
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
                raise OSError("오류 로그 폴더를 열지 못했습니다.")
        self._guard("오류 로그 폴더", open_folder)

    def _about(self):
        QMessageBox.about(self, "yzip 정보", f"<b>yzip {__version__}</b><br>가볍고 빠른 압축 프로그램<br>"
                          f"<a href='https://github.com/{REPO}'>github.com/{REPO}</a><br>MIT License")

    def _licenses(self):
        self._guard("라이선스", self._show_licenses)

    def _show_licenses(self):
        root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        paths = [root / "LICENSE"]
        folder = root / "tools" / "licenses"
        if folder.is_dir():
            paths.extend(sorted(path for path in folder.iterdir() if path.is_file()))
        sections = []
        for path in paths:
            if path.is_file():
                sections.append(path.name + "\n\n" + path.read_text(encoding="utf-8", errors="replace"))
        if not sections:
            raise FileNotFoundError("라이선스 파일을 찾을 수 없습니다.")
        dialog = QDialog(self)
        dialog.setObjectName("settingsDialog")
        dialog.setStyleSheet(PREFERENCES_QSS)
        dialog.setWindowTitle("yzip 및 포함 소프트웨어 라이선스")
        dialog.resize(700, 500)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(16)
        title = QLabel("라이선스")
        title.setObjectName("settingsTitle")
        layout.addWidget(title)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText("\n\n".join(sections))
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()
