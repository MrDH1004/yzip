"""Behavioral regressions for the renewed Qt UI; no desktop access required."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton

import appcore
import appearance
import yzip_gui as gui


class RenewalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if not QFontDatabase.families():
            for name in ("segoeui.ttf", "malgun.ttf", "segmdl2.ttf"):
                path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
                if path.is_file():
                    QFontDatabase.addApplicationFont(str(path))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yzip-ui-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"YZIP_SETTINGS": str(self.root / "settings.json"), "YZIP_NO_OPEN": "1"})
        env.start()
        self.addCleanup(env.stop)
        appcore.save_settings({"theme": "light", "language": "ko", "font_size": 10})
        appearance.apply_appearance(self.app)
        self.win = gui.Main()
        self.win.show()
        self.app.processEvents()
        self.addCleanup(self.close)
        self.arc = self.root / "review.zip"
        with zipfile.ZipFile(self.arc, "w") as z:
            z.writestr("Alpha.txt", "alpha")
            z.writestr("Beta.txt", "beta")
            z.writestr("docs/inside.txt", "inside")

    def close(self):
        self.win.close()
        self.win.deleteLater()
        self.app.processEvents()

    def test_home_buttons_and_permanent_header_route_to_real_actions(self):
        with mock.patch.object(self.win, "open_dialog") as opened, \
                mock.patch.object(self.win, "new_archive") as created, \
                mock.patch.object(self.win, "preferences") as prefs, \
                mock.patch.object(self.win, "start_update_check") as update:
            for button in (self.win.home_open_btn, self.win.home_new_btn, self.win.settings_btn,
                           self.win.update_check_btn, self.win.formats_btn):
                QTest.mouseClick(button, Qt.LeftButton)
            opened.assert_called_once_with()
            created.assert_called_once_with()
            self.assertEqual(prefs.call_args_list, [mock.call(), mock.call(section="associations")])
            update.assert_called_once_with(manual=True)
        self.assertEqual(self.win.size().toTuple(), (640, 454))
        self.assertEqual(self.win.minimumSize().toTuple(), (640, 420))

    def test_supported_formats_distinguish_reading_and_writing(self):
        foot = self.win.homeFoot.text()
        for name in ("ZIP", "7Z", "RAR", "EGG", "ALZ", "TAR", "ISO", "CAB", "ZIPX", "LZH", "ZPAQ"):
            self.assertIn(name, foot)
        self.assertTrue(foot.startswith("열기·풀기:"))
        d = gui.CompressDialog(self.win, str(self.root / "new.zip"))
        self.addCleanup(d.close)
        self.assertNotIn("rar", [d.fmt.itemData(i) for i in range(d.fmt.count())])

    def test_minimum_home_keeps_primary_controls_and_footer_in_view(self):
        self.win.resize(640, 454)
        QTest.qWait(100)
        for widget in (self.win.home_open_btn, self.win.home_new_btn, self.win.settings_btn,
                       self.win.update_check_btn, self.win.homeTitle, self.win.homeFoot,
                       self.win.formats_btn):
            rect = widget.rect()
            self.assertTrue(self.win.rect().contains(widget.mapTo(self.win, rect.topLeft())))
            self.assertTrue(self.win.rect().contains(widget.mapTo(self.win, rect.bottomRight())))
        self.assertEqual(self.win.home_scroll.verticalScrollBar().maximum(), 0)
        self.assertNotIn("??", self.win.homeTitle.text())
        self.assertEqual(self.win.homeTitle.text(), "압축 파일 작업, 간편하게")

    def test_drop_area_stays_compact_when_window_grows(self):
        QTest.qWait(100)
        compact_height = self.win.drop_zone.height()
        self.assertLessEqual(compact_height, 200)
        notes = [label for label in self.win.drop_zone.findChildren(QLabel)
                 if label.objectName() == "homeActionHint"]
        for note in notes:
            self.assertGreaterEqual(note.width(), note.sizeHint().width())
            self.assertEqual(note.height(), note.sizeHint().height())
        self.win.resize(960, 680)
        QTest.qWait(100)
        self.assertLessEqual(self.win.drop_zone.height(), compact_height)

    def test_english_default_font_keeps_footer_visible_after_language_change(self):
        QTest.qWait(100)
        appcore.save_settings({"language": "en", "font_size": 10})
        appearance.apply_appearance(self.app)
        QTest.qWait(150)
        self.assertEqual(self.win.home_scroll.verticalScrollBar().maximum(), 0)
        button = self.win.formats_btn
        self.assertTrue(self.win.rect().contains(button.mapTo(self.win, button.rect().bottomRight())))

    def test_large_font_home_actions_keep_height_and_are_scroll_accessible(self):
        for language in ("ko", "en"):
            for size in (16, 20):
                with self.subTest(language=language, size=size):
                    appcore.save_settings({"font_family": "Segoe UI", "font_size": size, "language": language})
                    appearance.apply_appearance(self.app)
                    self.win.resize(640, 454)
                    QTest.qWait(100)
                    for button in (self.win.home_open_btn, self.win.home_new_btn):
                        self.assertGreaterEqual(button.height(), button.sizeHint().height())
                        self.win.home_scroll.ensureWidgetVisible(button, 0, 4)
                        self.app.processEvents()
                        rect = button.rect()
                        viewport = self.win.home_scroll.viewport()
                        self.assertTrue(viewport.rect().contains(button.mapTo(viewport, rect.center())))
                    for button in (self.win.settings_btn, self.win.update_check_btn):
                        self.assertGreaterEqual(button.height(), button.sizeHint().height())
                        self.assertGreaterEqual(button.width(), button.sizeHint().width())
                        self.assertTrue(self.win.rect().contains(button.mapTo(self.win, button.rect().bottomRight())))
                    self.assertEqual(self.win.width(), 640)
                    self.assertGreater(self.win.home_scroll.verticalScrollBar().maximum(), 0)

    def test_search_deselects_hidden_items_and_clear_restores_visibility_only(self):
        self.assertTrue(self.win.open_archive(str(self.arc)))
        for i in range(self.win.tree.topLevelItemCount()):
            self.win.tree.topLevelItem(i).setSelected(True)
        self.win.search.setText("ALPHA")
        self.assertEqual(self.win.selected_names(), ["Alpha.txt"])
        visible = [self.win.tree.topLevelItem(i).text(0) for i in range(self.win.tree.topLevelItemCount())
                   if not self.win.tree.topLevelItem(i).isHidden()]
        self.assertEqual(visible, ["Alpha.txt"])
        self.win.search.clear()
        self.assertEqual(self.win.selected_names(), ["Alpha.txt"])
        self.assertTrue(all(not self.win.tree.topLevelItem(i).isHidden()
                            for i in range(self.win.tree.topLevelItemCount())))

    def test_search_scope_navigation_and_new_archive_reset(self):
        self.win.open_archive(str(self.arc))
        self.win.search.setText("inside")
        self.assertTrue(all(self.win.tree.topLevelItem(i).isHidden()
                            for i in range(self.win.tree.topLevelItemCount())))
        self.win.go_to("docs/")
        self.assertEqual(self.win.search.text(), "")
        self.assertEqual(self.win.tree.topLevelItemCount(), 1)
        self.assertEqual(self.win.tree.topLevelItem(0).text(0), "inside.txt")
        self.win.search.setText("no match")
        second = self.root / "other.zip"
        with zipfile.ZipFile(second, "w") as z:
            z.writestr("fresh.txt", "fresh")
        self.win.open_archive(str(second))
        self.assertEqual(self.win.search.text(), "")
        self.assertFalse(self.win.tree.topLevelItem(0).isHidden())

    def test_filtered_extraction_defaults_to_visible_files_and_expands_folders(self):
        appcore.save_settings({"extract_smart": False, "extract_open_folder": False})
        self.win.open_archive(str(self.arc))
        for query, expected in (("ALPHA", ["Alpha.txt"]), ("docs", ["docs/inside.txt"])):
            self.win.search.setText(query)
            self.assertTrue(self.win.a_extract.isEnabled())
            def accept(d):
                self.assertTrue(d.sel_only.isChecked())
                self.assertEqual(d.sel_only.text(), "검색 결과 1개 항목만")
                return QDialog.Accepted
            with mock.patch.object(gui.ExtractDialog, "exec", accept), \
                    mock.patch.object(self.win, "_do_extract", return_value=True) as extract:
                self.win.extract()
            self.assertEqual(extract.call_args.args[1], expected)

    def test_filtered_extraction_all_requires_explicit_checkbox_opt_out(self):
        appcore.save_settings({"extract_smart": False, "extract_open_folder": False})
        self.win.open_archive(str(self.arc))
        self.win.search.setText("alpha")
        def accept_all(d):
            self.assertTrue(d.sel_only.isChecked())
            d.sel_only.setChecked(False)
            return QDialog.Accepted
        with mock.patch.object(gui.ExtractDialog, "exec", accept_all), \
                mock.patch.object(self.win, "_do_extract", return_value=True) as extract:
            self.win.extract()
        self.assertIsNone(extract.call_args.args[1])

    def test_filtered_extraction_writes_only_matching_members(self):
        dest = self.root / "filtered-output"
        appcore.save_settings({"extract_smart": False, "extract_open_folder": False, "extract_dir": str(dest)})
        self.win.open_archive(str(self.arc))
        self.win.search.setText("alpha")
        def immediate_job(parent, title, fn, **kwargs):
            return appcore.outcome(fn, lambda *args: None, lambda: False)
        with mock.patch.object(gui.ExtractDialog, "exec", return_value=QDialog.Accepted), \
                mock.patch.object(gui, "run_job", side_effect=immediate_job):
            self.win.extract()
        self.assertEqual((dest / "Alpha.txt").read_text(encoding="utf-8"), "alpha")
        self.assertFalse((dest / "Beta.txt").exists())
        self.assertFalse((dest / "docs").exists())

    def test_zero_search_results_disable_extraction_and_guard_direct_call(self):
        self.win.open_archive(str(self.arc))
        self.win.search.setText("no match")
        self.assertFalse(self.win.a_extract.isEnabled())
        with mock.patch.object(gui.QMessageBox, "information") as info, \
                mock.patch.object(gui, "ExtractDialog") as dialog, \
                mock.patch.object(self.win, "_do_extract") as extract:
            self.win.extract()
        info.assert_called_once()
        dialog.assert_not_called()
        extract.assert_not_called()
        self.win.search.clear()
        self.assertTrue(self.win.a_extract.isEnabled())

    def test_archive_header_home_and_toolbar_compatibility(self):
        self.win.open_archive(str(self.arc))
        self.app.processEvents()
        self.assertTrue(self.win.settings_btn.isVisible())
        self.assertTrue(self.win.update_check_btn.isVisible())
        self.assertEqual(self.win.toolbar.actions()[:3], [self.win.a_extract, self.win.a_open, self.win.a_new])
        self.win.home_btn.click()
        self.assertEqual(self.win.pages.currentIndex(), 0)
        self.assertIsNone(self.win.arc_path)
        self.assertEqual(self.win.selected_names(), [])
        self.assertFalse(self.win.a_extract.isEnabled())

    def test_zip_repair_action_uses_separate_default_output_and_reports_result(self):
        self.win.open_archive(str(self.arc))
        self.assertTrue(hasattr(self.win, "a_repair"))
        self.assertTrue(self.win.a_repair.isEnabled())
        expected = self.root / "review_recovered.zip"

        def immediate(_parent, _title, fn, **_kwargs):
            return True, fn(lambda *_args: None, lambda: False)

        result = mock.Mock(output=str(expected), recovered=("Alpha.txt", "Beta.txt"), skipped=())
        with mock.patch.object(gui.QFileDialog, "getSaveFileName", return_value=(str(expected), "ZIP (*.zip)")) as save, \
                mock.patch.object(gui, "run_job", side_effect=immediate), \
                mock.patch.object(gui.ziprepair, "repair_zip", return_value=result) as repair, \
                mock.patch.object(gui.QMessageBox, "information") as info:
            self.win.repair_archive()

        self.assertEqual(Path(save.call_args.args[2]), expected)
        repair.assert_called_once()
        self.assertEqual(repair.call_args.args[:2], (str(self.arc), str(expected)))
        self.assertIn("2", info.call_args.args[2])

    def test_opening_zip_with_missing_central_directory_offers_repair(self):
        damaged = self.root / "broken.zip"
        damaged.write_bytes(self.arc.read_bytes().split(b"PK\x01\x02", 1)[0])

        with mock.patch.object(gui.QMessageBox, "question", return_value=gui.QMessageBox.Yes) as question, \
                mock.patch.object(self.win, "repair_archive") as repair, \
                mock.patch.object(gui.QMessageBox, "critical") as critical:
            opened = self.win.open_archive(str(damaged))

        self.assertFalse(opened)
        question.assert_called_once()
        repair.assert_called_once_with(str(damaged))
        critical.assert_not_called()

    def test_support_details_opens_association_tab(self):
        dlg = mock.Mock()
        dlg.exec.return_value = QDialog.Rejected
        with mock.patch.object(gui, "SettingsDialog", return_value=dlg):
            self.win.preferences(section="associations")
        dlg.tabs.setCurrentIndex.assert_called_once_with(1)
        dlg.apply.assert_not_called()

    def test_progress_cancel_is_once_and_done_button_closes(self):
        d = gui.ProgressWindow(self.win, "압축 풀기")
        canceled = mock.Mock()
        d.canceled.connect(canceled)
        d.cancel()
        d.cancel()
        canceled.assert_called_once_with()
        self.assertFalse(d.stop_btn.isEnabled())
        d.show_done()
        self.assertEqual(d.stop_btn.text(), "닫기")
        self.assertTrue(d.stop_btn.isEnabled())
        d.stop_btn.click()
        self.assertEqual(d.result(), QDialog.Accepted)

    def test_dark_progress_surface_and_icon_actions_remain_readable(self):
        appcore.save_settings({"theme": "dark"})
        d = gui.ProgressWindow(self.win, "압축 풀기", str(self.root))
        d.show()
        appearance.apply_appearance(self.app)
        self.app.processEvents()
        self.assertLess(d.grab().toImage().pixelColor(1, 1).lightness(), 100)
        for button in (d.folder_btn, d.keep_btn, d.menu_btn):
            self.assertEqual(button.text(), "")
            self.assertFalse(button.icon().isNull())
            self.assertTrue(button.accessibleName())
        d.show_done()
        d.close()

    def test_extract_options_use_native_arrows_and_keep_saved_collision_mode(self):
        appcore.save_settings({"extract_mode": "ask"})
        d = gui.ExtractDialog(self.win, str(self.root), 1)
        self.addCleanup(d.close)
        self.assertEqual(d.more.arrowType(), Qt.DownArrow)
        self.assertEqual(d.more.text(), "옵션 더 보기")
        d.more.setChecked(True)
        self.assertEqual(d.more.arrowType(), Qt.UpArrow)
        self.assertTrue(d.mode_ask.isChecked())
        d.more.setChecked(False)
        self.assertEqual(d.more.arrowType(), Qt.DownArrow)


if __name__ == "__main__":
    unittest.main()
