import ctypes
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import archive_accel


class FakeFunction:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class FakeLibrary:
    def __init__(self, *, abi=archive_accel.ABI_VERSION, capabilities=0, error=""):
        self.yz_accel_abi_version = FakeFunction(lambda: abi)
        self.yz_accel_capabilities = FakeFunction(lambda: capabilities)

        def last_error(buffer, size):
            required = len(error) + 1
            if buffer is not None and size:
                buffer.value = error[: max(0, size - 1)]
            return required

        self.yz_accel_last_error = FakeFunction(last_error)

        def cancel(pointer):
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32)).contents.value = 1

        self.yz_accel_cancel = FakeFunction(cancel)


class LoaderTests(unittest.TestCase):
    def tearDown(self):
        archive_accel._reset_for_tests()

    def test_missing_dll_disables_accelerator_with_reason(self):
        missing = Path(tempfile.gettempdir()) / "definitely-missing-yzip-accel.dll"
        accelerator = archive_accel.load_accelerator(missing)
        self.assertIsNone(accelerator)
        self.assertIn("not found", archive_accel.disabled_reason().lower())

    def test_wrong_abi_disables_accelerator(self):
        library = FakeLibrary(abi=archive_accel.ABI_VERSION + 1)
        with mock.patch.object(archive_accel.ctypes, "WinDLL", return_value=library):
            accelerator = archive_accel.load_accelerator(Path("wrong-abi.dll"))
        self.assertIsNone(accelerator)
        self.assertIn("abi", archive_accel.disabled_reason().lower())

    def test_capability_mask_preserves_all_64_bits(self):
        expected = 0x8000000000000005
        library = FakeLibrary(capabilities=expected)
        with mock.patch.object(archive_accel.ctypes, "WinDLL", return_value=library):
            accelerator = archive_accel.load_accelerator(Path("capabilities.dll"))
        self.assertIsNotNone(accelerator)
        self.assertEqual(expected, accelerator.capabilities)
        self.assertIs(ctypes.c_uint64, library.yz_accel_capabilities.restype)

    def test_unicode_error_is_retrieved_without_truncation(self):
        expected = "7-Zip 초기화 실패: 경로 없음 ☃"
        library = FakeLibrary(error=expected)
        with mock.patch.object(archive_accel.ctypes, "WinDLL", return_value=library):
            accelerator = archive_accel.load_accelerator(Path("unicode.dll"))
        self.assertEqual(expected, accelerator.last_error())
        self.assertEqual(
            [ctypes.POINTER(ctypes.c_wchar), ctypes.c_size_t],
            library.yz_accel_last_error.argtypes,
        )

    def test_cancellation_flag_uses_dll_atomic_cancel_entry_point(self):
        library = FakeLibrary()
        with mock.patch.object(archive_accel.ctypes, "WinDLL", return_value=library):
            accelerator = archive_accel.load_accelerator(Path("cancel.dll"))
        flag = archive_accel.CancellationFlag(accelerator)
        self.assertFalse(flag.cancelled)
        flag.cancel()
        self.assertTrue(flag.cancelled)
        self.assertEqual([ctypes.POINTER(ctypes.c_uint32)], library.yz_accel_cancel.argtypes)


class BuiltBridgeTests(unittest.TestCase):
    def test_built_x64_bridge_exports_expected_stub_abi(self):
        configured = os.environ.get("YZIP_ACCEL_DLL")
        dll = Path(configured) if configured else (
            Path(__file__).parent / "nativeaccel" / "build" / "yzip_accel.dll"
        )
        if not dll.exists():
            if os.environ.get("YZIP_REQUIRE_ACCEL_DLL") == "1":
                self.fail(f"required native accelerator DLL is missing: {dll}")
            self.skipTest("nativeaccel/build.ps1 has not run")
        accelerator = archive_accel.load_accelerator(dll)
        self.assertIsNotNone(accelerator, archive_accel.disabled_reason())
        self.assertEqual(0, accelerator.capabilities)
        self.assertEqual("", accelerator.last_error())


class BuildContractTests(unittest.TestCase):
    def test_msvc_build_uses_required_optimized_static_runtime_flags(self):
        script = (Path(__file__).parent / "nativeaccel" / "build.ps1").read_text(
            encoding="utf-8"
        )
        command = next(line for line in script.splitlines() if "cl /nologo" in line)
        for required in ("/O2", "/GL", "/MT", "/LTCG"):
            self.assertIn(required, command)
        self.assertNotIn("/MD", command)

    def test_fetch_reads_7zip_source_pin_from_committed_manifest(self):
        root = Path(__file__).parent
        script = (root / "fetch_tools.ps1").read_text(encoding="utf-8")
        pins = json.loads(
            (root / "nativeaccel" / "7zip-pins.json").read_text(encoding="utf-8")
        )
        self.assertIn("ConvertFrom-Json", script)
        self.assertIn("source_url", script)
        self.assertIn("source_sha256", script)
        self.assertNotIn(pins["source_url"], script)
        self.assertNotIn(pins["source_sha256"], script)

    def test_required_native_test_mode_fails_when_dll_is_missing(self):
        missing = Path(tempfile.gettempdir()) / "required-yzip-accel-is-missing.dll"
        environment = os.environ.copy()
        environment["YZIP_ACCEL_DLL"] = str(missing)
        environment["YZIP_REQUIRE_ACCEL_DLL"] = "1"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "test_archive_accel.BuiltBridgeTests.test_built_x64_bridge_exports_expected_stub_abi",
                "-v",
            ],
            cwd=Path(__file__).parent,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("required native accelerator DLL is missing", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
