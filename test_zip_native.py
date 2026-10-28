import ctypes
import os
import tempfile
import unittest
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
    def test_built_bridge_extracts_store_and_deflate_to_supplied_staging_paths(self):
        dll = Path(__file__).parent / "nativeaccel" / "build" / "yzip_accel.dll"
        self.assertTrue(dll.exists(), "nativeaccel/build.ps1 must run before this test")
        accelerator = archive_accel.load_accelerator(dll)
        self.assertIsNotNone(accelerator, archive_accel.disabled_reason())
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


if __name__ == "__main__":
    unittest.main()
