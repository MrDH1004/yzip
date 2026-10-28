"""Offscreen appearance regressions; no real input or user settings are used."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QFontDatabase, QPalette
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QLabel, QLineEdit,
                               QPushButton, QStatusBar, QTabWidget, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from appearance import apply_appearance, translate_text


class AppearanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        # Windows' offscreen plugin does not enumerate installed fonts. Load
        # real fonts explicitly for reviewable snapshots; production is untouched.
        if not QFontDatabase.families():
            font_folder = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
            for name in ("arial.ttf", "malgun.ttf"):
                path = font_folder / name
                if path.is_file():
                    QFontDatabase.addApplicationFont(str(path))
        # Import before per-test settings mocks so GUI's imported load_settings
        # reference never outlives a temporary mock.
        import yzip_gui  # noqa: F401

    def setUp(self):
        self.settings = {"language": "ko", "theme": "light", "font_family": "Arial", "font_size": 10}
        self.mock_settings = patch("appcore.load_settings", side_effect=lambda: dict(self.settings))
        self.mock_settings.start()
        self.controller = apply_appearance(self.app)
        self.windows = []

    def tearDown(self):
        for widget in self.windows:
            widget.close()
            widget.deleteLater()
        self.app.processEvents()
        self.mock_settings.stop()

    def window(self):
        widget = QWidget()
        self.windows.append(widget)
        widget.setLayout(QVBoxLayout())
        return widget

    def apply(self, **values):
        self.settings.update(values)
        apply_appearance(self.app)
        self.app.processEvents()

    def test_default_korean_and_roundtrip(self):
        window = self.window()
        window.setWindowTitle("환경설정")
        button = QPushButton("  압축 파일 열기", window)
        window.layout().addWidget(button)
        self.controller.refresh()
        self.assertEqual(button.text(), "  압축 파일 열기")
        self.apply(language="en")
        self.assertEqual(button.text(), "  Open archive")
        self.assertEqual(window.windowTitle(), "Preferences")
        self.apply(language="ko")
        self.assertEqual(button.text(), "  압축 파일 열기")
        self.assertEqual(window.windowTitle(), "환경설정")

    def test_user_values_and_tree_contents_are_opaque(self):
        window = self.window()
        entry = QLineEdit("환경설정", window)
        editable = QComboBox(window)
        editable.setEditable(True)
        editable.addItems(["분할 안 함", "환경설정"])
        editable.setEditText("압축 풀기")
        tree = QTreeWidget(window)
        tree.setHeaderLabels(["이름", "크기"])
        tree.addTopLevelItem(QTreeWidgetItem(["환경설정", "압축 풀기"]))
        filename = QLabel("환경설정", window)
        filename.setObjectName("archName")
        for widget in (entry, editable, tree, filename):
            window.layout().addWidget(widget)
        self.apply(language="en")
        self.assertEqual(entry.text(), "환경설정")
        self.assertEqual(editable.currentText(), "압축 풀기")
        self.assertEqual(editable.itemText(0), "분할 안 함")
        self.assertEqual(tree.topLevelItem(0).text(0), "환경설정")
        self.assertEqual(tree.topLevelItem(0).text(1), "압축 풀기")
        self.assertEqual(filename.text(), "환경설정")
        self.assertEqual(tree.headerItem().text(0), "Name")
        self.apply(language="ko")
        self.assertEqual(tree.headerItem().text(0), "이름")

    def test_combo_data_signals_tabs_actions(self):
        window = self.window()
        combo = QComboBox(window)
        combo.addItem("분할 안 함", "분할 안 함")
        combo.addItem("밝게", "light")
        changed = []
        combo.currentTextChanged.connect(changed.append)
        tabs = QTabWidget(window)
        tabs.addTab(QWidget(), "일반")
        action = QAction("업데이트 확인", window)
        window.addAction(action)
        window.layout().addWidget(combo)
        window.layout().addWidget(tabs)
        self.apply(language="en")
        self.assertEqual(combo.currentText(), "Do not split")
        self.assertEqual(combo.currentData(), "분할 안 함")
        self.assertEqual(changed, [])
        self.assertEqual(tabs.tabText(0), "General")
        self.assertEqual(action.text(), "Check for updates")
        self.apply(language="ko")
        self.assertEqual(combo.currentText(), "분할 안 함")

    def test_dynamic_text_updates_restore_latest_source(self):
        window = self.window()
        label = QLabel("선택한 12개 항목만", window)
        window.layout().addWidget(label)
        self.apply(language="en")
        self.assertEqual(label.text(), "Only the selected 12 items")
        label.setText("선택한 23개 항목만")
        self.controller.refresh()
        self.assertEqual(label.text(), "Only the selected 23 items")
        self.apply(language="ko")
        self.assertEqual(label.text(), "선택한 23개 항목만")

    def test_styles_fonts_rich_text_and_roundtrip(self):
        window = self.window()
        source = "QWidget { background: #ffffff; color: #1f2328; font-family: 'Segoe UI'; font-size: 9.5pt; }"
        window.setStyleSheet(source)
        label = QLabel("<div style='font-size:9pt; color:#1f2328'>작업을 완료했습니다.</div>", window)
        window.layout().addWidget(label)
        self.apply(language="en", theme="dark", font_family="Arial", font_size=14)
        self.assertIn("background: #20242b", window.styleSheet())
        self.assertIn("color: #e6edf3", window.styleSheet())
        self.assertIn("font-size: 13.3pt", window.styleSheet())
        self.assertIn("font-family: 'Arial'", window.styleSheet())
        self.assertIn("Operation completed.", label.text())
        self.assertIn("#e6edf3", label.text())
        self.assertAlmostEqual(label.font().pointSizeF(), 13.3)
        self.assertEqual(self.app.palette().color(QPalette.Window).name(), "#20242b")
        self.apply(language="ko", theme="light", font_size=10)
        self.assertIn("background: #ffffff", window.styleSheet())
        self.assertIn("color:#1f2328", label.text())
        self.assertIn("작업을 완료했습니다.", label.text())
        self.assertNotIn("14pt", window.styleSheet())
        # Styles written after initial application become the new reversible source.
        label.setStyleSheet("color: #57606a; font-size: 8pt;")
        self.apply(theme="dark")
        self.assertIn("#b1bac4", label.styleSheet())
        self.apply(theme="light")
        self.assertIn("#57606a", label.styleSheet())

    def test_show_new_dialog_applies_before_paint(self):
        self.apply(language="en", theme="dark", font_size=13)
        dialog = QDialog()
        self.windows.append(dialog)
        dialog.setWindowTitle("압축 풀기")
        layout = QVBoxLayout(dialog)
        label = QLabel("풀 위치")
        layout.addWidget(label)
        dialog.show()
        self.app.processEvents()
        self.assertEqual(dialog.windowTitle(), "Extract archive")
        self.assertEqual(label.text(), "Destination")
        self.assertEqual(label.font().pointSizeF(), 13)

    def test_font_scaling_preserves_heading_and_helper_hierarchy(self):
        window = self.window()
        window.setStyleSheet("QLabel#title { font-size: 22pt; } QLabel#hint { font-size: 9pt; }")
        self.apply(font_size=15)
        self.assertIn("font-size: 33pt", window.styleSheet())
        self.assertIn("font-size: 13.5pt", window.styleSheet())
        self.apply(font_size=10)
        self.assertIn("font-size: 22pt", window.styleSheet())
        self.assertIn("font-size: 9pt", window.styleSheet())

    def test_dark_accent_text_and_button_background_have_separate_colors(self):
        window = self.window()
        window.setStyleSheet("QLabel { color: #16776A; } QPushButton { background: #16776A; color: #FFFFFF; }")
        self.apply(theme="dark")
        self.assertIn("color: #74c8b8", window.styleSheet())
        self.assertIn("background: #16776a", window.styleSheet())
        self.assertIn("color: #ffffff", window.styleSheet())
        self.apply(theme="light")
        self.assertIn("color: #16776A", window.styleSheet())

    def test_timer_covers_already_visible_dynamic_labels(self):
        window = self.window()
        label = QLabel("닫기", window)
        window.layout().addWidget(label)
        self.apply(language="en")
        label.setText("중지하는 중…")
        deadline = time.monotonic() + 1
        while label.text() != "Stopping…" and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.assertEqual(label.text(), "Stopping…")
        status = QStatusBar(window)
        status.showMessage("취소됨", 30)
        self.controller.refresh()
        self.assertEqual(status.currentMessage(), "Cancelled")
        deadline = time.monotonic() + .2
        while status.currentMessage() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.assertEqual(status.currentMessage(), "")

    def test_system_uses_style_hints_and_reacts_to_change(self):
        self.apply(theme="system")
        self.assertEqual(self.controller.dark, self.app.styleHints().colorScheme() == Qt.ColorScheme.Dark)
        with patch.object(self.controller, "reload") as reload:
            self.controller._scheme_changed(Qt.ColorScheme.Dark)
            reload.assert_called_once()
        self.apply(theme="light")
        with patch.object(self.controller, "reload") as reload:
            self.controller._scheme_changed(Qt.ColorScheme.Dark)
            reload.assert_not_called()

    def test_worker_request_runs_on_gui_thread(self):
        self.settings["language"] = "en"
        errors = []
        def run():
            try:
                apply_appearance(self.app)
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=run)
        worker.start()
        worker.join(timeout=2)
        self.app.processEvents()
        self.assertEqual(errors, [])
        self.assertEqual(self.controller.language, "en")
        self.assertEqual(self.controller.thread(), self.app.thread())

    def test_version_and_payload_translations(self):
        self.assertEqual(translate_text("새 버전 <b>yzip 2.7</b>이(가) 나왔습니다. 지금 버전: 2.6"),
                         "New version <b>yzip 2.7</b> is available. Current version: 2.6")
        self.assertEqual(translate_text("압축하기 환경설정.zip"), "Compressing 환경설정.zip")
        self.assertEqual(translate_text("100% 압축 푸는 중... - yzip"), "100% Extracting... - yzip")
        self.assertEqual(translate_text("C:/환경설정/닫기.zip"), "C:/환경설정/닫기.zip")

    def test_actual_main_preferences_compress_extract_progress(self):
        import yzip_gui as gui
        from preferences import PreferencesDialog
        with patch.object(gui.Main, "start_update_check"), patch("assoc.status", return_value={}), \
                patch("assoc.enabled", return_value=False):
            main = gui.Main()
            preferences = PreferencesDialog(main)
        compress = gui.CompressDialog(main, "C:/환경설정/닫기.zip")
        original_path = compress.path.text()
        extract = gui.ExtractDialog(main, "C:/환경설정", 12)
        progress = gui.ProgressWindow(main, "압축 푸는 중...")
        self.windows.extend([main, preferences, compress, extract, progress])
        self.controller.refresh()
        korean_home = main.findChild(QLabel, "homeTitle").text()
        self.assertEqual(korean_home, "압축 파일 작업, 간편하게")
        original_values = preferences.values()
        self.apply(language="en", theme="dark", font_size=12)
        self.assertEqual(main.findChild(QLabel, "homeTitle").text(), "Archive tasks, made simple")
        self.assertEqual(preferences.tabs.tabText(0), "General")
        self.assertEqual(preferences.values(), original_values)
        self.assertEqual(compress.windowTitle(), "Create archive")
        self.assertEqual(compress.path.text(), original_path)
        self.assertEqual(extract.sel_only.text(), "Only the selected 12 items")
        progress._show_stats(2, 10, "환경설정")
        self.controller.refresh()
        self.assertIn("환경설정", progress.label.text())
        self.assertNotIn("Preferences", progress.label.text())
        self.assertIn("#e6edf3", progress.label.text())
        progress.show_done()
        self.controller.refresh()
        self.assertIn("Operation completed.", progress.label.text())
        self.apply(language="ko", theme="light", font_size=10)
        self.assertEqual(main.findChild(QLabel, "homeTitle").text(), korean_home)
        self.assertEqual(preferences.tabs.tabText(0), "일반")
        self.assertEqual(preferences.values(), original_values)
        # Optional reviewable snapshots, still entirely offscreen.
        output = os.environ.get("YZIP_APPEARANCE_SNAPSHOTS")
        if output:
            folder = Path(output)
            folder.mkdir(parents=True, exist_ok=True)
            for lang, theme in (("ko", "light"), ("ko", "dark"), ("en", "dark")):
                self.apply(language=lang, theme=theme)
                for name, widget in (("main", main), ("preferences", preferences), ("compress", compress)):
                    widget.show()
                    self.app.processEvents()
                    self.assertTrue(widget.grab().save(str(folder / f"{name}-{lang}-{theme}.png")))


if __name__ == "__main__":
    unittest.main()
