"""Exe entry point. "<name>.zip으로 압축하기" (--archive-list LIST --out ZIP) starts compressing before Qt is
imported (PySide6 and the GUI module take ~0.2 s to load), then the progress window follows the running job."""
import os
import sys


def main() -> int:
    import appcore
    settings = appcore.apply_runtime_settings(strict=False)
    args = sys.argv[1:]
    job = None
    if args[:1] == ["--archive-list"] and len(args) >= 4 and args[2] == "--out":
        # Keep the selection available for elevation or GUI fallback.
        with open(args[1], encoding="utf-8") as selection:
            paths = [line.rstrip("\r") for line in selection.read().split("\n") if line.strip("\r")]
        if not paths:
            appcore.read_list(args[1])
            return 0
        targets = appcore.quick_targets(paths, args[3], settings, explicit_zip=True)
        writable = all(appcore.can_write(os.path.dirname(os.path.abspath(out))) for _, out in targets)
        # a folder only administrators may write to (C:\): the same command as administrator, like Bandizip
        # (Windows asks in its UAC prompt); if that is refused, the GUI offers another place
        if not writable and not os.environ.get(
                "YZIP_AUTOACCEPT") and appcore.run_elevated(args):
            return 0
        if writable:
            try:
                paths = appcore.read_list(args[1])
                if not paths:
                    return 0
                job = appcore.quick_compress(paths, args[3])
            except Exception as e:
                appcore.log_exception(e)
                raise
    import yzip_gui
    yzip_gui.finish(yzip_gui.attach_job(job) if job is not None else yzip_gui.main() or 0)
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout and sys.stdout.flush()
    os._exit(code)
