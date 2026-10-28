import ctypes
import os
import tempfile
import threading
import time
import unittest
import warnings
import zipfile
import zlib
from pathlib import Path
from unittest import mock

import archive_accel
import ziplib


class FakeFunction:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class BindingLibrary:
    def __init__(self):
        self.captured_size = None
        self.extract_calls = 0
        self.yz_accel_abi_version = FakeFunction(lambda: archive_accel.ABI_VERSION)
        self.yz_accel_capabilities = FakeFunction(lambda: archive_accel.CAP_ZIP_EXTRACT)
        self.yz_accel_last_error = FakeFunction(lambda _buffer, _size: 1)
        self.yz_accel_cancel = FakeFunction(
            lambda pointer: setattr(
                ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32)).contents,
                "value",
                1,
            )
        )

        def extract_zip(_dll, _source, jobs, count, _limit, progress, _cancel):
            self.extract_calls += 1
            self.captured_size = jobs[0].expected_size
            progress.contents.completed_bytes = jobs[0].expected_size
            return archive_accel.AccelStatus.OK

        self.yz_accel_extract_zip = FakeFunction(extract_zip)


class NativeBindingTests(unittest.TestCase):
    def tearDown(self):
        archive_accel._reset_for_tests()

    def test_capability_without_operation_export_disables_accelerator(self):
        library = BindingLibrary()
        del library.yz_accel_extract_zip
        with mock.patch.object(archive_accel.ctypes, "WinDLL", return_value=library):
            accelerator = archive_accel.load_accelerator(Path("broken-bridge.dll"))

        self.assertIsNone(accelerator)
        self.assertIn("yz_accel_extract_zip", archive_accel.disabled_reason())

    def test_job_size_is_64_bit_and_zero_byte_job_is_preserved(self):
        library = BindingLibrary()
        accelerator = archive_accel.Accelerator(library, Path("bridge.dll"))
        huge = (1 << 32) + 17
        jobs = [
            archive_accel.NativeZipJob(3, "stage.bin", huge, 0x12345678),
            archive_accel.NativeZipJob(4, "empty.bin", 0, 0),
        ]

        result = accelerator.extract_zip("source.zip", jobs, None, None)

        self.assertEqual(archive_accel.AccelStatus.OK, result.status)
        self.assertEqual(huge, result.completed_bytes)
        self.assertEqual(huge, library.captured_size)
        self.assertIs(ctypes.c_uint64, library.yz_accel_extract_zip.argtypes[4])

    def test_runtime_unavailable_and_io_error_have_distinct_status_codes(self):
        self.assertNotEqual(
            archive_accel.AccelStatus.UNAVAILABLE,
            archive_accel.AccelStatus.IO_ERROR,
        )
        self.assertNotEqual(
            archive_accel.AccelStatus.UNAVAILABLE,
            archive_accel.AccelStatus.CORRUPT,
        )

    def test_missing_or_hash_mismatched_runtime_7z_is_unavailable_before_native_call(self):
        for payload in (None, b"not the pinned 7z dll"):
            with self.subTest(payload=payload):
                library = BindingLibrary()
                accelerator = archive_accel.Accelerator(library, Path("bridge.dll"))
                with tempfile.TemporaryDirectory() as root:
                    runtime_dll = Path(root, "7z.dll")
                    if payload is not None:
                        runtime_dll.write_bytes(payload)
                    with mock.patch.dict(os.environ, {"YZIP_7Z_DLL": str(runtime_dll)}):
                        result = accelerator.extract_zip(
                            "source.zip",
                            [archive_accel.NativeZipJob(0, "stage.bin", 0, 0)],
                            None,
                            None,
                        )

                self.assertEqual(archive_accel.AccelStatus.UNAVAILABLE, result.status)
                self.assertEqual(0, library.extract_calls)


class RecordingAccelerator:
    capabilities = archive_accel.CAP_ZIP_EXTRACT

    def __init__(self, status=archive_accel.AccelStatus.OK, corrupt=False):
        self.status = status
        self.corrupt = corrupt
        self.calls = []

    def extract_zip(self, source, jobs, progress, cancel):
        self.calls.append((source, list(jobs)))
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            for job in jobs:
                os.makedirs(os.path.dirname(job.staging_path), exist_ok=True)
                data = archive.read(infos[job.source_index])
                if self.corrupt and data:
                    data = b"corrupt" + data[7:]
                with open(job.staging_path, "wb") as output:
                    output.write(data)
        completed = sum(job.expected_size for job in jobs)
        if progress is not None:
            progress(completed, completed, "")
        return archive_accel.NativeResult(self.status, completed)


class NativeZipTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = self.temp.name

    def tearDown(self):
        self.temp.cleanup()

    def make_zip(self, members, compression=zipfile.ZIP_DEFLATED):
        path = os.path.join(self.root, "input.zip")
        with zipfile.ZipFile(path, "w", compression) as archive:
            for name, data in members:
                archive.writestr(name, data)
        return path

    def extract_with(self, path, accelerator, *, enable_native=True, **kwargs):
        destination = os.path.join(self.root, "out")
        password = kwargs.pop("password", None)
        environment = {"YZIP_NATIVE_ZIP_EXTRACT": "1"} if enable_native else {}
        with mock.patch.dict(os.environ, environment, clear=False), \
                mock.patch("ziplib.load_accelerator", return_value=accelerator):
            if not enable_native:
                os.environ.pop("YZIP_NATIVE_ZIP_EXTRACT", None)
            with ziplib.Archive(path, password) as archive:
                archive.extract(destination, **kwargs)
        return destination

    def assert_no_staging_files(self, destination):
        leftovers = [
            name
            for directory, _subdirs, files in os.walk(destination)
            for name in files
            if name.endswith((".part", ".native-stage", ".native-backup"))
        ]
        self.assertEqual([], leftovers)

    def test_plain_deflate_archive_uses_native_and_publishes_after_validation(self):
        path = self.make_zip([("a.txt", b"alpha"), ("empty.bin", b"")])
        accelerator = RecordingAccelerator()

        destination = self.extract_with(path, accelerator)

        self.assertEqual(1, len(accelerator.calls))
        self.assertEqual(b"alpha", Path(destination, "a.txt").read_bytes())
        self.assertEqual(b"", Path(destination, "empty.bin").read_bytes())
        self.assert_no_staging_files(destination)

    def test_native_path_remains_disabled_by_default_after_failed_benchmark(self):
        path = self.make_zip([("a.txt", b"alpha")])
        accelerator = RecordingAccelerator()

        destination = self.extract_with(path, accelerator, enable_native=False)

        self.assertEqual([], accelerator.calls)
        self.assertEqual(b"alpha", Path(destination, "a.txt").read_bytes())

    def test_many_small_plain_members_use_faster_python_engine_when_native_is_enabled(self):
        members = [(f"f{index:03d}.txt", f"payload-{index}".encode()) for index in range(256)]
        path = self.make_zip(members)
        accelerator = RecordingAccelerator()

        destination = self.extract_with(path, accelerator)

        self.assertEqual([], accelerator.calls)
        for name, payload in members:
            self.assertEqual(payload, Path(destination, name).read_bytes())

    def test_store_and_deflate_mixed_archive_falls_back_for_whole_operation(self):
        path = os.path.join(self.root, "mixed.zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("stored.txt", b"stored", compress_type=zipfile.ZIP_STORED)
            archive.writestr("deflated.txt", b"deflated", compress_type=zipfile.ZIP_DEFLATED)
        accelerator = RecordingAccelerator()

        destination = self.extract_with(path, accelerator)

        self.assertEqual([], accelerator.calls)
        self.assertEqual(b"stored", Path(destination, "stored.txt").read_bytes())
        self.assertEqual(b"deflated", Path(destination, "deflated.txt").read_bytes())

    def test_unsupported_method_falls_back_for_whole_operation(self):
        path = self.make_zip([("bzip.txt", b"payload")], zipfile.ZIP_BZIP2)
        accelerator = RecordingAccelerator()

        destination = self.extract_with(path, accelerator)

        self.assertEqual([], accelerator.calls)
        self.assertEqual(b"payload", Path(destination, "bzip.txt").read_bytes())

    def test_split_archive_falls_back_for_whole_operation(self):
        source = Path(self.root, "large.bin")
        source.write_bytes(os.urandom(200_000))
        archive_path = os.path.join(self.root, "split.zip")
        parts = ziplib.create_zip([str(source)], archive_path, store_only=True, split_size=50_000)
        accelerator = RecordingAccelerator()

        destination = self.extract_with(parts[0], accelerator)

        self.assertEqual([], accelerator.calls)
        self.assertEqual(source.read_bytes(), Path(destination, "large.bin").read_bytes())

    def test_aes_archive_falls_back_for_whole_operation(self):
        source = Path(self.root, "secret.txt")
        source.write_bytes(b"secret")
        archive_path = os.path.join(self.root, "aes.zip")
        ziplib.create_zip([str(source)], archive_path, password="pw")
        accelerator = RecordingAccelerator()

        destination = self.extract_with(archive_path, accelerator, password="pw")

        self.assertEqual([], accelerator.calls)
        self.assertEqual(b"secret", Path(destination, "secret.txt").read_bytes())

    def test_cancelled_native_operation_preserves_existing_output(self):
        path = self.make_zip([("report.txt", b"new version")])
        destination = Path(self.root, "out")
        destination.mkdir()
        target = destination / "report.txt"
        target.write_bytes(b"old version")
        accelerator = RecordingAccelerator(archive_accel.AccelStatus.CANCELLED)

        with self.assertRaises(ziplib.Cancelled):
            self.extract_with(path, accelerator)

        self.assertEqual(b"old version", target.read_bytes())
        self.assert_no_staging_files(str(destination))

    def test_native_error_preserves_existing_output(self):
        path = self.make_zip([("report.txt", b"new version")])
        destination = Path(self.root, "out")
        destination.mkdir()
        target = destination / "report.txt"
        target.write_bytes(b"old version")
        accelerator = RecordingAccelerator(archive_accel.AccelStatus.ERROR)

        with self.assertRaises(ziplib.NativeExtractionError):
            self.extract_with(path, accelerator)

        self.assertEqual(b"old version", target.read_bytes())
        self.assert_no_staging_files(str(destination))

    def test_runtime_unavailable_falls_back_for_whole_archive_before_publish(self):
        path = self.make_zip([("a.txt", b"alpha"), ("b.txt", b"beta")])
        accelerator = RecordingAccelerator(archive_accel.AccelStatus.UNAVAILABLE)

        destination = self.extract_with(path, accelerator)

        self.assertEqual(1, len(accelerator.calls))
        self.assertEqual(b"alpha", Path(destination, "a.txt").read_bytes())
        self.assertEqual(b"beta", Path(destination, "b.txt").read_bytes())
        self.assert_no_staging_files(destination)

    def test_native_io_error_does_not_fallback_and_preserves_existing_output(self):
        path = self.make_zip([("report.txt", b"new version")])
        destination = Path(self.root, "out")
        destination.mkdir()
        target = destination / "report.txt"
        target.write_bytes(b"old version")
        accelerator = RecordingAccelerator(archive_accel.AccelStatus.IO_ERROR)

        with self.assertRaises(OSError):
            self.extract_with(path, accelerator)

        self.assertEqual(b"old version", target.read_bytes())
        self.assert_no_staging_files(str(destination))

    def test_python_crc_verification_rejects_corrupt_native_output(self):
        data = b"correct payload"
        path = self.make_zip([("report.txt", data)])
        destination = Path(self.root, "out")
        destination.mkdir()
        target = destination / "report.txt"
        target.write_bytes(b"old version")
        accelerator = RecordingAccelerator(corrupt=True)

        with self.assertRaises(zipfile.BadZipFile):
            self.extract_with(path, accelerator)

        self.assertEqual(b"old version", target.read_bytes())
        self.assert_no_staging_files(str(destination))

    def test_cancel_during_python_revalidation_publishes_nothing(self):
        path = self.make_zip([("a.bin", b"a" * 1000), ("b.bin", b"b" * 1000)])
        destination = Path(self.root, "out")
        destination.mkdir()
        (destination / "a.bin").write_bytes(b"old a")
        (destination / "b.bin").write_bytes(b"old b")
        cancelled = threading.Event()
        real_crc32 = zlib.crc32

        def crc_then_cancel(data, value=0):
            result = real_crc32(data, value)
            cancelled.set()
            return result

        with mock.patch.object(ziplib.zlib, "crc32", side_effect=crc_then_cancel):
            with self.assertRaises(ziplib.Cancelled):
                self.extract_with(path, RecordingAccelerator(), cancel=cancelled.is_set)

        self.assertEqual(b"old a", (destination / "a.bin").read_bytes())
        self.assertEqual(b"old b", (destination / "b.bin").read_bytes())
        self.assert_no_staging_files(str(destination))

    def test_cancel_between_file_publishes_rolls_back_every_existing_output(self):
        path = self.make_zip([("a.bin", b"new a"), ("b.bin", b"new b")])
        destination = Path(self.root, "out")
        destination.mkdir()
        (destination / "a.bin").write_bytes(b"old a")
        (destination / "b.bin").write_bytes(b"old b")
        cancelled = threading.Event()
        real_finish = ziplib.finish_part
        calls = {"count": 0}

        def finish_then_cancel(part, target):
            real_finish(part, target)
            calls["count"] += 1
            if calls["count"] == 1:
                cancelled.set()

        with mock.patch.object(ziplib, "finish_part", side_effect=finish_then_cancel):
            with self.assertRaises(ziplib.Cancelled):
                self.extract_with(path, RecordingAccelerator(), cancel=cancelled.is_set)

        self.assertEqual(b"old a", (destination / "a.bin").read_bytes())
        self.assertEqual(b"old b", (destination / "b.bin").read_bytes())
        self.assert_no_staging_files(str(destination))

    def test_second_finish_failure_removes_new_files_and_restores_all_backups(self):
        path = self.make_zip([("new.bin", b"new"), ("a.bin", b"new a"), ("b.bin", b"new b")])
        destination = Path(self.root, "out")
        destination.mkdir()
        (destination / "a.bin").write_bytes(b"old a")
        (destination / "b.bin").write_bytes(b"old b")
        real_finish = ziplib.finish_part
        calls = {"count": 0}

        def fail_second(part, target):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("injected publish failure")
            real_finish(part, target)

        with mock.patch.object(ziplib, "finish_part", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "injected publish failure"):
                self.extract_with(path, RecordingAccelerator())

        self.assertFalse((destination / "new.bin").exists())
        self.assertEqual(b"old a", (destination / "a.bin").read_bytes())
        self.assertEqual(b"old b", (destination / "b.bin").read_bytes())
        self.assert_no_staging_files(str(destination))

    def test_rollback_continues_after_remove_and_restore_failures_and_reports_backups(self):
        path = self.make_zip([("a.bin", b"new a"), ("b.bin", b"new b"), ("new.bin", b"new")])
        destination = Path(self.root, "out")
        destination.mkdir()
        (destination / "a.bin").write_bytes(b"old a")
        (destination / "b.bin").write_bytes(b"old b")
        real_finish = ziplib.finish_part
        real_remove = os.remove
        real_replace = os.replace
        publishes = {"count": 0}
        failed_backup = []

        def fail_third_publish(part, target):
            publishes["count"] += 1
            if publishes["count"] == 3:
                raise OSError("injected publish failure")
            real_finish(part, target)

        def fail_first_remove(candidate):
            if os.fspath(candidate).endswith(os.sep + "a.bin"):
                raise PermissionError("published target locked")
            real_remove(candidate)

        def fail_first_restore(source, target):
            if (os.fspath(source).endswith(".native-backup") and
                    os.fspath(target).endswith(os.sep + "a.bin")):
                failed_backup.append(os.fspath(source))
                raise PermissionError("backup restore locked")
            real_replace(source, target)

        with mock.patch.object(ziplib, "finish_part", side_effect=fail_third_publish), \
                mock.patch.object(ziplib.os, "remove", side_effect=fail_first_remove), \
                mock.patch.object(ziplib.os, "replace", side_effect=fail_first_restore):
            with self.assertRaises(ziplib.NativeRollbackError) as raised:
                self.extract_with(path, RecordingAccelerator())

        error = raised.exception
        self.assertIn("injected publish failure", str(error))
        self.assertEqual(2, len(error.failures))
        self.assertEqual(b"old b", (destination / "b.bin").read_bytes())
        self.assertEqual(b"new a", (destination / "a.bin").read_bytes())
        self.assertEqual(failed_backup, error.recoverable_backups)
        self.assertEqual(b"old a", Path(error.recoverable_backups[0]).read_bytes())

    def test_backup_delete_failure_is_nonfatal_and_warns_with_recoverable_backup(self):
        path = self.make_zip([("a.bin", b"new a")])
        destination = Path(self.root, "out")
        destination.mkdir()
        target = destination / "a.bin"
        target.write_bytes(b"old a")
        real_remove = os.remove

        def deny_backup_delete(candidate):
            if os.fspath(candidate).endswith(".native-backup"):
                raise PermissionError("backup locked")
            real_remove(candidate)

        with mock.patch.object(ziplib.os, "remove", side_effect=deny_backup_delete):
            with self.assertWarnsRegex(RuntimeWarning, "backup"):
                self.extract_with(path, RecordingAccelerator())

        self.assertEqual(b"new a", target.read_bytes())
        backups = list(destination.glob("*.native-backup"))
        self.assertEqual(1, len(backups))
        self.assertEqual(b"old a", backups[0].read_bytes())

    def test_overwrite_skip_is_initial_progress_and_total_includes_skipped_bytes(self):
        path = self.make_zip([("skip.bin", b"s" * 7), ("write.bin", b"w" * 5)])
        destination = Path(self.root, "out")
        destination.mkdir()
        (destination / "skip.bin").write_bytes(b"existing")
        updates = []

        self.extract_with(
            path,
            RecordingAccelerator(),
            overwrite=False,
            progress=lambda done, total, name: updates.append((done, total, name)),
        )

        self.assertEqual((7, 12, ""), updates[0])
        self.assertEqual((12, 12, ""), updates[-1])

    def test_parallel_revalidation_uses_worker_limit_and_finishes_before_publish(self):
        members = [(f"f{index}.bin", bytes([index]) * 1000) for index in range(4)]
        path = self.make_zip(members)
        threads = set()
        verified = {"count": 0}
        active = {"count": 0, "maximum": 0}
        lock = threading.Lock()
        real_verify = ziplib._verify_native_staging

        def observed_verify(*args, **kwargs):
            with lock:
                threads.add(threading.get_ident())
                active["count"] += 1
                active["maximum"] = max(active["maximum"], active["count"])
            try:
                time.sleep(0.05)
                return real_verify(*args, **kwargs)
            finally:
                with lock:
                    active["count"] -= 1
                    verified["count"] += 1

        def assert_all_verified(part, target):
            self.assertEqual(4, verified["count"])
            return ziplib.os.replace(part, target)

        with mock.patch.object(ziplib.runtimeconfig, "worker_count", return_value=2), \
                mock.patch.object(ziplib, "_verify_native_staging", side_effect=observed_verify), \
                mock.patch.object(ziplib, "finish_part", side_effect=assert_all_verified):
            self.extract_with(path, RecordingAccelerator())

        self.assertEqual(2, len(threads))
        self.assertEqual(2, active["maximum"])

    def test_success_path_checks_each_target_once_and_tracks_staging_state(self):
        path = self.make_zip([("a.bin", b"a"), ("b.bin", b"b")])
        real_exists = os.path.exists
        calls = []

        def observed_exists(candidate):
            calls.append(os.fspath(candidate))
            return real_exists(candidate)

        with mock.patch.object(ziplib.os.path, "exists", side_effect=observed_exists):
            self.extract_with(path, RecordingAccelerator())

        relevant = [call for call in calls if call.endswith(("a.bin", "b.bin", ".part"))]
        self.assertLessEqual(len(relevant), 2)

    def test_safe_names_case_duplicates_and_zero_byte_are_transactional(self):
        path = self.make_zip(
            [
                ("../inside.txt", b"inside"),
                ("CON", b"device"),
                ("Readme.txt", b"first"),
                ("README.TXT", b"last"),
                ("zero.bin", b""),
            ],
            zipfile.ZIP_STORED,
        )
        accelerator = RecordingAccelerator()

        destination = self.extract_with(path, accelerator)

        self.assertFalse(Path(self.root, "inside.txt").exists())
        self.assertEqual(b"inside", Path(destination, "inside.txt").read_bytes())
        self.assertEqual(b"device", Path(destination, "_CON").read_bytes())
        readmes = list(Path(destination).glob("[Rr][Ee][Aa][Dd][Mm][Ee].[Tt][Xx][Tt]"))
        self.assertEqual(1, len(readmes))
        self.assertEqual(b"last", readmes[0].read_bytes())
        self.assertEqual(b"", Path(destination, "zero.bin").read_bytes())


class BuiltNativeZipTests(unittest.TestCase):
    def load_built(self):
        dll = Path(__file__).parent / "nativeaccel" / "build" / "yzip_accel.dll"
        self.assertTrue(dll.exists(), "nativeaccel/build.ps1 must run before this test")
        accelerator = archive_accel.load_accelerator(dll)
        self.assertIsNotNone(accelerator, archive_accel.disabled_reason())
        return accelerator

    def test_built_bridge_extracts_store_and_deflate_to_supplied_staging_paths(self):
        accelerator = self.load_built()
        self.assertTrue(accelerator.capabilities & archive_accel.CAP_ZIP_EXTRACT)
        with tempfile.TemporaryDirectory() as root:
            source = Path(root, "source.zip")
            first = b"stored payload"
            second = b"deflated payload" * 100
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("first.bin", first, compress_type=zipfile.ZIP_STORED)
                archive.writestr("second.bin", second, compress_type=zipfile.ZIP_DEFLATED)
            with zipfile.ZipFile(source) as archive:
                infos = archive.infolist()
            stage1 = Path(root, "stage", "one.part")
            stage2 = Path(root, "stage", "two.part")
            stage1.parent.mkdir()
            jobs = [
                archive_accel.NativeZipJob(0, str(stage1), len(first), zlib.crc32(first)),
                archive_accel.NativeZipJob(1, str(stage2), len(second), zlib.crc32(second)),
            ]

            result = accelerator.extract_zip(str(source), jobs, None, None)

            self.assertEqual(archive_accel.AccelStatus.OK, result.status)
            self.assertEqual(first, stage1.read_bytes())
            self.assertEqual(second, stage2.read_bytes())

    def test_built_bridge_reports_staging_open_failure_as_io_error(self):
        accelerator = self.load_built()
        with tempfile.TemporaryDirectory() as root:
            source = Path(root, "source.zip")
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("data.bin", b"payload")
            staging_directory = Path(root, "stage")
            staging_directory.mkdir()
            job = archive_accel.NativeZipJob(
                0, str(staging_directory), len(b"payload"), zlib.crc32(b"payload")
            )

            result = accelerator.extract_zip(str(source), [job], None, None)

            self.assertEqual(archive_accel.AccelStatus.IO_ERROR, result.status)

    def test_built_bridge_write_failure_and_successful_short_write_are_io_errors(self):
        accelerator = self.load_built()
        for fault in ("failure", "short"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as root:
                source = Path(root, "source.zip")
                payload = b"write fault payload" * 100
                with zipfile.ZipFile(source, "w") as archive:
                    archive.writestr("data.bin", payload)
                staging = Path(root, "data.part")
                job = archive_accel.NativeZipJob(
                    0, str(staging), len(payload), zlib.crc32(payload)
                )

                with mock.patch.dict(
                    os.environ, {"YZIP_NATIVE_TEST_WRITE_FAULT": fault}, clear=False
                ):
                    result = accelerator.extract_zip(str(source), [job], None, None)

                self.assertEqual(archive_accel.AccelStatus.IO_ERROR, result.status)
                self.assertIn("I/O", accelerator.last_error())

                recovered = accelerator.extract_zip(str(source), [job], None, None)
                self.assertEqual(archive_accel.AccelStatus.OK, recovered.status)

    def test_built_bridge_extracts_nonsequential_partial_indices(self):
        accelerator = self.load_built()
        with tempfile.TemporaryDirectory() as root:
            source = Path(root, "source.zip")
            payloads = [b"zero", b"one", b"two"]
            with zipfile.ZipFile(source, "w") as archive:
                for index, payload in enumerate(payloads):
                    archive.writestr(f"{index}.bin", payload)
            stage2 = Path(root, "two.part")
            stage0 = Path(root, "zero.part")
            jobs = [
                archive_accel.NativeZipJob(2, str(stage2), len(payloads[2]), zlib.crc32(payloads[2])),
                archive_accel.NativeZipJob(0, str(stage0), len(payloads[0]), zlib.crc32(payloads[0])),
            ]

            result = accelerator.extract_zip(str(source), jobs, None, None)

            self.assertEqual(archive_accel.AccelStatus.OK, result.status)
            self.assertEqual(payloads[2], stage2.read_bytes())
            self.assertEqual(payloads[0], stage0.read_bytes())
            self.assertEqual(2, result.metrics.lookup_calls)

    def test_built_bridge_reports_callback_crc_and_write_diagnostics(self):
        accelerator = self.load_built()
        with tempfile.TemporaryDirectory() as root:
            source = Path(root, "source.zip")
            payload = b"diagnostic payload" * 10000
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("data.bin", payload)
            staging = Path(root, "data.part")
            job = archive_accel.NativeZipJob(
                0, str(staging), len(payload), zlib.crc32(payload)
            )

            result = accelerator.extract_zip(str(source), [job], None, None)

            self.assertEqual(archive_accel.AccelStatus.OK, result.status)
            self.assertEqual(1, result.metrics.callback_calls)
            self.assertEqual(1, result.metrics.lookup_calls)
            self.assertGreater(result.metrics.write_calls, 0)
            self.assertEqual(len(payload), result.metrics.write_bytes)
            self.assertGreater(result.metrics.qpc_frequency, 0)
            self.assertGreaterEqual(result.metrics.callback_ticks, 0)
            self.assertGreaterEqual(result.metrics.write_ticks, 0)
            self.assertGreaterEqual(result.metrics.crc_ticks, 0)
            self.assertEqual(0, result.metrics.close_hresult)
            self.assertEqual(0, result.metrics.free_library_error)

    def test_built_bridge_crc_matches_standard_vectors(self):
        accelerator = self.load_built()
        vectors = [(b"", 0), (b"123456789", 0xCBF43926)]
        with tempfile.TemporaryDirectory() as root:
            source = Path(root, "vectors.zip")
            with zipfile.ZipFile(source, "w", zipfile.ZIP_STORED) as archive:
                for index, (payload, _crc) in enumerate(vectors):
                    archive.writestr(f"{index}.bin", payload)
            jobs = [
                archive_accel.NativeZipJob(
                    index,
                    str(Path(root, f"{index}.part")),
                    len(payload),
                    expected_crc,
                )
                for index, (payload, expected_crc) in enumerate(vectors)
            ]

            result = accelerator.extract_zip(str(source), jobs, None, None)

            self.assertEqual(archive_accel.AccelStatus.OK, result.status)
            for index, (payload, _crc) in enumerate(vectors):
                self.assertEqual(payload, Path(root, f"{index}.part").read_bytes())


if __name__ == "__main__":
    unittest.main()
