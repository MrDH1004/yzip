import ctypes
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
        dll = Path(__file__).parent / "nativeaccel" / "build" / "yzip_accel.dll"
        if not dll.exists():
            self.skipTest("nativeaccel/build.ps1 has not run")
        accelerator = archive_accel.load_accelerator(dll)
        self.assertIsNotNone(accelerator, archive_accel.disabled_reason())
        self.assertEqual(0, accelerator.capabilities)
        self.assertEqual("", accelerator.last_error())


if __name__ == "__main__":
    unittest.main()
