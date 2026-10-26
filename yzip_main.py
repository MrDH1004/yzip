"""Exe entry point. "<name>.zip으로 압축하기" (--archive-list LIST --out ZIP) starts compressing before Qt is
imported (PySide6 and the GUI module take ~0.2 s to load). The progress window only appears after 0.3 s anyway,
so a small job finishes with no Qt at all; Qt is loaded only when the job runs longer, fails, or the window
is set to stay open ('작업 완료 후 창을 닫지 않기')."""
import os
import sys
import time


def main() -> int:
    args = sys.argv[1:]
    job = None
    if args[:1] == ["--archive-list"] and len(args) >= 4 and args[2] == "--out":
        import appcore
        # a folder only administrators may write to (C:\): the same command as administrator, like Bandizip
        # (Windows asks in its UAC prompt); if that is refused, the GUI offers another place
        if not appcore.can_write(os.path.dirname(os.path.abspath(args[3]))) and not os.environ.get(
                "YZIP_AUTOACCEPT") and appcore.run_elevated(args):
            return 0
        if appcore.can_write(os.path.dirname(os.path.abspath(args[3]))):
            try:
                paths = appcore.read_list(args[1])
                if not paths:
                    return 0
                job = appcore.quick_compress(paths, args[3])
                keep = bool(appcore.load_settings().get("keep_open")) and not os.environ.get("YZIP_AUTOACCEPT")
                if job.wait(max(0.0, 0.3 - (time.time() - job.started))) and job.ok and not keep:
                    return 0
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
