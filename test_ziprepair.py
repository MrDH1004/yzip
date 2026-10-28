import os
from pathlib import Path
import mmap
import struct
import tempfile
import unittest
from unittest import mock
import zipfile
import zlib

import zipwriter

try:
    import ziprepair
except ImportError:
    ziprepair = None


LOCAL = b"PK\x03\x04"
CENTRAL = b"PK\x01\x02"


def _local_store(name, payload, *, descriptor=True, signed=True, zip64=False,
                 declared_csize=None, declared_usize=None):
    raw = name.encode("utf-8")
    crc = zlib.crc32(payload)
    flags = 0x0808 if descriptor else 0x0800
    if zip64:
        extra = struct.pack("<HHQQ", 1, 16, len(payload), len(payload))
        csize = usize = 0xFFFFFFFF
    else:
        extra = b""
        csize = 0 if descriptor else (len(payload) if declared_csize is None else declared_csize)
        usize = 0 if descriptor else (len(payload) if declared_usize is None else declared_usize)
    header_crc = 0 if descriptor else crc
    header = struct.pack("<IHHHHHIIIHH", 0x04034B50, 45 if zip64 else 20, flags, 0,
                         0, 0, header_crc, csize, usize, len(raw), len(extra)) + raw + extra
    if not descriptor:
        return header + payload
    prefix = struct.pack("<I", 0x08074B50) if signed else b""
    sizes = struct.pack("<QQ" if zip64 else "<II", len(payload), len(payload))
    return header + payload + prefix + struct.pack("<I", crc) + sizes


class ZipRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yzip-repair-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _archive(self, names=("good.txt", "second.txt"), malicious=False, store_only=False):
        items = []
        for i, name in enumerate(names):
            source = self.root / f"source-{i}.txt"
            source.write_bytes((f"payload-{i}-" * 300).encode())
            items.append((str(source), "../../outside.txt" if malicious and i == 0 else name))
        archive = self.root / "damaged.zip"
        zipwriter.write_zip(items, str(archive), level=5, store_only=store_only)
        return archive

    @staticmethod
    def _remove_central(archive):
        data = archive.read_bytes()
        archive.write_bytes(data[:data.index(CENTRAL)])

    @staticmethod
    def _damage_first_payload(archive):
        data = bytearray(archive.read_bytes())
        offset = data.index(LOCAL)
        name_len, extra_len = struct.unpack_from("<HH", data, offset + 26)
        payload = offset + 30 + name_len + extra_len
        data[payload + 4] ^= 0x55
        archive.write_bytes(data)

    def test_recovers_store_and_deflate_when_central_directory_is_missing(self):
        self.assertIsNotNone(ziprepair, "ziprepair module must exist")
        archive = self._archive()
        self._remove_central(archive)
        output = self.root / "recovered.zip"

        result = ziprepair.repair_zip(str(archive), str(output))

        self.assertEqual(result.recovered, ("good.txt", "second.txt"))
        self.assertEqual(result.skipped, ())
        with zipfile.ZipFile(output) as repaired:
            self.assertIsNone(repaired.testzip())
            self.assertEqual(repaired.read("good.txt"), ("payload-0-" * 300).encode())
            self.assertEqual(repaired.read("second.txt"), ("payload-1-" * 300).encode())

        stored = self._archive(names=("stored.txt",), store_only=True)
        self._remove_central(stored)
        stored_output = self.root / "stored-recovered.zip"
        stored_result = ziprepair.repair_zip(str(stored), str(stored_output))
        self.assertEqual(stored_result.recovered, ("stored.txt",))
        with zipfile.ZipFile(stored_output) as repaired:
            self.assertEqual(repaired.read("stored.txt"), ("payload-0-" * 300).encode())

    def test_skips_corrupt_member_and_resynchronizes_at_next_header(self):
        archive = self._archive()
        self._damage_first_payload(archive)
        self._remove_central(archive)
        output = self.root / "recovered.zip"

        result = ziprepair.repair_zip(str(archive), str(output))

        self.assertEqual(result.recovered, ("second.txt",))
        self.assertTrue(any(item.name == "good.txt" for item in result.skipped))
        with zipfile.ZipFile(output) as repaired:
            self.assertEqual(repaired.namelist(), ["second.txt"])

    def test_sanitizes_traversal_name_and_never_writes_outside_workspace(self):
        archive = self._archive(names=("ignored",), malicious=True)
        self._remove_central(archive)
        output = self.root / "recovered.zip"

        result = ziprepair.repair_zip(str(archive), str(output))

        self.assertEqual(result.recovered, ("outside.txt",))
        self.assertFalse((self.root.parent / "outside.txt").exists())
        with zipfile.ZipFile(output) as repaired:
            self.assertEqual(repaired.namelist(), ["outside.txt"])

    def test_refuses_to_overwrite_source(self):
        archive = self._archive()
        original = archive.read_bytes()

        with self.assertRaises(ValueError):
            ziprepair.repair_zip(str(archive), str(archive))

        self.assertEqual(archive.read_bytes(), original)

    def test_cancel_removes_partial_output(self):
        archive = self._archive()
        self._remove_central(archive)
        output = self.root / "recovered.zip"

        with self.assertRaises(ziprepair.Cancelled):
            ziprepair.repair_zip(str(archive), str(output), cancel=lambda: True)

        self.assertFalse(output.exists())

    def test_empty_recovery_does_not_publish_an_archive(self):
        archive = self.root / "not-a-zip.zip"
        archive.write_bytes(b"not a zip")
        output = self.root / "recovered.zip"

        result = ziprepair.repair_zip(str(archive), str(output))

        self.assertEqual(result.recovered, ())
        self.assertFalse(output.exists())

    def test_rejects_false_store_sizes_before_copying_payload(self):
        archive = self.root / "false-sizes.zip"
        archive.write_bytes(_local_store("bad.bin", b"x" * (2 << 20), descriptor=False,
                                         declared_csize=2 << 20, declared_usize=1))
        output = self.root / "recovered.zip"

        result = ziprepair.repair_zip(str(archive), str(output))

        self.assertEqual(result.recovered, ())
        self.assertIn("stored sizes", result.skipped[0].reason)
        self.assertFalse(output.exists())

    def test_recovers_unsigned_store_descriptor(self):
        archive = self.root / "unsigned.zip"
        archive.write_bytes(_local_store("plain.txt", b"unsigned descriptor", signed=False))
        output = self.root / "recovered.zip"

        result = ziprepair.repair_zip(str(archive), str(output))

        self.assertEqual(result.recovered, ("plain.txt",))
        with zipfile.ZipFile(output) as repaired:
            self.assertEqual(repaired.read("plain.txt"), b"unsigned descriptor")

    def test_recovers_zip64_store_descriptor_and_extra(self):
        archive = self.root / "zip64.zip"
        archive.write_bytes(_local_store("large.txt", b"zip64 payload", zip64=True))
        output = self.root / "recovered.zip"

        result = ziprepair.repair_zip(str(archive), str(output))

        self.assertEqual(result.recovered, ("large.txt",))
        with zipfile.ZipFile(output) as repaired:
            self.assertEqual(repaired.read("large.txt"), b"zip64 payload")

    def test_recovers_zip64_deflate_descriptor_and_extra(self):
        with mock.patch.object(zipwriter, "ZIP64_LIMIT", 1):
            archive = self._archive(names=("deflated.txt",))
        self._remove_central(archive)
        output = self.root / "recovered.zip"

        result = ziprepair.repair_zip(str(archive), str(output))

        self.assertEqual(result.recovered, ("deflated.txt",))
        with zipfile.ZipFile(output) as repaired:
            self.assertEqual(repaired.read("deflated.txt"), ("payload-0-" * 300).encode())

    def test_last_valid_case_insensitive_duplicate_wins(self):
        archive = self.root / "duplicates.zip"
        archive.write_bytes(_local_store("Name.txt", b"first") +
                            _local_store("name.TXT", b"second"))
        output = self.root / "recovered.zip"

        result = ziprepair.repair_zip(str(archive), str(output))

        self.assertEqual(result.recovered, ("name.TXT",))
        with zipfile.ZipFile(output) as repaired:
            self.assertEqual(repaired.namelist(), ["name.TXT"])
            self.assertEqual(repaired.read("name.TXT"), b"second")

    def test_valid_central_directory_ignores_orphan_local_header(self):
        archive = self._archive(names=("listed.txt",))
        with archive.open("ab") as stream:
            stream.write(_local_store("orphan.txt", b"must not recover"))
        output = self.root / "recovered.zip"

        result = ziprepair.repair_zip(str(archive), str(output))

        self.assertEqual(result.recovered, ("listed.txt",))
        with zipfile.ZipFile(output) as repaired:
            self.assertEqual(repaired.namelist(), ["listed.txt"])

    def test_cancel_during_store_copy_preserves_existing_output(self):
        archive = self.root / "large-store.zip"
        archive.write_bytes(_local_store("large.bin", b"x" * (3 << 20)))
        output = self.root / "recovered.zip"
        output.write_bytes(b"existing output")
        checks = 0

        def cancel():
            nonlocal checks
            checks += 1
            return checks >= 4

        with mock.patch("ziprepair.zipwriter.write_zip",
                        side_effect=AssertionError("copy did not observe cancellation")):
            with self.assertRaises(ziprepair.Cancelled):
                ziprepair.repair_zip(str(archive), str(output), cancel=cancel)

        self.assertGreaterEqual(checks, 4)
        self.assertEqual(output.read_bytes(), b"existing output")

    def test_replace_failure_preserves_existing_output(self):
        archive = self._archive(names=("good.txt",))
        output = self.root / "recovered.zip"
        output.write_bytes(b"existing output")

        real_replace = os.replace

        def replace(source, destination):
            if os.path.realpath(destination) == os.path.realpath(output):
                raise OSError("publish failed")
            return real_replace(source, destination)

        with mock.patch("ziprepair.os.replace", side_effect=replace):
            with self.assertRaisesRegex(OSError, "publish failed"):
                ziprepair.repair_zip(str(archive), str(output))

        self.assertEqual(output.read_bytes(), b"existing output")

    def test_many_fake_store_descriptors_have_linear_crc_work(self):
        payload = bytearray(b"prefix")
        for _ in range(300):
            candidate_size = len(payload)
            payload.extend(struct.pack("<IIII", 0x08074B50, 0xDEADBEEF,
                                       candidate_size, candidate_size))
            payload.extend(b"data")
        archive = self.root / "many-candidates.zip"
        archive.write_bytes(_local_store("many.bin", bytes(payload)))
        output = self.root / "recovered.zip"
        real_crc32 = zlib.crc32
        processed = 0

        def counting_crc32(data, value=0):
            nonlocal processed
            processed += len(data)
            return real_crc32(data, value)

        with mock.patch("ziprepair.zlib.crc32", side_effect=counting_crc32):
            result = ziprepair.repair_zip(str(archive), str(output))

        self.assertEqual(result.recovered, ("many.bin",))
        self.assertLessEqual(processed, len(payload) * 3 + (1 << 16))

    def test_duplicate_replacement_uses_final_total_size(self):
        archive = self.root / "duplicate-limit.zip"
        archive.write_bytes(_local_store("same.bin", b"12345678") +
                            _local_store("SAME.BIN", b"ABCDEF"))
        output = self.root / "recovered.zip"

        with mock.patch.object(ziprepair, "_MAX_RECOVERED", 10):
            result = ziprepair.repair_zip(str(archive), str(output))

        self.assertEqual(result.recovered, ("SAME.BIN",))
        with zipfile.ZipFile(output) as repaired:
            self.assertEqual(repaired.read("SAME.BIN"), b"ABCDEF")

    def test_zip64_empty_store_descriptor_consumes_64_bit_form(self):
        first = _local_store("empty.bin", b"", zip64=True)
        archive = self.root / "empty-zip64.zip"
        archive.write_bytes(first + _local_store("next.bin", b"next"))
        workspace = self.root / "workspace"
        workspace.mkdir()

        with archive.open("rb") as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            _name, _path, next_offset, _size, _is_dir, _time, _date = \
                ziprepair._member(mm, 0, os.path.realpath(workspace), 1024)

        self.assertEqual(next_offset, len(first))

    def test_central_directory_recovery_reports_progress(self):
        archive = self._archive(names=("listed.txt",))
        output = self.root / "recovered.zip"
        events = []

        ziprepair.repair_zip(str(archive), str(output),
                             progress=lambda done, total, name: events.append((done, total, name)))

        self.assertTrue(events)
        self.assertEqual(events[-1], (archive.stat().st_size, archive.stat().st_size, "listed.txt"))

    def test_equal_store_sizes_over_limit_are_rejected_before_copy(self):
        archive = self.root / "over-limit.zip"
        archive.write_bytes(_local_store("large.bin", b"x" * 11, descriptor=False))
        output = self.root / "recovered.zip"

        with mock.patch.object(ziprepair, "_MAX_RECOVERED", 10), \
                mock.patch("ziprepair._copy_store",
                           side_effect=AssertionError("oversized payload was copied")):
            result = ziprepair.repair_zip(str(archive), str(output))

        self.assertEqual(result.recovered, ())
        self.assertIn("declared member size", result.skipped[0].reason)


if __name__ == "__main__":
    unittest.main()
