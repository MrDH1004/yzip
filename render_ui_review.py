"""Render synthetic Qt widgets offscreen, never the user's desktop.

Usage: python render_ui_review.py --output C:/Temp/yzip-ui-review
All settings and sample archives are temporary; no network/update check is run.
"""
import argparse
import json
import os
from pathlib import Path
import tempfile
import zipfile

# Must be set before importing Qt. Do not allow the caller's desktop backend.
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["YZIP_NO_OPEN"] = "1"

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel

import appcore
import appearance
import yzip_gui as gui
from ui_theme import PREFERENCES_QSS


def render(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    font_root = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for font_name in ("segoeui.ttf", "segoeuib.ttf", "malgun.ttf", "malgunbd.ttf", "arial.ttf",
                      "segmdl2.ttf", "SegoeIcons.ttf", "seguisym.ttf"):
        font_path = font_root / font_name
        if font_path.is_file() and QFontDatabase.addApplicationFont(str(font_path)) < 0:
            raise RuntimeError(f"Could not load review font {font_path}")
    if not QFontDatabase.families():
        raise RuntimeError("No fonts available for readable offscreen review")
    report = []

    def capture(widget, name, size=None):
        if size:
            widget.resize(*size)
        widget.show()
        appearance.apply_appearance(app)
        app.processEvents()
        QTest.qWait(160)
        path = output / f"{name}.png"
        if not widget.grab().save(str(path)):
            raise OSError(f"Could not save {path}")
        report.append({"file": path.name, "width": widget.width(), "height": widget.height(),
                       "minimum_width": widget.minimumWidth()})

    with tempfile.TemporaryDirectory(prefix="yzip-render-") as temp:
        root = Path(temp)
        old_settings = os.environ.get("YZIP_SETTINGS")
        os.environ["YZIP_SETTINGS"] = str(root / "settings.json")
        try:
            archive = root / "Project files.zip"
            source = root / "notes.txt"
            source.write_text("Synthetic review content\n", encoding="utf-8")
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
                for name, content in (("documents/guide.txt", "Guide\n" * 240),
                                      ("documents/changelog.txt", "Changes\n" * 120),
                                      ("assets/logo.txt", "Local asset\n" * 70),
                                      ("README.txt", "Read me\n" * 80),
                                      ("release-notes.txt", "Release notes\n" * 96)):
                    z.writestr(name, content)
            for theme in ("light", "dark"):
                appcore.save_settings({"theme": theme, "language": "ko", "font_size": 10,
                                       "font_family": "Segoe UI", "auto_update": False})
                win = gui.Main()
                for width, height in ((960, 680), (640, 454), (640, 420)):
                    capture(win, f"home-{theme}-{width}x{height}", (width, height))
                win.open_archive(str(archive))
                for width, height in ((960, 680), (640, 420)):
                    capture(win, f"archive-{theme}-{width}", (width, height))
                win.search.setText("README")
                capture(win, f"archive-search-{theme}-640", (640, 520))
                win.search.clear()
                win.resize(960, 680)
                prefs = gui.SettingsDialog(win)
                prefs.setStyleSheet(PREFERENCES_QSS)
                for width, height in ((960, 680), (prefs.minimumWidth(), prefs.minimumHeight())):
                    capture(prefs, f"settings-{theme}-{width}", (width, height))
                prefs.tabs.setCurrentIndex(1)
                capture(prefs, f"settings-formats-{theme}-960", (960, 680))
                prefs.close()
                prefs.deleteLater()
                compress = gui.CompressDialog(win, str(root / "output.zip"), [str(source)])
                capture(compress, f"compress-{theme}-640", (640, compress.sizeHint().height()))
                capture(compress, f"compress-{theme}-minimum", (compress.minimumWidth(), compress.sizeHint().height()))
                compress.close()
                compress.deleteLater()
                extract = gui.ExtractDialog(win, str(root / "output"), 2)
                extract.more.setChecked(True)
                capture(extract, f"extract-{theme}-640", (640, extract.sizeHint().height()))
                capture(extract, f"extract-{theme}-minimum", (extract.minimumWidth(), extract.sizeHint().height()))
                extract.close()
                extract.deleteLater()
                progress = gui.ProgressWindow(win, "압축 풀기", str(root))
                progress.on_progress(380000, 1000000, "documents/guide.txt")
                capture(progress, f"progress-{theme}")
                progress.show_done()
                capture(progress, f"progress-done-{theme}")
                progress.close()
                progress.deleteLater()
                win.close()
                win.deleteLater()
                app.processEvents()
                for language in ("ko", "en"):
                    for font_size in (16, 20):
                        appcore.save_settings({"theme": theme, "language": language, "font_size": font_size,
                                               "font_family": "Segoe UI", "auto_update": False})
                        large = gui.Main()
                        prefix = f"home-{theme}-{language}-{font_size}pt-640"
                        capture(large, prefix + "-top", (640, 520))
                        large.home_scroll.ensureWidgetVisible(large.home_open_btn, 0, 4)
                        capture(large, prefix + "-open")
                        large.home_scroll.ensureWidgetVisible(large.home_new_btn, 0, 4)
                        capture(large, prefix + "-new")
                        large.home_scroll.ensureWidgetVisible(large.formats_btn, 0, 4)
                        capture(large, prefix + "-footer")
                        large.close()
                        large.deleteLater()
                        app.processEvents()
        finally:
            if old_settings is None:
                os.environ.pop("YZIP_SETTINGS", None)
            else:
                os.environ["YZIP_SETTINGS"] = old_settings
    (output / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # A local review page is convenient for side-by-side inspection.
    cards = "".join(f'<figure><figcaption>{r["file"]} ({r["width"]}×{r["height"]})</figcaption>'
                    f'<img src="{r["file"]}" loading="lazy"></figure>' for r in report)
    (output / "index.html").write_text('<!doctype html><meta charset="utf-8"><title>yzip UI review</title>'
        '<style>body{font:14px Segoe UI;background:#dce3e6;margin:24px}figure{display:inline-block;'
        'vertical-align:top;margin:12px}img{max-width:640px;display:block;margin-top:8px}</style>' + cards,
        encoding="utf-8")
    print(json.dumps({"output": str(output), "captures": len(report)}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(Path(tempfile.gettempdir()) / "yzip-ui-review-20261028"))
    render(parser.parse_args().output)
