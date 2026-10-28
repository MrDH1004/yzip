"""Recover verifiable Store/Deflate members from a damaged ZIP into a new ZIP."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import mmap
import os
import struct
import tempfile
import time
from typing import Callable, Optional
import zipfile
import zlib

import ziplib
import zipwriter

Cancelled = ziplib.Cancelled

_LOCAL = b"PK\x03\x04"
_CENTRAL = b"PK\x01\x02"
_DESCRIPTOR = b"PK\x07\x08"
_HEADER = struct.Struct("<IHHHHHIIIHH")
_CHUNK = 64 << 10
_MAX_RECOVERED = 8 << 30


@dataclass(frozen=True)
class SkippedItem:
    name: str
    reason: str


@dataclass(frozen=True)
class RepairResult:
    output: Optional[str]
    recovered: tuple[str, ...]
    skipped: tuple[SkippedItem, ...]


def _name(raw: bytes, utf8: bool) -> str:
    if utf8:
        return raw.decode("utf-8", "replace")
    for encoding in ("utf-8", "cp949", "cp437"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.decode("cp437", "replace")


def _cancelled(cancel: Optional[Callable[[], bool]]) -> None:
    if cancel and cancel():
        raise Cancelled()


def _inflate(mm, start: int, compressed: Optional[int], dst, limit: int,
             cancel: Optional[Callable[[], bool]] = None):
    dec = zlib.decompressobj(-15)
    crc, size, cursor = 0, 0, start
    end_limit = len(mm) if compressed is None else min(len(mm), start + compressed)
    while cursor < end_limit:
        _cancelled(cancel)
        block = bytes(mm[cursor:min(cursor + _CHUNK, end_limit)])
        supplied = len(block)
        while block:
            out = dec.decompress(block, 1 << 20)
            if out:
                size += len(out)
                if size > limit:
                    raise ValueError("member exceeds the safety limit")
                crc = zlib.crc32(out, crc)
                dst.write(out)
            if dec.eof:
                consumed = supplied - len(dec.unused_data)
                return cursor + consumed, crc, size
            tail = dec.unconsumed_tail
            if tail:
                block = tail
            else:
                cursor += supplied
                break
    if compressed is not None:
        out = dec.flush()
        if out:
            size += len(out)
            if size > limit:
                raise ValueError("member exceeds the safety limit")
            crc = zlib.crc32(out, crc)
            dst.write(out)
    if not dec.eof:
        raise ValueError("deflate stream is truncated")
    return cursor, crc, size


def _descriptor(mm, offset: int, crc: int, csize: int, usize: int, zip64: bool = False) -> int:
    pos = offset + (4 if mm[offset:offset + 4] == _DESCRIPTOR else 0)
    formats = (("<IQQ", 20), ("<III", 12)) if zip64 else (("<III", 12), ("<IQQ", 20))
    for fmt, width in formats:
        if pos + width > len(mm):
            continue
        got_crc, got_csize, got_usize = struct.unpack_from(fmt, mm, pos)
        if (got_crc, got_csize, got_usize) == (crc, csize, usize):
            return pos + width
    raise ValueError("data descriptor does not match recovered data")


def _copy_store(mm, start: int, size: int, dst, limit: int,
                cancel: Optional[Callable[[], bool]]) -> int:
    if size < 0 or size > limit or start + size > len(mm):
        raise ValueError("stored member exceeds the safety limit")
    crc = 0
    cursor = start
    end = start + size
    while cursor < end:
        _cancelled(cancel)
        block = bytes(mm[cursor:min(cursor + _CHUNK, end)])
        crc = zlib.crc32(block, crc)
        dst.write(block)
        cursor += len(block)
    return crc


def _store_candidates(mm, start: int, cancel: Optional[Callable[[], bool]]):
    signatures = (_DESCRIPTOR, _LOCAL, _CENTRAL, b"PK\x05\x06", b"PK\x06\x06")
    window_start = start
    while window_start < len(mm):
        _cancelled(cancel)
        window_end = min(len(mm), window_start + _CHUNK)
        search_end = min(len(mm), window_end + 20)
        candidates = set()
        for signature in signatures:
            marker = mm.find(signature, window_start, search_end)
            while marker >= 0:
                if signature == _DESCRIPTOR:
                    candidates.add(marker)
                else:
                    candidates.update((marker - 12, marker - 20))
                marker = mm.find(signature, marker + 1, search_end)
        if window_end == len(mm):
            candidates.update((len(mm) - 12, len(mm) - 20))
        for candidate in sorted(candidate for candidate in candidates
                                if window_start <= candidate < window_end and candidate >= start):
            yield candidate
        window_start += _CHUNK


def _store_with_descriptor(mm, start: int, dst, limit: int,
                           cancel: Optional[Callable[[], bool]], zip64: bool = False):
    got_crc = 0
    crc_cursor = start
    formats = (("<IQQ", 20), ("<III", 12)) if zip64 else (("<III", 12), ("<IQQ", 20))
    for candidate in _store_candidates(mm, start, cancel):
        _cancelled(cancel)
        size = candidate - start
        if size > limit:
            break
        while crc_cursor < candidate:
            _cancelled(cancel)
            end = min(crc_cursor + _CHUNK, candidate)
            got_crc = zlib.crc32(mm[crc_cursor:end], got_crc)
            crc_cursor = end
        signed = mm[candidate:candidate + 4] == _DESCRIPTOR
        pos = candidate + (4 if signed else 0)
        for fmt, width in formats:
            if pos + width > len(mm):
                continue
            crc, csize, usize = struct.unpack_from(fmt, mm, pos)
            if csize != size or usize != size:
                continue
            if got_crc == crc:
                copied_crc = _copy_store(mm, start, size, dst, limit, cancel)
                return pos + width, copied_crc, size
    raise ValueError("store data descriptor is missing")


def _zip64_sizes(extra: bytes, csize: int, usize: int) -> tuple[int, int, bool]:
    cursor = 0
    while cursor + 4 <= len(extra):
        field_id, field_size = struct.unpack_from("<HH", extra, cursor)
        cursor += 4
        end = cursor + field_size
        if end > len(extra):
            raise ValueError("ZIP extra field is truncated")
        if field_id == 0x0001:
            values = extra[cursor:end]
            value_offset = 0
            if usize == 0xFFFFFFFF:
                if value_offset + 8 > len(values):
                    raise ValueError("ZIP64 original size is missing")
                usize = struct.unpack_from("<Q", values, value_offset)[0]
                value_offset += 8
            if csize == 0xFFFFFFFF:
                if value_offset + 8 > len(values):
                    raise ValueError("ZIP64 compressed size is missing")
                csize = struct.unpack_from("<Q", values, value_offset)[0]
            return csize, usize, True
        cursor = end
    if csize == 0xFFFFFFFF or usize == 0xFFFFFFFF:
        raise ValueError("ZIP64 size extra field is missing")
    return csize, usize, False


def _member(mm, offset: int, workspace: str, total_limit: int,
            cancel: Optional[Callable[[], bool]] = None):
    if offset + _HEADER.size > len(mm):
        raise ValueError("local header is truncated")
    signature, _version, flags, method, dos_time, dos_date, crc, csize, usize, nlen, xlen = \
        _HEADER.unpack_from(mm, offset)
    if signature != 0x04034B50 or not nlen or offset + 30 + nlen + xlen > len(mm):
        raise ValueError("invalid local header")
    raw_name = bytes(mm[offset + 30:offset + 30 + nlen])
    extra = bytes(mm[offset + 30 + nlen:offset + 30 + nlen + xlen])
    csize, usize, is_zip64 = _zip64_sizes(extra, csize, usize)
    original_name = _name(raw_name, bool(flags & 0x0800)).replace("\\", "/")
    if flags & 1:
        raise ValueError("encrypted member is not supported by raw recovery")
    if method not in (0, 8):
        raise ValueError(f"compression method {method} is not supported")
    target = ziplib.safe_target(workspace, original_name)
    if target is None:
        raise ValueError("unsafe or empty member name")
    relative = ziplib._safe_rel(original_name)
    data_start = offset + 30 + nlen + xlen
    if original_name.endswith("/"):
        os.makedirs(target, exist_ok=True)
        return relative.rstrip("/") + "/", target, data_start, 0, True, dos_time, dos_date
    os.makedirs(os.path.dirname(target), exist_ok=True)
    part = ziplib.part_path(target)
    try:
        with open(part, "wb") as dst:
            if flags & 0x0008:
                if method == 8:
                    end, got_crc, got_size = _inflate(mm, data_start, None, dst, total_limit, cancel)
                    next_offset = _descriptor(mm, end, got_crc, end - data_start, got_size, is_zip64)
                else:
                    next_offset, got_crc, got_size = _store_with_descriptor(
                        mm, data_start, dst, total_limit, cancel, is_zip64)
            else:
                if csize > len(mm) - data_start or usize > total_limit or csize > total_limit:
                    raise ValueError("declared member size is invalid")
                if method == 8:
                    end, got_crc, got_size = _inflate(mm, data_start, csize, dst, total_limit, cancel)
                    if end != data_start + csize:
                        raise ValueError("compressed size does not match")
                else:
                    if csize != usize:
                        raise ValueError("stored sizes do not match")
                    got_crc = _copy_store(mm, data_start, csize, dst, total_limit, cancel)
                    end, got_size = data_start + csize, csize
                next_offset = end
                if (got_crc, got_size) != (crc, usize):
                    raise ValueError("CRC or original size does not match")
        ziplib.finish_part(part, target)
    except BaseException:
        if os.path.exists(part):
            os.remove(part)
        raise
    try:
        year = 1980 + (dos_date >> 9)
        month, day = (dos_date >> 5) & 15, dos_date & 31
        hour, minute, second = dos_time >> 11, (dos_time >> 5) & 63, (dos_time & 31) * 2
        stamp = time.mktime((year, month, day, hour, minute, second, 0, 0, -1))
        os.utime(target, (stamp, stamp))
    except (OverflowError, OSError, ValueError):
        pass
    return relative, target, next_offset, got_size, False, dos_time, dos_date


def _recover_from_central(source: str, workspace: str, per_member_limit: int, total_limit: int,
                          cancel: Optional[Callable[[], bool]],
                          progress: Optional[Callable[[int, int, str], None]] = None):
    recovered = OrderedDict()
    skipped: list[SkippedItem] = []
    total = 0
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        source_size = os.path.getsize(source)
        for index, info in enumerate(infos):
            _cancelled(cancel)
            name = info.filename.replace("\\", "/")
            relative = ziplib._safe_rel(name)
            key = relative.casefold()
            previous_size = recovered.get(key, (None, None, None, 0))[3]
            available = total_limit - total + previous_size
            if info.flag_bits & 1:
                skipped.append(SkippedItem(name, "encrypted member is not supported by recovery"))
            elif info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                skipped.append(SkippedItem(name, f"compression method {info.compress_type} is not supported"))
            elif info.file_size > per_member_limit or info.file_size > available:
                skipped.append(SkippedItem(name, "member exceeds the safety limit"))
            else:
                target = ziplib.safe_target(workspace, name)
                if target is None:
                    skipped.append(SkippedItem(name, "unsafe or empty member name"))
                else:
                    is_dir = info.is_dir()
                    try:
                        written = 0
                        if is_dir:
                            os.makedirs(target, exist_ok=True)
                            relative = relative.rstrip("/") + "/"
                        else:
                            os.makedirs(os.path.dirname(target), exist_ok=True)
                            part = ziplib.part_path(target)
                            try:
                                with archive.open(info) as src, open(part, "wb") as dst:
                                    while True:
                                        _cancelled(cancel)
                                        block = src.read(_CHUNK)
                                        if not block:
                                            break
                                        written += len(block)
                                        if written > per_member_limit or written > available:
                                            raise ValueError("member exceeds the safety limit")
                                        dst.write(block)
                                ziplib.finish_part(part, target)
                            except BaseException:
                                if os.path.exists(part):
                                    os.remove(part)
                                raise
                        total = total - previous_size + written
                        recovered[key] = (relative, target, is_dir, written)
                    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
                        skipped.append(SkippedItem(name, str(error)))
            if progress:
                done = source_size if index + 1 == len(infos) else min(info.header_offset, source_size)
                progress(done, source_size, name)
    return recovered, skipped


def repair_zip(source: str, output: str, progress: Optional[Callable[[int, int, str], None]] = None,
               cancel: Optional[Callable[[], bool]] = None) -> RepairResult:
    source = os.path.realpath(source)
    output = os.path.realpath(output)
    if source == output:
        raise ValueError("원본 ZIP과 복구 ZIP은 다른 경로여야 합니다.")
    if not os.path.isfile(source):
        raise FileNotFoundError(source)
    size = os.path.getsize(source)
    per_member_limit = min(_MAX_RECOVERED, max(64 << 20, size * 1000))
    total_limit = min(_MAX_RECOVERED, max(256 << 20, size * 1000))
    output_dir = os.path.dirname(output) or os.curdir
    os.makedirs(output_dir, exist_ok=True)
    recovered = OrderedDict()
    skipped: list[SkippedItem] = []
    temporary_output = ziplib.part_path(output)
    try:
        with tempfile.TemporaryDirectory(prefix=".yzip-repair-", dir=output_dir) as workspace:
            try:
                recovered, skipped = _recover_from_central(
                    source, workspace, per_member_limit, total_limit, cancel, progress)
            except (OSError, ValueError, zipfile.BadZipFile):
                recovered = OrderedDict()
                skipped = []
                raw = open(source, "rb")
                try:
                    if not size:
                        mm = None
                    else:
                        mm = mmap.mmap(raw.fileno(), 0, access=mmap.ACCESS_READ)
                    if mm is not None:
                        try:
                            offset = 0
                            total_recovered = 0
                            while True:
                                _cancelled(cancel)
                                found = mm.find(_LOCAL, offset)
                                if found < 0:
                                    break
                                display = f"0x{found:X}"
                                try:
                                    if found + 30 <= size:
                                        nlen = struct.unpack_from("<H", mm, found + 26)[0]
                                        display = _name(bytes(mm[found + 30:found + 30 + nlen]),
                                                        bool(struct.unpack_from("<H", mm, found + 6)[0] & 0x0800))
                                    expected_key = ziplib._safe_rel(display.replace("\\", "/")).casefold()
                                    previous_size = recovered.get(expected_key, (None, None, None, 0))[3]
                                    available = total_limit - total_recovered + previous_size
                                    name, path, next_offset, unpacked, is_dir, _time, _date = _member(
                                        mm, found, workspace,
                                        min(per_member_limit, available), cancel)
                                    key = name.casefold()
                                    previous_size = recovered.get(key, (None, None, None, 0))[3]
                                    total_recovered = total_recovered - previous_size + unpacked
                                    if total_recovered > total_limit:
                                        raise ValueError("total recovered size exceeds the safety limit")
                                    recovered[key] = (name, path, is_dir, unpacked)
                                    offset = max(next_offset, found + 4)
                                except (OSError, ValueError, zlib.error) as error:
                                    skipped.append(SkippedItem(display, str(error)))
                                    offset = found + 4
                                if progress:
                                    progress(min(offset, size), size, display)
                        finally:
                            mm.close()
                finally:
                    raw.close()
            if recovered:
                items = [(path, name) for name, path, _is_dir, _size in recovered.values()]
                zipwriter.write_zip(items, temporary_output, level=6, cancel=cancel)
                os.replace(temporary_output, output)
                published = output
            else:
                published = None
        return RepairResult(published, tuple(item[0] for item in recovered.values()), tuple(skipped))
    except BaseException:
        if os.path.exists(temporary_output):
            os.remove(temporary_output)
        raise
