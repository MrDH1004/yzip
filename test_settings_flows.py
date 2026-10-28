"""Settings integration tests. Never change user settings or Windows registrations.

Run: py -3.14 -m unittest -v test_settings_flows
"""
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThread, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

import appcore
import appearance
from preferences import PreferencesDialog
import yzip_gui as gui


class SettingsFlows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yzip-settings-flows-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        env = mock.patch.dict(os.environ, {
            "YZIP_SETTINGS": str(self.root / "settings.json"),
            "LOCALAPPDATA": str(self.root / "local"), "YZIP_NO_OPEN": "1",
        })
        env.start()
        self.addCleanup(env.stop)
        for target in ("yzip_gui.shell_register", "yzip_gui.shell_unregister", "assoc.register", "assoc.unregister",
                       "PySide6.QtWidgets.QMessageBox.critical", "PySide6.QtWidgets.QMessageBox.warning"):
            patcher = mock.patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.win = gui.Main()
        self.addCleanup(self.close_window)
        self.source = self.root / "source.txt"
        self.source.write_text("new contents", encoding="utf-8")
        self.archive = self.root / "input.zip"
        with zipfile.ZipFile(self.archive, "w") as z:
            z.writestr("a.txt", b"new")
            z.writestr("b.txt", b"other")
        self.win.arc_path = str(self.archive)

    def close_window(self):
        self.win.close()
        self.win.deleteLater()
        self.app.processEvents()

    def settings(self, **values):
        appcore.save_settings(values)

    @staticmethod
    def immediate_job(parent, title, fn, **kwargs):
        return appcore.outcome(fn, lambda *args: None, lambda: False)

    def test_preferences_alias_and_failed_apply_stops_followup(self):
        self.assertIs(gui.SettingsDialog, PreferencesDialog)
        dlg = mock.Mock()
        dlg.exec.return_value = QDialog.Accepted
        dlg.apply.return_value = False
        with mock.patch.object(gui, "SettingsDialog", return_value=dlg), \
                mock.patch.object(appcore, "apply_runtime_settings") as runtime, \
                mock.patch.object(appearance, "apply_appearance") as visual:
            self.win.preferences()
            runtime.assert_not_called()
            visual.assert_not_called()
            gui.shell_register.assert_not_called()
            gui.shell_unregister.assert_not_called()

    def test_preferences_apply_updates_runtime_appearance_shell_and_toggle(self):
        for shell in (False, True):
            with self.subTest(shell=shell):
                dlg = mock.Mock()
                dlg.exec.return_value = QDialog.Accepted
                dlg.auto_update_check.isChecked.return_value = False
                dlg.apply.side_effect = lambda: (self.settings(shell_enabled=shell, auto_update=False) or True)
                gui.shell_register.reset_mock()
                gui.shell_unregister.reset_mock()
                with mock.patch.object(gui, "SettingsDialog", return_value=dlg), \
                        mock.patch.object(appcore, "apply_runtime_settings") as runtime, \
                        mock.patch.object(appearance, "apply_appearance") as visual:
                    self.win.preferences()
                runtime.assert_called_once_with()
                visual.assert_called_once_with(self.app)
                self.assertFalse(self.win.a_auto_update.isChecked())
                self.assertEqual(gui.shell_register.call_count, int(shell))
                self.assertEqual(gui.shell_unregister.call_count, int(not shell))

    def test_compress_dialog_saved_defaults_and_stream_guard(self):
        output = self.root / "output"
        output.mkdir()
        self.settings(compress_format="zip", compress_level=0, compress_split="100MB", compress_dir=str(output))
        d = gui.CompressDialog(self.win, str(self.root / "archive.zip"), [str(self.source)])
        self.assertEqual(d.current_format().id, "zip")
        options = d.options()
        self.assertTrue(options["store_only"])
        self.assertEqual(options["split_size"], 100 * 1024**2)
        self.assertEqual(Path(options["out_path"]).parent, output)
        d.close()
        self.settings(compress_format="gz")
        one = gui.CompressDialog(self.win, str(self.root / "archive.zip"), [str(self.source)])
        self.assertEqual(one.current_format().id, "gz")
        self.assertEqual(Path(one.path.text()).name, "source.txt.gz")
        one.close()
        for selection in ([str(self.root)], [str(self.source), str(self.archive)]):
            many = gui.CompressDialog(self.win, str(self.root / "archive.zip"), selection)
            self.assertEqual(many.current_format().id, "zip")
            self.assertFalse(many.fmt.model().item(many.fmt.findData("gz")).isEnabled())
            many.close()

    def test_new_archive_separate_defaults_store_and_result_folder(self):
        output = self.root / "output"
        output.mkdir()
        self.settings(shell_multi="separate", compress_dir=str(output), compress_level=0, compress_open_folder=True)
        with mock.patch.object(gui, "run_job", side_effect=self.immediate_job), \
                mock.patch.object(gui, "writable_target", side_effect=lambda parent, target, *args: target), \
                mock.patch.object(gui, "open_path") as opened:
            self.win.new_archive([str(self.source), str(self.archive)], standalone=True, ask=False)
        self.assertEqual(opened.call_args_list, [mock.call(str(output)), mock.call(str(output))])
        for name in ("source.zip", "input.zip"):
            with zipfile.ZipFile(output / name) as z:
                self.assertTrue(all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist()))

    def test_new_archive_non_zip_default_and_explicit_shell_zip(self):
        self.settings(compress_format="gz", compress_dir=str(self.root / "elsewhere"))
        with mock.patch.object(gui, "run_job", side_effect=self.immediate_job), \
                mock.patch.object(gui, "writable_target", side_effect=lambda parent, target, *args: target):
            self.win.new_archive([str(self.source)], standalone=True, ask=False)
            self.assertTrue((self.root / "elsewhere" / "source.txt.gz").is_file())
            promised = self.root / "promised.zip"
            self.win.new_archive([str(self.source), str(self.archive)], standalone=True, ask=False, out=str(promised))
        with zipfile.ZipFile(promised) as z:
            self.assertEqual(set(z.namelist()), {"source.txt", "input.zip"})

    def test_extract_dialog_saved_defaults(self):
        self.settings(extract_dir=str(self.root / "preferred"), extract_mode="rename", extract_smart=False)
        d = gui.ExtractDialog(self.win, str(self.root), 0)
        self.assertEqual(d.dest.text(), str(self.root / "preferred"))
        self.assertTrue(d.mode_ren.isChecked())
        self.assertFalse(d.smart.isChecked())
        d.close()
        self.settings(extract_mode="ask")
        d = gui.ExtractDialog(self.win, str(self.root), 0)
        self.assertTrue(d.mode_ask.isChecked())
        d.close()

    def test_ask_cancel_does_not_start_job_or_write_any_member(self):
        dest = self.root / "dest"
        dest.mkdir()
        (dest / "a.txt").write_bytes(b"original")
        def cancel(collisions):
            self.assertEqual(QThread.currentThread(), self.app.thread())
            self.assertEqual(collisions, [str(dest / "a.txt")])
            self.assertEqual((dest / "a.txt").read_bytes(), b"original")
            self.assertFalse((dest / "b.txt").exists())
            return None
        with mock.patch.object(self.win, "_collision_mode", side_effect=cancel), \
                mock.patch.object(gui, "run_job") as job:
            self.assertFalse(self.win._do_extract(str(dest), None, "ask"))
            job.assert_not_called()
        self.assertEqual((dest / "a.txt").read_bytes(), b"original")
        self.assertFalse((dest / "b.txt").exists())

    def test_ask_modes_apply_to_real_zip_and_keep_existing_modes(self):
        for mode in ("overwrite", "skip", "rename"):
            for ask in (False, True):
                with self.subTest(mode=mode, ask=ask):
                    dest = self.root / f"{mode}-{ask}"
                    dest.mkdir()
                    (dest / "a.txt").write_bytes(b"original")
                    with mock.patch.object(self.win, "_collision_mode", return_value=mode) as prompt, \
                            mock.patch.object(gui, "run_job", side_effect=self.immediate_job):
                        self.assertTrue(self.win._do_extract(str(dest), None, "ask" if ask else mode))
                    self.assertEqual(prompt.call_count, int(ask))
                    self.assertEqual((dest / "a.txt").read_bytes(), b"new" if mode == "overwrite" else b"original")
                    self.assertEqual((dest / "b.txt").read_bytes(), b"other")
                    self.assertEqual((dest / "a (2).txt").exists(), mode == "rename")

    def test_ask_actual_worker_prompts_before_write_and_skips_prompt_without_collision(self):
        dest = self.root / "worker"
        dest.mkdir()
        (dest / "a.txt").write_bytes(b"original")
        def choose(collisions):
            self.assertEqual(QThread.currentThread(), self.app.thread())
            self.assertEqual((dest / "a.txt").read_bytes(), b"original")
            self.assertFalse((dest / "b.txt").exists())
            return "rename"
        with mock.patch.object(self.win, "_collision_mode", side_effect=choose) as prompt:
            self.assertTrue(self.win._do_extract(str(dest), None, "ask"))
            prompt.assert_called_once()
        self.assertEqual((dest / "a.txt").read_bytes(), b"original")
        self.assertEqual((dest / "a (2).txt").read_bytes(), b"new")
        self.assertEqual((dest / "b.txt").read_bytes(), b"other")
        fresh = self.root / "fresh-worker"
        with mock.patch.object(self.win, "_collision_mode") as prompt:
            self.assertTrue(self.win._do_extract(str(fresh), None, "ask"))
            prompt.assert_not_called()
        self.assertEqual((fresh / "a.txt").read_bytes(), b"new")

    def test_collision_scan_uses_selection_and_safe_paths(self):
        dest = self.root / "dest"
        dest.mkdir()
        (dest / "a.txt").write_bytes(b"original")
        with mock.patch.object(gui.ziplib, "safe_target", wraps=gui.ziplib.safe_target) as safe:
            self.assertEqual(self.win._extract_collisions(str(dest), ["b.txt"]), [])
            self.assertEqual(safe.call_args_list, [mock.call(os.path.realpath(dest), "b.txt")])
        with mock.patch.object(gui.ziplib, "safe_target", return_value=None), \
                mock.patch.object(self.win, "_collision_mode") as prompt, \
                mock.patch.object(gui, "run_job", side_effect=self.immediate_job):
            self.assertTrue(self.win._do_extract(str(dest), None, "ask"))
            prompt.assert_not_called()
        self.assertEqual((dest / "a.txt").read_bytes(), b"original")
        self.assertFalse((dest / "b.txt").exists())

    def test_collision_choice_buttons_and_safe_default(self):
        for label, expected in (("덮어쓰기", "overwrite"), ("건너뛰기", "skip"), ("이름변경", "rename"), ("취소", None)):
            checks = []
            def click():
                box = QApplication.activeModalWidget()
                checks.append(isinstance(box, QMessageBox) and box.defaultButton().text() == "취소")
                if isinstance(box, QMessageBox):
                    next(b for b in box.buttons() if b.text() == label).click()
            QTimer.singleShot(30, click)
            self.assertEqual(self.win._collision_mode(["a.txt"]), expected)
            self.assertEqual(checks, [True])

    def test_gui_and_quick_extract_open_folder_toggle(self):
        for on in (False, True):
            self.settings(extract_smart=False, extract_open_folder=on)
            with mock.patch.object(gui.ExtractDialog, "exec", return_value=QDialog.Accepted), \
                    mock.patch.object(self.win, "_do_extract", return_value=True), \
                    mock.patch.object(gui, "open_path") as opened:
                self.win.extract()
                self.assertEqual(opened.call_count, int(on))
            with mock.patch.object(self.win, "_do_extract", return_value=True) as extract, \
                    mock.patch.object(gui, "writable_target", return_value=str(self.root)), \
                    mock.patch.object(gui, "open_path") as opened:
                gui.quick_extract(self.win, str(self.archive), False)
                self.assertEqual(extract.call_args.args[2], "rename")
                self.assertEqual(opened.call_count, int(on))

    def test_update_history_all_results_both_modes_and_network_log(self):
        for manual in (False, True):
            for release, error in ((None, None), (SimpleNamespace(version="9.0.0"), None), (None, OSError("offline"))):
                with self.subTest(manual=manual, release=release, error=error):
                    with mock.patch.object(QMessageBox, "information"), mock.patch.object(QMessageBox, "warning"), \
                            mock.patch.object(QMessageBox, "question", return_value=QMessageBox.No), \
                            mock.patch.object(gui, "log_exception") as log:
                        self.win._update_result(release, error, manual)
                    settings = appcore.load_settings()
                    self.assertIsNotNone(datetime.fromisoformat(settings["update_last_checked"]).tzinfo)
                    self.assertEqual(settings["update_last_result"], self.win.status.currentMessage())
                    self.assertEqual(log.call_count, int(error is not None))
                    if error:
                        self.assertIn("offline", settings["update_last_result"])
                    # Real preferences history labels must accept the persisted format.
                    d = PreferencesDialog(self.win)
                    self.assertEqual(d.last_checked_label.text(), settings["update_last_checked"])
                    d.close()

    def test_app_applies_runtime_and_appearance(self):
        with mock.patch.object(appcore, "apply_runtime_settings") as runtime, \
                mock.patch.object(appearance, "apply_appearance") as visual:
            self.assertIs(gui._app(), self.app)
        runtime.assert_called_once_with(strict=False)
        visual.assert_called_once_with(self.app)

    def test_attach_quick_compress_opens_actual_output_folder_only_when_enabled(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                output = self.root / f"attached-{enabled}"
                self.settings(compress_open_folder=enabled, compress_dir=str(output), compress_format="gz")
                with mock.patch.object(appcore, "apply_runtime_settings", return_value=appcore.load_settings()):
                    job = appcore.quick_compress([str(self.source)], str(self.root / "shell-name.zip"))
                self.addCleanup(job.wait, 10)
                self.assertTrue(job.reveal)  # The manual folder button remains available with auto-open off.
                with mock.patch.object(gui, "_app", return_value=self.app), \
                        mock.patch.object(gui, "MIN_SHOWN", 0), \
                        mock.patch.object(gui, "open_path") as opened:
                    self.assertEqual(gui.attach_job(job), 0)
                self.assertTrue(job.ok, job.result)
                self.assertEqual(Path(job.result[0]).parent, output)
                with zipfile.ZipFile(job.result[0]) as archive:
                    self.assertEqual(archive.read("source.txt"), self.source.read_bytes())
                if enabled:
                    opened.assert_called_once_with(str(output))
                else:
                    opened.assert_not_called()

    def test_attach_job_does_not_open_folder_after_failure_cancel_or_empty_result(self):
        self.settings(compress_open_folder=True)
        job = SimpleNamespace(title="압축", reveal=str(self.archive), done_text="완료")
        for ok, result in ((False, ("cancel", "")), (False, ("error", "failed")), (True, [])):
            with self.subTest(ok=ok, result=result):
                with mock.patch.object(gui, "_app", return_value=self.app), \
                        mock.patch.object(gui, "run_job", return_value=(ok, result)), \
                        mock.patch.object(gui, "open_path") as opened:
                    self.assertEqual(gui.attach_job(job), 0)
                    opened.assert_not_called()

    def test_startup_bad_temp_falls_back_without_rewriting_saved_preference(self):
        preferred = str(self.root / "unavailable-drive")
        self.settings(temp_dir=preferred, worker_threads=2)
        for error in (FileNotFoundError("drive missing"), PermissionError("temp denied")):
            with self.subTest(error=type(error).__name__):
                def configure(worker_threads, temp_dir):
                    if temp_dir == preferred:
                        raise error
                    self.assertEqual(temp_dir, "")
                    return str(self.root / "system-temp")
                with mock.patch.object(appcore.runtimeconfig, "configure", side_effect=configure) as runtime, \
                        mock.patch.object(appcore, "set_low_priority"), \
                        mock.patch.object(appcore, "log_exception") as log, \
                        mock.patch.object(appearance, "apply_appearance") as visual:
                    self.assertIs(gui._app(), self.app)
                self.assertEqual(runtime.call_count, 2)
                self.assertTrue(log.called)
                visual.assert_called_once_with(self.app)
                self.assertEqual(appcore.load_settings()["temp_dir"], preferred)

    def test_preferences_runtime_apply_keeps_strict_temp_validation(self):
        dlg = mock.Mock()
        dlg.exec.return_value = QDialog.Accepted
        dlg.apply.return_value = True
        with mock.patch.object(gui, "SettingsDialog", return_value=dlg), \
                mock.patch.object(appcore, "apply_runtime_settings", side_effect=PermissionError("temp denied")) as runtime, \
                mock.patch.object(appearance, "apply_appearance"), \
                mock.patch.object(gui, "log_exception") as log:
            self.win.preferences()
        runtime.assert_called_once_with()
        log.assert_called_once()
        gui.shell_register.assert_not_called()
        gui.shell_unregister.assert_not_called()


if __name__ == "__main__":
    unittest.main()
