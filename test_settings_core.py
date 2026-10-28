"""Settings/core regressions without Qt or changes to real file associations."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch
import zipfile

import appcore
import assoc
import formats
import lzmamt
import runtimeconfig
import ziplib
import zipwriter


class SettingsCoreTests(unittest.TestCase):
    def setUp(self):
        self.previous_temp = tempfile.tempdir
        self.previous_threads = runtimeconfig._threads
        self.previous_roots = set(runtimeconfig._sessions)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = self.root / "settings.json"
        self.env = patch.dict(os.environ, YZIP_SETTINGS=str(self.settings))
        self.env.start()

    def tearDown(self):
        tempfile.tempdir = self.previous_temp
        runtimeconfig._threads = self.previous_threads
        for root in set(runtimeconfig._sessions) - self.previous_roots:
            runtimeconfig._sessions.pop(root)[1].close()
        self.env.stop()
        self.tmp.cleanup()

    def configure(self, **values):
        return appcore.apply_runtime_settings({"temp_dir": str(self.root), **values})

    def source(self, name="memo.txt"):
        path = self.root / name
        path.write_bytes(b"hello world " * 2000)
        return str(path)

    def test_defaults_unknown_keys_and_invalid_disk_values(self):
        self.assertEqual(appcore.load_settings(), appcore.DEFAULTS)
        self.settings.write_text(json.dumps({"update": {"etag": "123"}, "theme": "bad",
                                              "worker_threads": True}), encoding="utf-8")
        s = appcore.load_settings()
        self.assertEqual(s["update"], {"etag": "123"})
        self.assertEqual(s["theme"], "system")
        self.assertEqual(s["worker_threads"], 0)
        appcore.save_settings({"theme": "dark", "keep_open": True})
        self.assertEqual(appcore.load_settings()["update"], {"etag": "123"})
        self.assertTrue(appcore.load_settings()["keep_open"])

    def test_strict_validation_never_touches_file(self):
        appcore.save_settings({"theme": "dark"})
        original = self.settings.read_bytes()
        invalid = {"language": "fr", "theme": None, "font_family": 1, "font_size": 0,
                   "compress_format": "rar", "compress_level": 10, "worker_threads": True,
                   "extract_mode": "delete", "shell_multi": "all", "compress_split": "1KB",
                   "temp_dir": "x\0", "extract_smart": 1, "auto_update": "false"}
        for key, value in invalid.items():
            with self.subTest(key=key), self.assertRaises(ValueError):
                appcore.save_settings({key: value})
            self.assertEqual(self.settings.read_bytes(), original)
        for value in ([], 1, "{}"):
            with self.assertRaises(ValueError):
                appcore.save_settings(value)

    def test_atomic_replace_failure_leaves_original_and_no_partial_file(self):
        appcore.save_settings({"theme": "dark"})
        original = self.settings.read_bytes()
        with patch("appcore.os.replace", side_effect=OSError("denied")), self.assertRaises(OSError):
            appcore.save_settings({"theme": "light"})
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".yzip-settings-*")), [])

    def test_relative_path_and_concurrent_updates(self):
        with patch("appcore.settings_path", return_value=str(self.settings)):
            jobs = [threading.Thread(target=appcore.save_settings, args=({f"extra{i}": i},)) for i in range(8)]
            for job in jobs:
                job.start()
            for job in jobs:
                job.join()
        s = appcore.load_settings()
        self.assertTrue(all(s[f"extra{i}"] == i for i in range(8)))
        previous = os.getcwd()
        try:
            os.chdir(self.root)
            with patch("appcore.settings_path", return_value="settings.json"):
                appcore.save_settings({"language": "en"})
        finally:
            os.chdir(previous)
        self.assertEqual(appcore.load_settings()["language"], "en")

    def test_every_format_is_valid_and_boundaries(self):
        for fmt in formats.FORMATS:
            self.assertEqual(appcore.normalize_settings({"compress_format": fmt.id}, strict=True)["compress_format"], fmt.id)
        for value in (0, 9):
            self.assertEqual(appcore.normalize_settings({"compress_level": value}, strict=True)["compress_level"], value)
        self.assertEqual(appcore.split_bytes("분할 안 함"), 0)
        self.assertEqual(appcore.split_bytes("1.5MB"), 1572864)

    def test_runtime_initialization_and_failed_reconfigure(self):
        self.configure(worker_threads=1)
        session = Path(tempfile.gettempdir())
        self.assertEqual(session.parent, self.root.resolve() / "yzip-temp")
        with tempfile.NamedTemporaryFile() as file:
            self.assertEqual(Path(file.name).parent, session)
        bad = self.root / "file"
        bad.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(OSError):
            self.configure(worker_threads=2, temp_dir=str(bad))
        self.assertEqual(tempfile.gettempdir(), str(session))
        self.assertEqual(runtimeconfig.worker_count(), 1)

    def test_validate_runtime_probes_without_applying(self):
        self.configure(worker_threads=1)
        before = (tempfile.tempdir, runtimeconfig._threads, dict(runtimeconfig._sessions))
        custom = self.root / "new-temp"
        with patch("appcore.set_low_priority") as priority:
            values = appcore.validate_runtime_settings({"worker_threads": 2, "temp_dir": str(custom)})
            priority.assert_not_called()
        self.assertEqual(values["worker_threads"], 2)
        self.assertEqual((tempfile.tempdir, runtimeconfig._threads, runtimeconfig._sessions), before)
        self.assertEqual(list((custom / "yzip-temp").iterdir()), [])
        bad = self.root / "not-directory"
        bad.write_bytes(b"file")
        with self.assertRaises(OSError):
            appcore.validate_runtime_settings({"temp_dir": str(bad)})
        with self.assertRaises(ValueError):
            appcore.validate_runtime_settings({"worker_threads": True})

    def test_startup_fallback_logs_and_preserves_saved_settings(self):
        bad = self.root / "not-directory"
        bad.write_bytes(b"file")
        appcore.save_settings({"temp_dir": str(bad), "worker_threads": 1})
        before = self.settings.read_bytes()
        with self.assertRaises(OSError):
            appcore.apply_runtime_settings()
        with patch("appcore.log_exception") as log, patch.object(runtimeconfig, "_system_temp", str(self.root / "system")):
            applied = appcore.apply_runtime_settings(strict=False)
            log.assert_called_once()
        self.assertEqual(applied["temp_dir"], str(bad))
        self.assertTrue(Path(tempfile.gettempdir()).is_relative_to(self.root.resolve() / "system"))
        self.assertEqual(runtimeconfig.worker_count(), 1)
        self.assertEqual(self.settings.read_bytes(), before)

    def test_cleanup_only_abandoned_marked_sessions(self):
        self.configure()
        active = Path(tempfile.gettempdir())
        (active / "active.txt").write_text("active", encoding="utf-8")
        outside = self.root / "untouched.txt"
        outside.write_text("keep", encoding="utf-8")
        root = active.parent
        unknown = root / ("session-" + "a" * 32)
        unknown.mkdir()
        (unknown / ".yzip-owner").write_bytes(b"not ours")
        script = "import runtimeconfig,sys; print(runtimeconfig.configure(temp_dir=sys.argv[1]),flush=True)"
        result = subprocess.run([sys.executable, "-c", script, str(self.root)], capture_output=True,
                                text=True, encoding="utf-8", check=True)
        stale = Path(result.stdout.strip())
        (stale / "garbage").write_bytes(b"old")
        candidates = appcore.cleanup_temp(dry_run=True)
        self.assertEqual(candidates, [str(stale)])
        self.assertTrue(stale.exists())
        self.assertEqual(appcore.cleanup_temp(paths=[])["removed"], 0)
        self.assertEqual(appcore.cleanup_temp(paths=[str(outside)])["removed"], 0)
        report = appcore.cleanup_temp(paths=candidates)
        self.assertEqual(report["removed"], 1)
        self.assertFalse(stale.exists())
        self.assertTrue((active / "active.txt").exists())
        self.assertTrue(unknown.exists())
        self.assertTrue(outside.exists())

    def test_cleanup_skips_other_live_process(self):
        self.configure()
        script = ("import runtimeconfig,sys; print(runtimeconfig.configure(temp_dir=sys.argv[1]),flush=True); "
                  "sys.stdin.readline()")
        with subprocess.Popen([sys.executable, "-c", script, str(self.root)], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True, encoding="utf-8") as process:
            active = Path(process.stdout.readline().strip())
            try:
                self.assertEqual(appcore.cleanup_temp()["removed"], 0)
                self.assertTrue(active.exists())
            finally:
                process.communicate("done\n", timeout=10)
        self.assertEqual(appcore.cleanup_temp()["removed"], 1)

    def test_cleanup_rechecks_candidate_lease_and_rejects_redirected_tree(self):
        self.configure()
        root = Path(tempfile.gettempdir()).parent
        stale = root / ("session-" + "b" * 32)
        stale.mkdir()
        marker = stale / ".yzip-owner"
        marker.write_bytes(runtimeconfig._MARKER)
        candidates = appcore.cleanup_temp(dry_run=True)
        self.assertEqual(candidates, [str(stale)])
        with open(marker, "r+b") as owner:
            runtimeconfig._lease(owner)
            self.assertEqual(appcore.cleanup_temp(paths=candidates)["removed"], 0)
        child = stale / "redirect"
        child.mkdir()
        original = runtimeconfig._redirect
        with patch("runtimeconfig._redirect", side_effect=lambda p: str(p) == str(child) or original(p)):
            self.assertEqual(appcore.cleanup_temp(dry_run=True), [])
            self.assertEqual(appcore.cleanup_temp(paths=candidates)["removed"], 0)
        self.assertTrue(stale.exists())

    def test_main_applies_defaults_and_keeps_dialog_action(self):
        import yzip_main
        src = self.source()
        listing = self.root / "list.txt"
        listing.write_text(src + "\n", encoding="utf-8")
        appcore.save_settings({"temp_dir": str(self.root), "compress_format": "tgz",
                               "compress_dir": str(self.root / "output")})
        attached = []
        fake = types.SimpleNamespace(attach_job=lambda job: attached.append(job), finish=lambda code: None,
                                     main=lambda: 0)
        with patch.dict(sys.modules, yzip_gui=fake), patch.object(sys, "argv", [
                "yzip", "--archive-list", str(listing), "--out", str(self.root / "memo.zip")]):
            self.assertEqual(yzip_main.main(), 0)
        self.assertEqual(len(attached), 1)
        self.assertTrue(attached[0].wait(10))
        self.assertTrue(attached[0].ok, attached[0].result)
        self.assertTrue(zipfile.is_zipfile(self.root / "output" / "memo.zip"))
        self.assertFalse((self.root / "output" / "memo.tar.gz").exists())
        listing.write_text(src + "\n", encoding="utf-8")
        with patch.dict(sys.modules, yzip_gui=fake), patch.object(sys, "argv", [
                "yzip", "--compress-dialog-list", str(listing)]):
            self.assertEqual(yzip_main.main(), 0)
        self.assertTrue(listing.exists())
        self.assertEqual(len(attached), 1)

    def test_engine_worker_limits_are_used(self):
        self.configure(worker_threads=1)
        src = self.source()
        for module, invoke in (
            (zipwriter, lambda: zipwriter.write_zip([(src, "memo.txt")], str(self.root / "a.zip"), workers=8)),
            (lzmamt, lambda: formats.create("xz", [src], str(self.root / "a.xz"))),
        ):
            real = module.ThreadPoolExecutor
            with patch.object(module, "ThreadPoolExecutor", wraps=real) as pool:
                invoke()
                self.assertTrue(pool.called)
                self.assertEqual(pool.call_args.args[0], 1)
        self.assertEqual(lzmamt.plan(6, 10**9)[1], 1)
        if formats.zstd:
            self.assertEqual(formats._zstd_options(6)[formats.zstd.CompressionParameter.nb_workers], 1)
        archive = self.root / "a.zip"
        real = ziplib.ThreadPoolExecutor
        with patch.object(ziplib, "ThreadPoolExecutor", wraps=real) as pool, ziplib.Archive(str(archive)) as z:
            z.extract(str(self.root / "extract"))
            self.assertTrue(pool.called)
            self.assertEqual(pool.call_args.args[0], 1)
        self.assertEqual((self.root / "extract" / "memo.txt").read_bytes(), Path(src).read_bytes())

    def test_quick_defaults_and_separate_outputs(self):
        one = self.source("same.txt")
        folder = self.root / "other"
        folder.mkdir()
        two = folder / "same.txt"
        two.write_bytes(b"second")
        dest = self.root / "output"
        appcore.save_settings({"temp_dir": str(self.root), "compress_dir": str(dest),
                               "shell_multi": "separate", "compress_level": 0})
        job = appcore.quick_compress([one, str(two)], str(self.root / "named.zip"))
        self.assertTrue(job.wait(20))
        self.assertTrue(job.ok, job.result)
        self.assertEqual([Path(p).name for p in job.result], ["same.zip", "same (2).zip"])
        self.assertEqual(job.reveal, str(dest))
        for path in job.result:
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(archive.infolist()[0].compress_type, zipfile.ZIP_STORED)

    def test_explicit_quick_zip_ignores_format_and_keeps_other_defaults(self):
        src = self.source()
        for fmt in ("zip", "tgz", "gz"):
            appcore.save_settings({"temp_dir": str(self.root), "compress_format": fmt,
                                   "compress_level": 3, "compress_split": "100MB", "compress_open_folder": True})
            with patch("formats.create", return_value=["made"]) as create:
                job = appcore.quick_compress([src], str(self.root / "folder.v1.zip"))
                self.assertTrue(job.wait(10))
                self.assertTrue(job.ok, job.result)
                self.assertEqual(create.call_args.args[0], "zip")
                self.assertEqual(create.call_args.kwargs["level"], 3)
                self.assertEqual(create.call_args.kwargs["split_size"], 100 << 20)
                self.assertEqual(Path(create.call_args.args[2]).name, "folder.v1.zip")
                self.assertTrue(job.reveal)

    def test_target_planning_saved_format_and_explicit_zip_separate(self):
        one = self.source("one.txt")
        two = self.source("two.txt")
        settings = {"compress_format": "tgz", "shell_multi": "separate"}
        generic = appcore.quick_targets([one, two], str(self.root / "combined.zip"), settings)
        explicit = appcore.quick_targets([one, two], str(self.root / "combined.zip"), settings,
                                         explicit_zip=True)
        self.assertEqual([Path(p).name for _, p in generic], ["one.tar.gz", "two.tar.gz"])
        self.assertEqual([Path(p).name for _, p in explicit], ["one.zip", "two.zip"])
        settings["shell_multi"] = "combined"
        self.assertEqual(Path(appcore.quick_targets([one], "folder.v1.zip", settings)[0][1]).name,
                         "folder.v1.tar.gz")

    def test_assoc_empty_selection_does_no_registry_io(self):
        with patch("assoc._set") as write, patch("assoc._get") as read, patch("assoc.notify") as notify, patch("assoc.effective") as effective:
            assoc.register([])
            assoc.unregister([])
            self.assertTrue(assoc.enabled([]))
            self.assertEqual(assoc.status([]), {})
            for mocked in (write, read, notify, effective):
                mocked.assert_not_called()
        self.assertEqual(set(assoc.TYPES), set(formats.READ_EXTS) - {".exe"})
        with self.assertRaises(ValueError):
            assoc.register([".exe"])

    def test_assoc_subset_changes_only_selected_extension(self):
        with patch("assoc._set") as write, patch("assoc._get", return_value=None), patch("assoc.notify"):
            assoc.register([".xz"], exe="test.exe")
            paths = [call.args[0] for call in write.call_args_list]
            self.assertTrue(any("yzip.xz" in path for path in paths))
            self.assertFalse(any("yzip.zip" in path for path in paths))
        with patch("assoc._del_tree") as delete, patch("assoc._get", return_value=None), patch("assoc._del_value"), patch("assoc.notify"):
            assoc.unregister([".xz"])
            delete.assert_called_once_with(r"Software\Classes\yzip.xz")


if __name__ == "__main__":
    unittest.main()
