# In-process archive backends

## Goal

Remove child-process execution for non-ZIP/7z archive handling while keeping the existing `formats`/`Archive` contract, extraction safety rules, and Windows-only distribution model.

## Scope

- RAR/RAR5: list, extract, and integrity test for unencrypted archives through libarchive's in-process C API. Encrypted RAR is detected and rejected without writing because libarchive's RAR reader does not implement RAR decryption; creation remains unsupported because the upstream RAR reader is read-only and RAR creation has format/licensing constraints.
- ISO9660/UDF/CAB and other libarchive-readable inputs: list, extract, and test through the same in-process API. Creation is only exposed where the existing UI already has a format writer.
- EGG: list, extract, and test through the MIT-licensed `unegg` Rust library compiled into a native yzip module; no `unegg.exe` process.
- ALZ: list, extract, and test through the MIT-licensed `unalz` Rust library compiled into the same native yzip module; no `unalz.exe` process.
- Existing ZIP/7z/TAR/stream paths remain unchanged unless a regression test requires a shared safety helper.

## Non-goals

- Implementing a new RAR compressor.
- Replacing the existing 7z writer.
- Adding every obscure format that no bundled backend can read.
- Removing the existing CLI extractor until the native backend has passed the compatibility and corruption fixture suite.

## Architecture

```text
formats.detect()
  ├─ zip / 7z / tar / stream -> existing Python backends
  ├─ rar / cab / iso / ...    -> nativearchive.LibArchive
  └─ egg / alz                -> nativearchive.LegacyArchive

nativearchive.py              Python adapter; common Entry/progress/safety API
nativearchive/src/lib.rs      Rust cdylib for EGG/ALZ only
libarchive.dll                bundled in-process C library for RAR/ISO/CAB/etc.
```

All native readers write into a hidden staging directory and then reuse `formats._Planner`/`ziplib.finish_part` to enforce path containment, overwrite mode, and atomic placement. Native code must not write directly into a user-selected destination.

## Distribution and licensing

- Pin a libarchive release and its self-contained Windows x64 DLL by SHA-256; ship upstream license and source-offer notices.
- Pin `unegg` and `unalz` commits in the Rust manifest/lockfile; preserve their MIT notices.
- Update PyInstaller and CI to bundle the DLL and native module.
- Add a runtime capability error that identifies the missing backend instead of silently falling back to an executable.

## Review focus

1. RAR5 encrypted and multi-volume input: encrypted input must report the unsupported-decryption/password error and write nothing; unencrypted multi-volume input must be read as one set.
2. ISO/Joliet/Rock Ridge names and CAB multi-file sets: filenames must remain inside the destination and retain Unicode metadata where available.
3. EGG solid, encrypted, and split volumes: failed CRC/password/authentication must leave no final file.
4. ALZ CP949 names, ZipCrypto, and `.a00` volumes: path normalization and CRC errors must match the existing public error behavior.
5. Missing or damaged native backend: the application must show an actionable error and must not invoke a child process.
