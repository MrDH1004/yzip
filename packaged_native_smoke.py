"""Exercise the packaged native ZIP bridge against its bundled pinned runtime without installing yzip."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
from unittest import mock
import zipfile
import zlib

import archive_accel


def main() -> int:
    tools = Path(__file__).resolve().parent / "dist" / "yzip" / "_internal" / "tools"
    accelerator_dll = tools / "yzip_accel.dll"
    runtime_dll = tools / "7z.dll"
    manifest_path = tools / "7zip-pins.json"
    for required in (accelerator_dll, runtime_dll, manifest_path):
        if not required.is_file():
            raise FileNotFoundError(f"packaged native payload is missing: {required}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    runtime_hash = hashlib.sha256(runtime_dll.read_bytes()).hexdigest()
    if runtime_hash != manifest["binary_sha256"]["7z.dll"]:
        raise RuntimeError("packaged 7z.dll does not match 7zip-pins.json")

    with mock.patch.object(archive_accel, "_default_7z_path", return_value=runtime_dll):
        archive_accel._runtime_7z_matches_pin.cache_clear()
        accelerator = archive_accel.load_accelerator(accelerator_dll)
        if accelerator is None or not accelerator.capabilities & archive_accel.CAP_ZIP_EXTRACT:
            raise RuntimeError(archive_accel.disabled_reason() or "packaged accelerator unavailable")
        with tempfile.TemporaryDirectory(prefix="yzip-packaged-native-") as folder:
            root = Path(folder)
            source = root / "source.zip"
            staging = root / "payload.part"
            payload = b"packaged native payload" * 1000
            with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("payload.bin", payload)
            job = archive_accel.NativeZipJob(0, str(staging), len(payload), zlib.crc32(payload))
            result = accelerator.extract_zip(str(source), [job], None, None)
            if result.status != archive_accel.AccelStatus.OK:
                raise RuntimeError(
                    f"packaged native extraction failed: {result.status.name}: "
                    f"{accelerator.last_error()}"
                )
            if staging.read_bytes() != payload:
                raise RuntimeError("packaged native extraction content mismatch")
    print("packaged native ZIP smoke: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
