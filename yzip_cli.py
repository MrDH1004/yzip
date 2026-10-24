"""Command line front-end. Command set follows what REA found in Bandizip's bz.exe usage text:
a/c add-create, x extract (full path), e extract flat, t test, l list, d delete, rn rename."""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
import zipfile

import formats
import ziplib

_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(b|k|kb|m|mb|g|gb)?$", re.I)


def parse_size(s: str) -> int:
    m = _SIZE_RE.match(s.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"bad size: {s}")
    mult = {None: 1, "b": 1, "k": 1 << 10, "kb": 1 << 10, "m": 1 << 20, "mb": 1 << 20,
            "g": 1 << 30, "gb": 1 << 30}[(m.group(2) or "").lower() or None]
    return int(float(m.group(1)) * mult)


def _bar(done, total, name):
    pct = int(done * 100 / total) if total else 100
    sys.stdout.write(f"\r{pct:3d}%  {name[-60:]:<60}")
    sys.stdout.flush()


def _rewrite(src: str, keep) -> None:
    """Rebuild a zip keeping/renaming entries; keep(info)->new name or None to drop."""
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=os.path.dirname(os.path.abspath(src)))
    os.close(fd)
    try:
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                new = keep(info)
                if new is None:
                    continue
                data = zin.read(info)
                ni = zipfile.ZipInfo(new, info.date_time)
                ni.compress_type = info.compress_type
                ni.external_attr = info.external_attr
                zout.writestr(ni, data)
        os.replace(tmp, src)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def build_parser():
    p = argparse.ArgumentParser(prog="yzip", description="archiver: creates zip/7z/tar.*/gz/bz2/xz/zst, reads all of them")
    p.add_argument("command", choices=["a", "c", "x", "e", "t", "l", "d", "rn"])
    p.add_argument("archive")
    p.add_argument("files", nargs="*")
    p.add_argument("-fmt", dest="fmt", default="zip", choices=list(formats.BY_ID),
                   help="format for c (a/d/rn edit ZIP only)")
    p.add_argument("-l", dest="level", type=int, default=5, choices=range(0, 10), metavar="0-9")
    p.add_argument("-p", dest="password")
    p.add_argument("-o", dest="out", default=".")
    p.add_argument("-v", dest="volume", type=parse_size, default=0)
    p.add_argument("-y", action="store_true", help="assume yes")
    ow = p.add_mutually_exclusive_group()
    ow.add_argument("-aoa", dest="ow", action="store_const", const="all", default="all")
    ow.add_argument("-aos", dest="ow", action="store_const", const="skip")
    ow.add_argument("-aou", dest="ow", action="store_const", const="rename")
    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    argv = [x for arg in argv for x in (arg.split(":", 1) if arg.startswith("-fmt:") else [arg])]  # bz.exe style
    a = build_parser().parse_args(argv)
    cmd = a.command
    try:
        if cmd in ("a", "c"):
            if not a.files:
                print("no input files", file=sys.stderr)
                return 2
            if cmd == "c" and os.path.exists(a.archive):
                os.remove(a.archive)
            files = a.files
            if cmd == "a" and os.path.exists(a.archive):
                return _add_existing(a)
            level = 0 if a.level == 0 else max(1, min(9, a.level))
            parts = formats.create(a.fmt if cmd == "c" else "zip", files, a.archive, level=max(1, level),
                                   password=a.password, store_only=a.level == 0, split_size=a.volume, progress=_bar)
            print(f"\ncreated: {', '.join(parts)}")
        elif cmd in ("x", "e"):
            with formats.open_any(a.archive, a.password) as z:
                if cmd == "x":
                    z.extract(a.out, a.files or None, progress=_bar, overwrite={"all": True, "skip": False, "rename": "rename"}[a.ow])
                else:
                    _extract_flat(z, a.out, a.files or None)
            print("\ndone")
        elif cmd == "t":
            with formats.open_any(a.archive, a.password) as z:
                bad = z.test(progress=_bar)
            print("\nOK" if not bad else "\nBROKEN: " + ", ".join(bad))
            return 0 if not bad else 1
        elif cmd == "l":
            with formats.open_any(a.archive, a.password) as z:
                for e in z.entries():
                    print(f"{e.size:>12} {e.csize:>12} {e.method:<8} {'*' if e.encrypted else ' '} {e.name}")
        elif cmd == "d":
            targets = set(a.files)
            _rewrite(a.archive, lambda i: None if ziplib.fix_name(i) in targets else i.filename)
            print("deleted")
        elif cmd == "rn":
            if len(a.files) % 2:
                print("rn needs pairs: old new [old new ...]", file=sys.stderr)
                return 2
            mapping = dict(zip(a.files[::2], a.files[1::2]))
            _rewrite(a.archive, lambda i: mapping.get(ziplib.fix_name(i), i.filename))
            print("renamed")
    except ziplib.BadPassword:
        print("wrong or missing password", file=sys.stderr)
        return 3
    except FileNotFoundError as e:
        print(f"not found: {e.filename}", file=sys.stderr)
        return 4
    except ValueError as e:  # format rules, e.g. a single-file format given a folder
        print(str(e), file=sys.stderr)
        return 2
    return 0


def _add_existing(a) -> int:
    """Append files to an existing archive by rebuilding (keeps old entries)."""
    tmp_dir = tempfile.mkdtemp()
    try:
        with ziplib.Archive(a.archive, a.password) as z:
            z.extract(tmp_dir)
        roots = [os.path.join(tmp_dir, n) for n in os.listdir(tmp_dir)] + list(a.files)
        ziplib.create_zip(roots, a.archive, level=max(1, a.level), password=a.password,
                          store_only=a.level == 0, progress=_bar)
        print("\nupdated")
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _extract_flat(z, out, names):
    os.makedirs(out, exist_ok=True)
    for e in z.entries():
        if e.is_dir or (names and e.name not in names):
            continue
        tmp = tempfile.mkdtemp()
        try:
            z.extract(tmp, [e.name])
            src = os.path.join(tmp, *e.name.replace("\\", "/").split("/"))
            shutil.move(src, os.path.join(out, os.path.basename(e.name)))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
