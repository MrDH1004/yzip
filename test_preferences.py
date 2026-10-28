"""Offscreen preferences tests. Never touches the user's settings or associations."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QLabel, QListWidget, QMessageBox,
    QCheckBox, QLineEdit, QPushButton, QScrollArea, QWidget,
)
import shiboken6

import appcore
import appearance
import assoc
import preferences
import updater


class PreferencesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        # The Windows offscreen plugin does not discover system fonts itself.
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "malgun.ttf"
        if not QFontDatabase.families() and font.is_file():
            if QFontDatabase.addApplicationFont(str(font)) < 0:
                raise RuntimeError("Could not load the offscreen test font")

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="yzip-preferences-test-")
        self.addCleanup(self.folder.cleanup)
        self.settings_path = Path(self.folder.name) / "settings.json"
        self.patch(mock.patch.dict(os.environ, {"YZIP_SETTINGS": str(self.settings_path)}))
        appearance.apply_appearance(self.app)
        self.patch(mock.patch.object(assoc, "status", return_value={
            ext: (False, "다른 앱") for ext in assoc.TYPES}))
        self.registered = set()
        self.patch(mock.patch.object(assoc, "enabled", side_effect=lambda exts=None:
                                    all(ext in self.registered for ext in (exts or assoc.TYPES))))
        self.register = self.patch(mock.patch.object(assoc, "register"))
        self.unregister = self.patch(mock.patch.object(assoc, "unregister"))
        self.critical = self.patch(mock.patch.object(QMessageBox, "critical"))
        self.warning = self.patch(mock.patch.object(QMessageBox, "warning"))
        self.info = self.patch(mock.patch.object(QMessageBox, "information"))
        self.patch(mock.patch.object(appcore, "log_exception"))

    def patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def dialog(self, parent=None):
        dialog = preferences.PreferencesDialog(parent)
        self.addCleanup(lambda: shiboken6.delete(dialog) if shiboken6.isValid(dialog) else None)
        return dialog

    def wait_for(self, predicate, seconds=3):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(predicate(), "background operation did not complete")

    def write_import(self, data):
        path = Path(self.folder.name) / "import.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_tabs_and_defaults_cover_core_schema(self):
        dialog = self.dialog()
        self.assertEqual([dialog.tabs.tabText(i) for i in range(dialog.tabs.count())],
                         ["일반", "파일연결", "탐색기", "압축", "해제", "성능", "업데이트", "설정관리"])
        self.assertEqual(dialog.values(), appcore.DEFAULTS)
        self.assertFalse(self.settings_path.exists())

    def test_sidebar_and_tabs_stay_in_sync_and_keep_page_children_accessible(self):
        dialog = self.dialog()
        self.assertEqual(dialog.objectName(), "settingsDialog")
        self.assertIs(dialog.findChild(QListWidget, "settingsNav"), dialog.nav)
        self.assertIsNotNone(dialog.findChild(QWidget, "settingsBody"))
        self.assertIsNotNone(dialog.findChild(QLabel, "settingsTitle"))
        self.assertIsNotNone(dialog.findChild(QLabel, "settingsSubtitle"))
        self.assertEqual(dialog.nav.count(), 8)
        self.assertTrue(dialog.tabs.tabBar().isHidden())
        self.assertEqual((dialog.width(), dialog.height()), (920, 650))
        self.assertEqual((dialog.minimumWidth(), dialog.minimumHeight()), (760, 560))
        self.assertGreaterEqual(dialog.nav.minimumWidth(), 156)
        self.assertLessEqual(dialog.nav.maximumWidth(), 180)
        pages = [dialog.tabs.widget(i) for i in range(8)]
        for index, page in enumerate(pages):
            with self.subTest(index=index):
                self.assertEqual(dialog.nav.item(index).text(), dialog.tabs.tabText(index))
                dialog.nav.setCurrentRow(index)
                self.assertEqual(dialog.tabs.currentIndex(), index)
                self.assertIs(dialog.tabs.currentWidget(), page)
                if index == 1:
                    self.assertIsNone(page.findChild(QScrollArea))
                    self.assertIsNone(page.findChild(QLabel, "settingsSectionTitle"))
                else:
                    self.assertIsNotNone(page.findChild(QScrollArea))
                    self.assertIsNotNone(page.findChild(QLabel, "settingsSectionTitle"))
        dialog.tabs.setCurrentIndex(0)
        self.assertEqual(dialog.nav.currentRow(), 0)
        self.assertIs(dialog.tabs.widget(0).findChild(QWidget, "language"), dialog.controls["language"])
        self.assertIs(dialog.tabs.widget(1).findChild(type(dialog.assoc_list)), dialog.assoc_list)

    def test_save_and_cancel_buttons_keep_staged_host_flow(self):
        dialog = self.dialog()
        save = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
        cancel = dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.assertEqual(save.text(), "저장")
        self.assertEqual(cancel.text(), "취소")
        self.assertTrue(save.isDefault())
        self.assertFalse(cancel.autoDefault())
        dialog.auto_update_check.setChecked(False)
        save.click()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertFalse(self.settings_path.exists())
        self.register.assert_not_called()
        self.unregister.assert_not_called()
        self.assertTrue(dialog.apply())
        self.assertFalse(appcore.load_settings()["auto_update"])

    def test_association_page_prioritizes_list_at_minimum_window_size(self):
        dialog = self.dialog()
        font = dialog.font()
        font.setPointSize(10)
        dialog.setFont(font)
        dialog.resize(760, 560)
        dialog.nav.setCurrentRow(1)
        dialog.show()
        self.app.processEvents()
        page = dialog.tabs.widget(1)
        self.assertIsNone(page.findChild(QScrollArea))
        self.assertEqual(page.findChildren(QLabel), [])
        self.assertEqual(page.findChildren(QLineEdit), [])
        self.assertEqual(page.findChildren(QCheckBox), [])
        self.assertEqual([b.text() for b in page.findChildren(QPushButton)],
                         ["전체 선택", "전체 해제", "Windows 기본 앱 열기"])
        self.assertEqual(dialog.assoc_list.objectName(), "settingsAssocList")
        self.assertEqual(dialog.assoc_list.font().pointSize(), 10)
        self.assertIs(page.layout().itemAt(0).widget(), dialog.assoc_list)
        self.assertLessEqual(dialog.assoc_list.y(), 4)
        self.assertGreater(dialog.assoc_list.height(), page.height() * 0.75)
        self.assertGreater(dialog.assoc_list.verticalScrollBar().maximum(), 0)
        self.assertEqual(dialog.assoc_list.columnCount(), 3)
        self.assertEqual([dialog.assoc_list.headerItem().text(i) for i in range(3)],
                         ["확장자", "파일 형식", "현재 기본 앱"])
        self.assertEqual([dialog.assoc_list.topLevelItem(i).text(0)
                          for i in range(dialog.assoc_list.topLevelItemCount())],
                         list(preferences.formats.SUPPORTED_TYPES))

    def test_individual_selection_preserves_initial_checks_and_only_applies_diff(self):
        self.registered = {".zip", ".7z"}
        dialog = self.dialog()
        dialog._association_items[".tar"].setCheckState(0, Qt.CheckState.Checked)
        self.assertTrue(dialog.apply())
        self.register.assert_called_once_with([".tar"])
        self.unregister.assert_not_called()
        for ext in (".zip", ".7z", ".tar"):
            self.assertEqual(dialog._association_items[ext].checkState(0), Qt.CheckState.Checked)

    def test_bulk_buttons_stage_selection_without_changing_registry(self):
        dialog = self.dialog()
        buttons = {b.text(): b for b in dialog.tabs.widget(1).findChildren(QPushButton)}
        for title, state in (("전체 선택", Qt.CheckState.Checked),
                             ("전체 해제", Qt.CheckState.Unchecked)):
            buttons[title].click()
            self.assertTrue(all(item.checkState(0) == state
                                for item in dialog._association_items.values()))
        self.register.assert_not_called()
        self.unregister.assert_not_called()

    def test_small_window_large_font_keeps_pages_scrollable_and_footer_visible(self):
        self.addCleanup(lambda: appearance.apply_appearance(self.app))
        with mock.patch.object(appcore, "load_settings", return_value=dict(appcore.DEFAULTS, font_size=20)):
            controller = appearance.apply_appearance(self.app)
        dialog = self.dialog()
        dialog.resize(760, 560)
        dialog.show()
        self.addCleanup(dialog.hide)
        controller.refresh()
        self.assertEqual(dialog.controls["language"].font().pointSize(), 20)
        for index in range(8):
            with self.subTest(index=index):
                dialog.nav.setCurrentRow(index)
                self.app.processEvents()
                page = dialog.tabs.widget(index)
                scroll = dialog.assoc_list if index == 1 else page.findChild(QScrollArea)
                if index == 1:
                    self.assertIsNone(page.findChild(QScrollArea))
                else:
                    self.assertTrue(scroll.widgetResizable())
                self.assertGreater(scroll.viewport().height(), 0)
                self.assertGreater(scroll.viewport().width(), 0)
                for standard in (QDialogButtonBox.StandardButton.Ok, QDialogButtonBox.StandardButton.Cancel):
                    button = dialog.buttons.button(standard)
                    self.assertTrue(button.isVisible())
                    self.assertTrue(dialog.rect().contains(button.mapTo(dialog, button.rect().bottomRight())))
        dialog.nav.setCurrentRow(1)
        self.app.processEvents()
        self.assertGreater(dialog.assoc_list.verticalScrollBar().maximum(), 0)
        dialog.assoc_list.scrollToItem(dialog.assoc_list.topLevelItem(dialog.assoc_list.topLevelItemCount() - 1))
        self.app.processEvents()
        last = dialog.assoc_list.topLevelItem(dialog.assoc_list.topLevelItemCount() - 1)
        self.assertTrue(dialog.assoc_list.viewport().rect().intersects(dialog.assoc_list.visualItemRect(last)))
        for button in dialog.tabs.widget(1).findChildren(QPushButton):
            self.assertTrue(button.isVisible())
            self.assertTrue(dialog.rect().contains(button.mapTo(dialog, button.rect().bottomRight())))
        self.assertTrue(dialog.assoc_list.isVisible())

    def test_license_dialog_uses_shared_preferences_style(self):
        dialog = self.dialog()
        seen = []

        def inspect(license_dialog):
            expected_style = preferences.PREFERENCES_QSS
            controller = getattr(self.app, "_yzip_appearance", None)
            if controller is not None:
                expected_style = controller._style(expected_style)
            self.assertEqual(license_dialog.styleSheet(), expected_style)
            seen.append((license_dialog.objectName(),
                         license_dialog.findChild(QLabel, "settingsTitle").text()))
            return QDialog.DialogCode.Rejected

        with mock.patch.object(QDialog, "exec", inspect):
            dialog._show_licenses()
        self.assertEqual(seen, [("settingsDialog", "라이선스")])

    def test_every_preference_roundtrips_without_losing_other_app_state(self):
        settings = {
            "language": "en", "theme": "dark", "font_family": "맑은 고딕", "font_size": 96,
            "compress_format": "7z", "compress_level": 9, "compress_dir": "C:/압축",
            "compress_split": "250MB", "compress_open_folder": True,
            "extract_dir": "C:/해제", "extract_smart": False, "extract_mode": "rename",
            "extract_open_folder": True, "low_priority": True, "worker_threads": 4,
            "temp_dir": str(Path(self.folder.name) / "임시"), "shell_enabled": False, "shell_compress": False,
            "shell_extract_here": False, "shell_extract_folder": False,
            "shell_multi": "separate", "auto_update": False,
        }
        appcore.save_settings(dict(settings, show_details=True, update_last_result="기존 결과"))
        dialog = self.dialog()
        self.assertEqual(dialog.values(), settings)
        dialog.controls["compress_level"].setValue(0)
        self.assertTrue(dialog.apply())
        saved = appcore.load_settings()
        self.assertEqual(saved["compress_level"], 0)
        self.assertTrue(saved["show_details"])
        self.assertEqual(saved["update_last_result"], "기존 결과")
        self.register.assert_not_called()
        self.unregister.assert_not_called()

    def test_cancel_and_accept_do_not_save_before_explicit_apply(self):
        dialog = self.dialog()
        dialog.auto_update_check.setChecked(False)
        dialog.reject()
        self.assertFalse(self.settings_path.exists())
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertFalse(self.settings_path.exists())
        self.assertTrue(dialog.apply())
        self.assertFalse(appcore.load_settings()["auto_update"])

    def test_supported_types_are_listed_and_exe_cannot_be_checked(self):
        dialog = self.dialog()
        items = {dialog.assoc_list.topLevelItem(i).text(0): dialog.assoc_list.topLevelItem(i)
                 for i in range(dialog.assoc_list.topLevelItemCount())}
        self.assertEqual(set(items), set(preferences.formats.SUPPORTED_TYPES))
        self.assertFalse(items[".exe"].flags() & Qt.ItemFlag.ItemIsEnabled)
        self.assertFalse(items[".exe"].flags() & Qt.ItemFlag.ItemIsUserCheckable)
        self.assertNotIn(".exe", dialog._association_items)
        self.assertEqual(items[".zip"].text(2), "다른 앱")

    def test_partial_association_diff_and_retry_are_idempotent(self):
        self.registered = {".zip", ".7z"}
        dialog = self.dialog()
        dialog._association_items[".zip"].setCheckState(0, Qt.CheckState.Unchecked)
        dialog._association_items[".tar"].setCheckState(0, Qt.CheckState.Checked)
        self.assertTrue(dialog.apply())
        self.register.assert_called_once_with([".tar"])
        self.unregister.assert_called_once_with([".zip"])
        self.assertTrue(dialog.apply())
        self.assertEqual(self.register.call_count, 1)
        self.assertEqual(self.unregister.call_count, 1)

    def test_bulk_buttons_select_and_clear_every_associable_type(self):
        dialog = self.dialog()
        next(b for b in dialog.tabs.widget(1).findChildren(QPushButton) if b.text() == "전체 선택").click()
        self.assertTrue(all(item.checkState(0) == Qt.CheckState.Checked
                            for item in dialog._association_items.values()))
        self.assertTrue(dialog.apply())
        expected = sorted(dialog._association_items)
        self.register.assert_called_once_with(expected)
        next(b for b in dialog.tabs.widget(1).findChildren(QPushButton) if b.text() == "전체 해제").click()
        self.assertTrue(dialog.apply())
        self.unregister.assert_called_once_with(expected)
        self.assertNotIn(".exe", expected)

    def test_association_failure_is_visible_and_only_unfinished_diff_is_retried(self):
        self.registered = {".zip"}
        dialog = self.dialog()
        dialog._association_items[".zip"].setCheckState(0, Qt.CheckState.Unchecked)
        dialog._association_items[".7z"].setCheckState(0, Qt.CheckState.Checked)
        self.unregister.side_effect = PermissionError("registry denied")
        self.assertFalse(dialog.apply())
        self.critical.assert_called_once()
        self.assertIn("registry denied", self.critical.call_args.args[2])
        self.unregister.side_effect = None
        self.assertTrue(dialog.apply())
        self.assertEqual(self.register.call_count, 1)
        self.assertEqual(self.unregister.call_count, 2)

    def test_save_failure_does_not_change_associations(self):
        dialog = self.dialog()
        next(b for b in dialog.tabs.widget(1).findChildren(QPushButton) if b.text() == "전체 선택").click()
        with mock.patch.object(appcore, "save_settings", side_effect=OSError("disk full")):
            self.assertFalse(dialog.apply())
        self.register.assert_not_called()
        self.unregister.assert_not_called()
        self.assertIn("disk full", self.critical.call_args.args[2])

    def test_runtime_validation_failure_preserves_saved_settings_and_associations(self):
        appcore.save_settings({"theme": "light", "temp_dir": self.folder.name})
        original = self.settings_path.read_bytes()
        dialog = self.dialog()
        dialog.controls["theme"].setCurrentIndex(dialog.controls["theme"].findData("dark"))
        denied_path = str(Path(self.folder.name) / "접근불가")
        dialog.controls["temp_dir"].setText(denied_path)
        next(b for b in dialog.tabs.widget(1).findChildren(QPushButton) if b.text() == "전체 선택").click()
        with mock.patch.object(appcore, "validate_runtime_settings", side_effect=PermissionError("temp denied")) as validate, \
                mock.patch.object(appcore, "save_settings") as save, \
                mock.patch.object(appcore.runtimeconfig, "configure") as configure:
            self.assertFalse(dialog.apply())
        validate.assert_called_once_with(dialog.values())
        save.assert_not_called()
        configure.assert_not_called()
        self.assertEqual(self.settings_path.read_bytes(), original)
        self.register.assert_not_called()
        self.unregister.assert_not_called()
        self.assertIn("temp denied", self.critical.call_args.args[2])

    def test_runtime_validation_precedes_save_without_configuring_live_runtime(self):
        dialog = self.dialog()
        dialog.controls["temp_dir"].setText(self.folder.name)
        calls = mock.Mock()
        with mock.patch.object(appcore, "validate_runtime_settings") as validate, \
                mock.patch.object(appcore, "save_settings", wraps=appcore.save_settings) as save, \
                mock.patch.object(appcore.runtimeconfig, "configure") as configure:
            calls.attach_mock(validate, "validate")
            calls.attach_mock(save, "save")
            self.assertTrue(dialog.apply())
        self.assertEqual(calls.mock_calls, [mock.call.validate(dialog.values()), mock.call.save(dialog.values())])
        configure.assert_not_called()

    def test_real_runtime_probe_leaves_current_runtime_unchanged(self):
        dialog = self.dialog()
        dialog.controls["temp_dir"].setText(self.folder.name)
        runtime = appcore.runtimeconfig
        before = (tempfile.tempdir, runtime.worker_count(), dict(runtime._sessions))
        with mock.patch.object(runtime, "configure") as configure:
            self.assertTrue(dialog.apply())
        self.assertEqual((tempfile.tempdir, runtime.worker_count(), dict(runtime._sessions)), before)
        configure.assert_not_called()
        self.assertEqual(appcore.load_settings()["temp_dir"], self.folder.name)

    def test_association_query_failure_prevents_mutation(self):
        with mock.patch.object(assoc, "status", side_effect=OSError("query failed")):
            dialog = self.dialog()
        self.assertFalse(dialog.assoc_list.isEnabled())
        buttons = {b.text(): b for b in dialog.tabs.widget(1).findChildren(QPushButton)}
        self.assertFalse(buttons["전체 선택"].isEnabled())
        self.assertFalse(buttons["전체 해제"].isEnabled())
        self.assertTrue(buttons["Windows 기본 앱 열기"].isEnabled())
        dialog._select_associations(True)
        self.assertTrue(dialog.apply())
        self.register.assert_not_called()
        self.unregister.assert_not_called()
        self.critical.assert_called_once()

    def test_import_export_utf8_and_whitelist(self):
        dialog = self.dialog()
        source = self.write_import({"font_family": "맑은 고딕", "theme": "dark", "compress_split": "250MB"})
        dialog.import_file(source)
        self.assertFalse(self.settings_path.exists())
        out = Path(self.folder.name) / "export.json"
        dialog.export_file(out)
        exported = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(exported, dialog.values())
        self.assertIn("맑은 고딕", out.read_text(encoding="utf-8"))
        self.assertEqual(set(exported), set(appcore.DEFAULTS))

    def test_invalid_import_is_atomic(self):
        dialog = self.dialog()
        original = dialog.values()
        bad_values = [[], {"show_details": True}, {"theme": "neon"}, {"compress_level": 10},
                      {"worker_threads": True}, {"auto_update": "false"}, {"font_family": "a\0b"},
                      {"compress_split": "1KB"}, {"theme": "dark", "font_size": 200}]
        for data in bad_values:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    dialog.import_file(self.write_import(data))
                self.assertEqual(dialog.values(), original)
        self.assertFalse(self.settings_path.exists())

    def test_import_file_errors_are_displayed(self):
        dialog = self.dialog()
        with mock.patch.object(preferences.QFileDialog, "getOpenFileName",
                               return_value=(str(Path(self.folder.name) / "missing.json"), "")):
            dialog._import_settings()
        self.critical.assert_called_once()

    def test_custom_split_is_validated_before_accept_or_save(self):
        dialog = self.dialog()
        dialog.controls["compress_split"].setCurrentText("bad volume")
        dialog.accept()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.critical.assert_called_once()
        self.assertFalse(dialog.apply())
        self.assertFalse(self.settings_path.exists())

    def test_reset_is_staged_and_leaves_associations_and_history_untouched(self):
        self.registered = {".zip"}
        appcore.save_settings({"theme": "dark", "update_last_result": "지난 확인"})
        dialog = self.dialog()
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            dialog._reset()
        self.assertEqual(dialog.values(), appcore.DEFAULTS)
        self.assertEqual(appcore.load_settings()["theme"], "dark")
        self.assertEqual(dialog._association_items[".zip"].checkState(0), Qt.CheckState.Checked)
        self.assertEqual(dialog.last_result_label.text(), "지난 확인")

    def test_numeric_update_history_is_displayed(self):
        appcore.save_settings({"update_last_checked": 1700000000, "update_last_result": 123})
        dialog = self.dialog()
        self.assertIn("2023-", dialog.last_checked_label.text())
        self.assertEqual(dialog.last_result_label.text(), "123")

    def test_update_check_is_async_and_preserves_unsaved_preferences(self):
        dialog = self.dialog()
        dialog.auto_update_check.setChecked(False)
        release = threading.Event()
        started = threading.Event()

        def check():
            started.set()
            release.wait(3)
            return None

        with mock.patch.object(updater, "check", side_effect=check) as checker:
            self.addCleanup(release.set)
            dialog._check_update()
            self.assertTrue(started.wait(1))
            self.assertFalse(dialog.check_update_button.isEnabled())
            dialog._check_update()
            self.assertEqual(checker.call_count, 1)
            release.set()
            self.wait_for(lambda: dialog.check_update_button.isEnabled())
        saved = appcore.load_settings()
        self.assertIn("최신", saved["update_last_result"])
        self.assertIsInstance(saved["update_last_checked"], str)
        self.assertTrue(saved["auto_update"])
        self.assertFalse(dialog.auto_update_check.isChecked())

    def test_new_release_reaches_parent_consent_callback(self):
        parent = QWidget()
        self.addCleanup(lambda: shiboken6.delete(parent))
        parent._update_result = mock.Mock()
        dialog = self.dialog(parent)
        rel = updater.Release("99.0.0", "exe", "sha", "setup.exe", "", "")
        with mock.patch.object(updater, "check", return_value=rel):
            dialog._check_update()
            self.wait_for(lambda: dialog.check_update_button.isEnabled())
        parent._update_result.assert_called_once_with(rel, None, False)
        self.assertIn("99.0.0", appcore.load_settings()["update_last_result"])

    def test_update_error_is_recorded_and_shown(self):
        dialog = self.dialog()
        with mock.patch.object(updater, "check", side_effect=OSError("network unavailable")):
            dialog._check_update()
            self.wait_for(lambda: dialog.check_update_button.isEnabled())
        self.assertIn("network unavailable", appcore.load_settings()["update_last_result"])
        self.critical.assert_called_once()

    def test_destroying_dialog_during_update_is_safe(self):
        dialog = self.dialog()
        release = threading.Event()
        finished = threading.Event()

        def check():
            release.wait(3)
            finished.set()
            return None

        with mock.patch.object(updater, "check", side_effect=check):
            self.addCleanup(release.set)
            dialog._check_update()
            shiboken6.delete(dialog)
            release.set()
            self.assertTrue(finished.wait(1))
            self.app.processEvents()
        self.assertFalse(self.settings_path.exists())

    def test_cleanup_cancel_never_deletes_and_confirmation_shows_exact_paths(self):
        dialog = self.dialog()
        targets = [str(Path(self.folder.name) / "session-123"), str(Path(self.folder.name) / "session-456")]
        seen = []

        def decline(box):
            seen.append(box.detailedText())
            return QMessageBox.StandardButton.No

        with mock.patch.object(appcore, "cleanup_temp", return_value=targets) as cleaner, \
                mock.patch.object(QMessageBox, "exec", decline):
            dialog._cleanup_temp()
        cleaner.assert_called_once_with(dry_run=True)
        self.assertEqual(seen, ["\n".join(targets)])

    def test_cleanup_deletes_only_confirmed_paths_and_reports_errors(self):
        dialog = self.dialog()
        targets = [str(Path(self.folder.name) / "session-123")]
        with mock.patch.object(appcore, "cleanup_temp", side_effect=[targets,
                               {"removed": 0, "skipped": 0, "errors": 1}]) as cleaner, \
                mock.patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Yes):
            dialog._cleanup_temp()
        self.assertEqual(cleaner.call_args_list, [mock.call(dry_run=True), mock.call(paths=targets)])
        self.warning.assert_called_once()
        self.assertIn("오류: 1개", self.warning.call_args.args[2])

    def test_default_apps_failure_is_visible(self):
        dialog = self.dialog()
        button = next(b for b in dialog.tabs.widget(1).findChildren(QPushButton)
                      if b.text() == "Windows 기본 앱 열기")
        with mock.patch.object(assoc, "open_default_apps", side_effect=OSError("cannot open")):
            button.click()
        self.critical.assert_called_once()

    def test_default_apps_button_opens_windows_settings_without_applying_changes(self):
        dialog = self.dialog()
        button = next(b for b in dialog.tabs.widget(1).findChildren(QPushButton)
                      if b.text() == "Windows 기본 앱 열기")
        with mock.patch.object(assoc, "open_default_apps") as opened:
            button.click()
        opened.assert_called_once_with()
        self.assertFalse(self.settings_path.exists())
        self.register.assert_not_called()
        self.unregister.assert_not_called()


if __name__ == "__main__":
    unittest.main()
