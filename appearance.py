"""Live Qt appearance. Call apply_appearance(app) after loading/saving settings.

Only UI chrome is translated: item-view contents, editable combos and text
editors are deliberately opaque. Set ``appearance_user_text`` on any additional
widget whose text is supplied by a user (or ``appearance_translate`` to False).
Original values are retained per object, so repeated changes are reversible.
"""
from __future__ import annotations

import re

from PySide6.QtCore import QEvent, QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QAction, QColor, QFont, QPalette
from PySide6.QtWidgets import (QAbstractButton, QApplication, QComboBox, QGroupBox,
                               QLabel, QListWidget, QMenu, QSpinBox, QStatusBar, QTabBar, QTreeWidget, QWidget)


TRANSLATIONS = dict(tuple(part.replace(r"\n", "\n") for part in line.split("|", 1)) for line in r"""
일반|General
언어|Language
테마|Theme
시스템|System
시스템 설정|System
시스템 기본값|System default
밝게|Light
어둡게|Dark
밝은 테마|Light
어두운 테마|Dark
글꼴|Font
글꼴 크기|Font size
글꼴 이름|Font family
기본 글꼴|Default font
한국어|Korean
영어|English
환경설정|Preferences
yzip 환경설정|yzip Preferences
압축|Compression
압축하기|Compress
압축 해제|Extraction
압축 풀기|Extract archive
압축 파일 만들기|Create archive
압축 파일 열기|Open archive
파일을 여기로 끌어다 놓으세요|Drop files here
압축 파일은 바로 열고, 다른 파일·폴더는 새로 압축합니다|Open an archive, or compress other files and folders
새로 압축하기|Create archive
새로 압축|Create archive
열기|Open
풀기|Extract
닫기|Close
취소|Cancel
확인|OK
적용|Apply
저장|Save
찾아보기|Browse
찾아보기…|Browse…
찾아보기...|Browse...
삭제|Delete
초기화|Reset
기본값 복원|Restore defaults
설정 내보내기|Export settings
설정 가져오기|Import settings
내보내기|Export
가져오기|Import
형식|Format
파일 이름|File name
압축 수준|Compression level
분할 압축|Split archive
분할 안 함|Do not split
암호|Password
암호 확인|Confirm password
암호 표시|Show password
제외할 이름 끝|Exclude name suffixes
저장(압축 안 함)|Store (no compression)
암호를 입력하면 AES-256으로 암호화됩니다.|Enter a password to encrypt with AES-256.
암호를 입력하면 AES-256으로 암호화되고 파일 이름도 숨겨집니다.|Enter a password to encrypt with AES-256 and hide file names.
암호가 서로 다릅니다.|Passwords do not match.
암호가 틀렸습니다.|Incorrect password.
분할 크기를 이해할 수 없습니다. 예: 100MB, 1440KB (최소 64KB)|Invalid volume size. Examples: 100MB, 1440KB (minimum 64KB)
풀 위치|Destination
폴더|Folder
폴더 열기|Open folder
옵션 더 보기|More options
모두 덮어쓰기|Overwrite all
이미 있으면 건너뛰기|Skip existing files
이름 자동 변경: name (2).txt|Rename automatically: name (2).txt
스마트 풀기: 최상위 항목이 여럿이면 압축 파일 이름 폴더에 풀기|Smart extraction: use an archive-named folder for multiple top-level items
작업 완료 후 창을 닫지 않기|Keep window open when finished
낮은 우선순위|Low priority
메뉴|Menu
중지|Stop
중지하는 중…|Stopping…
작업을 완료했습니다.|Operation completed.
압축 작업을 완료했습니다.|Compression completed.
압축 풀기를 완료했습니다.|Extraction completed.
검사를 완료했습니다.|Verification completed.
내려받기를 마쳤습니다.|Download completed.
압축 푸는 중...|Extracting...
무결성 검사 중...|Verifying...
추가 중...|Adding...
삭제 중...|Deleting...
파일 추가…|Add files…
선택 항목 삭제|Delete selected items
압축 파일 검사|Test archive
자세한 정보 보기|Show details
상위 폴더|Parent folder
상위 폴더 (Backspace)|Parent folder (Backspace)
더 보기|More
선택 항목 풀기…|Extract selected…
모두 풀기…|Extract all…
업데이트|Updates
업데이트 확인|Check for updates
업데이트 자동 확인|Check for updates automatically
자동 업데이트|Automatic updates
지금 업데이트|Update now
나중에|Later
yzip 업데이트|yzip update
yzip 정보|About yzip
가볍고 빠른 압축 프로그램|A lightweight, fast archive manager
파일 연결|File associations
파일 연결하기|Associate file types
압축 파일 연결|Archive associations
탐색기 우클릭 메뉴 등록|Register Explorer context menu
탐색기 우클릭 메뉴 제거|Remove Explorer context menu
탐색기 메뉴|Explorer menu
모두 선택|Select all
기본 앱 설정 열기|Open default app settings
이름|Name
크기|Size
수정한 날짜|Modified
압축 크기|Packed size
방식|Method
확장자|Extension
파일 형식|File type
지금 여는 프로그램|Current application
연결 가능한 파일 형식 목록|Associable file types
이상 없음: 모든 항목이 정상입니다.|No errors: all items passed verification.
알 수 없는 오류|Unknown error
취소됨|Cancelled
제거했습니다.|Removed.
분할 압축은 수정할 수 없습니다. 먼저 하나로 합쳐 주세요.|Split archives cannot be modified. Combine the volumes first.
파일 추가·삭제는 ZIP 형식에서만 됩니다. 다른 형식은 풀어서 새로 압축해 주세요.|Only ZIP supports adding or deleting files. Extract other formats and create a new archive.
이 위치에는 관리자 권한이 있어야 파일을 만들 수 있습니다. 다른 위치를 선택해 주세요.|Administrator permission is required to create files here. Choose another location.
다른 위치에 저장...|Save elsewhere...
다른 위치에 저장|Save elsewhere
압축할 파일 선택|Select files to compress
추가할 파일|Files to add
yzip을 실행할 때마다 최신 버전을 확인하고, 새 버전이 있으면 팝업으로 묻습니다.|Check for updates on startup and ask before installing a new version.
아래 목록의 파일 확장자를 한꺼번에 yzip으로 여는 연결로 등록합니다.|Register the extensions below to open with yzip.
더블클릭하면 yzip으로 열 압축 파일 형식을 고르세요.|Choose archive types to open with yzip on double-click.
성능|Performance
작업 스레드 수|Worker threads
임시 폴더|Temporary folder
임시 파일 정리|Clean temporary files
압축 형식|Archive format
기본 압축 형식|Default archive format
기본 압축 수준|Default compression level
기본 저장 폴더|Default output folder
저장 폴더|Output folder
기본 해제 폴더|Default extraction folder
완료 후 폴더 열기|Open folder when finished
덮어쓰기|Overwrite
건너뛰기|Skip
이름 변경|Rename
매번 묻기|Ask every time
자동|Automatic
통합|Combined
개별|Separate
여기에 압축 풀기|Extract here
폴더에 압축 풀기|Extract to folder
우클릭 메뉴|Context menu
여러 파일 처리|Multiple files
파일연결|File associations
설정관리|Settings management
탐색기|Explorer
글꼴 크기 (pt)|Font size (pt)
시스템 기본 글꼴|System default font
비워 두면 기본 위치 사용|Leave empty to use the default location
폴더 선택|Choose folder
파일 연결하기 (전체 선택)|Associate file types (select all)
현재 기본 앱|Current default app
전체 선택|Select all
전체 해제|Clear all
Windows 기본 앱 열기|Open Windows default apps
기본 앱 설정|Default app settings
체크는 yzip 연결 등록 여부입니다. Windows에서 지정한 기본 앱은 별도로 표시됩니다. 기존 기본 앱이 유지되면 Windows 기본 앱 설정에서 변경하세요. EXE는 연결 대상에서 제외됩니다.|Checkboxes show yzip registrations; Windows default apps are shown separately. Use Windows default app settings to change an existing default. EXE files are excluded.
탐색기 우클릭 메뉴 사용|Enable Explorer context menu
여기에 풀기|Extract here
폴더에 풀기|Extract to folder
여러 파일 선택 시|When multiple files are selected
하나로 모아서 처리|Process together
파일별로 처리|Process separately
기본 형식|Default format
압축 수준 (0: 저장, 9: 최대)|Compression level (0: store, 9: maximum)
분할 크기|Volume size
압축 후 저장 폴더 열기|Open output folder after compression
해제|Extraction
해제 폴더|Extraction folder
알아서 풀기 (내용에 맞춰 하위 폴더 생성)|Smart extraction (create a subfolder when needed)
같은 이름의 파일|Existing files
물어보기|Ask
해제 후 폴더 열기|Open folder after extraction
낮은 우선순위로 작업|Run at low priority
yzip 임시 파일 정리…|Clean yzip temporary files…
정리 전에 대상 파일과 폴더를 확인할 수 있습니다.|Review the files and folders before cleanup.
현재 버전|Current version
지금 업데이트 확인|Check for updates now
마지막 확인|Last checked
마지막 결과|Last result
확인 기록 없음|No check history
확인 중…|Checking…
최신 버전입니다.|You are up to date.
설정 초기화…|Reset settings…
설정 가져오기 (JSON)…|Import settings (JSON)…
설정 내보내기 (JSON)…|Export settings (JSON)…
초기화와 가져오기는 적용 버튼을 눌러야 저장됩니다. 파일연결과 업데이트 기록은 포함하지 않습니다.|Press Apply to save reset or imported settings. File associations and update history are excluded.
오류 로그 폴더 열기|Open error log folder
라이선스 보기|View licenses
라이선스|Licenses
yzip 및 포함 소프트웨어 라이선스|yzip and bundled software licenses
설정 초기화|Reset settings
환경설정을 기본값으로 되돌릴까요?\n적용을 눌러야 저장됩니다.|Restore default preferences?\nPress Apply to save.
정리할 yzip 임시 파일이 없습니다.|There are no yzip temporary files to clean.
자세히 보기를 눌러 전체 경로를 확인하세요.|Click Show Details to review the full paths.
임시 파일 정리를 마쳤습니다.|Temporary files cleaned.
설정 적용|Apply settings
설정을 저장하지 못했습니다.|Could not save settings.
""".strip().splitlines())

# Anchored patterns keep filenames/error payloads opaque; no substring replacement
# on arbitrary plain text. HTML is split into text nodes before translation.
TRANSLATIONS.update({
    "yzip의 작업 방식과 사용 환경을 설정하세요.": "Choose how yzip works and looks.",
    "변경한 설정은 저장을 누르면 적용됩니다.": "Select Save to apply your changes.",
    "설정 섹션": "Settings sections",
    "언어와 화면 표시를 원하는 방식으로 맞추세요.": "Choose your language and display preferences.",
    "yzip으로 열 파일 형식을 선택하세요.": "Choose which file types to open with yzip.",
    "확장자 또는 파일 형식 검색": "Search extensions or file types",
    "검색 결과가 없습니다.": "No results found.",
    "검색은 목록만 필터링하며 선택한 연결은 유지됩니다.": "Filtering keeps your file association selections.",
    "Windows 탐색기에서 사용할 우클릭 메뉴를 선택하세요.": "Choose the context menu items to show in File Explorer.",
    "새 압축 파일을 만들 때 사용할 기본값입니다.": "Defaults for creating new archives.",
    "압축을 풀 위치와 같은 이름의 파일 처리 방식을 설정하세요.": "Choose an extraction folder and how to handle existing files.",
    "작업에 사용할 시스템 자원과 임시 저장 위치를 관리하세요.": "Manage processing limits and temporary storage.",
    "새 버전을 확인하고 최근 확인 결과를 살펴보세요.": "Check for updates and review the last result.",
    "설정을 백업하거나 복원하고 프로그램 정보를 확인하세요.": "Back up or restore settings and view application information.",
    "압축 파일 작업, 간편하게": "Archive tasks, made simple",
    "압축을 만들고 풀 때 필요한 도구를 한곳에.": "Create and extract archives in one place.",
    "파일이나 폴더를 여기에 놓으세요": "Drop files or folders here",
    "열기·풀기": "Open and extract",
    "압축 만들기": "Create archives",
    "지원 형식 전체 보기": "View all supported formats",
    "상세 지원 형식": "All supported formats",
    "압축 파일을 열고 풀거나, 파일과 폴더를 하나로 모으세요.": "Open and extract archives, or package files and folders together.",
    "내용 보기·풀기  ·  Ctrl+O": "Browse and extract  ·  Ctrl+O",
    "파일·폴더 압축  ·  Ctrl+N": "Archive files and folders  ·  Ctrl+N",
    "홈": "Home",
    "현재 폴더에서 파일 이름 검색": "Search file names in this folder",
    "파일 이름 검색": "Search file names",
    "검색 지우기": "Clear search",
    "라이선스": "Licenses",
    "초기화와 가져오기는 저장 버튼을 눌러야 저장됩니다. 파일연결과 업데이트 기록은 포함하지 않습니다.":
        "Select Save after resetting or importing. Backups exclude file associations and update history.",
    "열기·풀기:": "Open and extract:",
    "만들기:": "Create:",
    "만들기: ZIP · 7Z · TAR 계열 · GZ · BZ2 · XZ · ZST": "Create: ZIP · 7Z · TAR family · GZ · BZ2 · XZ · ZST",
    "만들기: ZIP · 7Z · TAR 계열 · GZ · BZ2 · XZ": "Create: ZIP · 7Z · TAR family · GZ · BZ2 · XZ",
    "중지를 누르면 현재 작업을 중단합니다.": "Select Stop to cancel the current task.",
})

_PATTERNS = [
    (r"검색 결과 ([\d,]+)개 항목만", r"Only the \1 matching items"),
    (r"선택한 ([\d,]+)개 항목만", r"Only the selected \1 items"),
    (r"지원하는 파일 형식 \(([\d,]+)개\)", r"Supported file types (\1)"),
    (r"([\d,]+)개 항목을 압축 파일에서 삭제할까요\?", r"Delete \1 items from the archive?"),
    (r"새 버전 yzip (.+)이 있습니다\.\n지금 업데이트하시겠습니까\?", r"yzip \1 is available.\nUpdate now?"),
    (r"새 버전 (.+)이\(가\) 나왔습니다\. 지금 버전: (.+)", r"New version \1 is available. Current version: \2"),
    (r"최신 버전입니다\. \(yzip (.+)\)", r"You are up to date. (yzip \1)"),
    (r"업데이트 내려받는 중 \((.+)\)", r"Downloading update (\1)"),
    (r"(.+) 형식은 암호를 지원하지 않습니다\.", r"The \1 format does not support passwords."),
    (r"걸린 시간 (.+)", r"Elapsed time \1"),
    (r"([\d,]+)개 선택 \(([\d,]+)개 파일 포함\)", r"\1 selected (\2 files)"),
    (r"([\d,]+)개 선택", r"\1 selected"),
    (r"([\d,]+)개 항목", r"\1 items"),
    (r"(.+?)  ·  파일 ([\d,]+)개  ·  (.+)", r"\1  ·  \2 files  ·  \3"),
    (r"(.+) 파일이 이미 있습니다\. 덮어쓸까요\?", r"\1 already exists. Overwrite?"),
    (r"yzip (.+)\(으\)로 업데이트했습니다\.", r"Updated to yzip \1."),
    (r"새 버전 (.+)", r"New version \1"),
    (r"확인 실패: (.+)", r"Check failed: \1"),
    (r"다음 yzip 임시 파일/폴더 ([\d,]+)개를 삭제할까요\?", r"Delete these \1 yzip temporary files/folders?"),
]
_PREFIXES = {"열 수 없습니다: ": "Cannot open: ", "업데이트를 확인하지 못했습니다.\n": "Could not check for updates.\n",
             "파일 연결을 변경하지 못했습니다.\n": "Could not change file associations.\n",
             "손상된 항목:\n": "Damaged items:\n", "압축하기 ": "Compressing ",
             "풀기 완료: ": "Extracted: ", "완료: ": "Completed: "}
_PREFIXES.update({"열기·풀기: ": "Open and extract: ", "만들기: ": "Create: ", "압축 만들기: ": "Create archives: "})


def translate_text(text: str) -> str:
    """Translate known UI strings, retaining unknown text and dynamic payloads."""
    if text in TRANSLATIONS:
        return TRANSLATIONS[text]
    stripped = text.strip()
    if stripped != text and stripped in TRANSLATIONS:
        return text[:len(text) - len(text.lstrip())] + TRANSLATIONS[stripped] + text[len(text.rstrip()):]
    for prefix, replacement in _PREFIXES.items():
        if text.startswith(prefix):
            return replacement + text[len(prefix):]
    for pattern, replacement in _PATTERNS:
        if re.fullmatch(pattern, text, re.S):
            return re.sub(pattern, replacement, text, flags=re.S)
    for prefix in ("▸ ", "▾ "):
        if text.startswith(prefix):
            return prefix + translate_text(text[len(prefix):])
    if re.match(r"\d+% ", text):
        prefix, rest = text.split("% ", 1)
        return prefix + "% " + translate_text(rest)
    if text.endswith(" - yzip"):
        return translate_text(text[:-7]) + " - yzip"
    if "\n\n" in text:
        first, rest = text.split("\n\n", 1)
        if rest in TRANSLATIONS:
            return first + "\n\n" + TRANSLATIONS[rest]
    if re.search(r"</?(?:div|b|br|a|span)\b", text):
        # Translate the full update sentence before splitting its embedded <b>.
        return "".join(part if part.startswith("<") else translate_text(part)
                       for part in re.split(r"(<[^>]*>)", text))
    return text


_DARK = {"#ffffff": "#20242b", "#1f2328": "#e6edf3", "#57606a": "#b1bac4",
         "#6e7781": "#a7b0bb", "#8c959f": "#9ba6b3", "#d0d7de": "#454d59",
         "#eef1f4": "#343c47", "#f0f2f5": "#303844", "#e9edf2": "#343c47",
         "#f3f5f8": "#303844", "#b6bfc9": "#596575", "#e6e9ee": "#3d4755",
         "#b0b7bf": "#76808d", "#c4c9cf": "#76808d", "#e7efff": "#294267",
         "#dce8ff": "#304c76", "#eef4ff": "#253750", "#d6e3ff": "#3a5375",
         "#fff8c5": "#453b20", "#f2e3a0": "#75632a", "#9a6700": "#e3b341",
         "#d5dbe2": "#454d59", "#fafbfc": "#252b33", "#f2f6ff": "#253750",
         "#f5f7fa": "#303844"}
_DARK.update({"#f4f6f7": "#161e24", "#202d35": "#e6edf3", "#64737d": "#a6b5bf",
              "#dce3e6": "#384650", "#16776a": "#16776a", "#116457": "#116457",
              "#eaf4f1": "#203c36", "#f8fafb": "#1b272f", "#e6eeee": "#344a4c"})


class AppearanceController(QObject):
    requested = Signal()

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        # Keep wrappers for C++-created children (notably QTabBar) alive. Weak
        # keys can disappear while the underlying Qt child still exists, losing
        # its Korean source. QObject destruction releases these records instead.
        self.records = {}
        self.busy = False
        self.language = "ko"
        self.theme = "system"
        self.dark = False
        self.font = QFont(app.font())
        self.default_font = QFont(app.font())
        self.default_palette = QPalette(app.palette())
        self.requested.connect(self.reload, Qt.QueuedConnection)
        app.installEventFilter(self)
        app.styleHints().colorSchemeChanged.connect(self._scheme_changed)
        # setText has no common Qt change event; this also covers hidden menus,
        # status messages and widgets updated while already visible.
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()

    @Slot()
    def reload(self):
        from appcore import load_settings
        settings = load_settings()
        self.language = settings.get("language", "ko")
        self.theme = settings.get("theme", "system")
        self.dark = self.theme == "dark" or (self.theme == "system" and
                    self.app.styleHints().colorScheme() == Qt.ColorScheme.Dark)
        self.font = QFont(self.default_font)
        if settings.get("font_family"):
            self.font.setFamily(settings["font_family"])
        self.font.setPointSizeF(float(settings.get("font_size", 10)))
        self.busy = True
        try:
            self.app.setFont(self.font)
            palette = QPalette(self.default_palette)
            colors = {QPalette.Window: "#20242b", QPalette.WindowText: "#e6edf3",
                      QPalette.Base: "#20242b", QPalette.AlternateBase: "#252b33",
                      QPalette.Text: "#e6edf3", QPalette.Button: "#303844",
                      QPalette.ButtonText: "#e6edf3", QPalette.ToolTipBase: "#303844",
                      QPalette.ToolTipText: "#e6edf3", QPalette.Highlight: "#16776a",
                      QPalette.HighlightedText: "#ffffff", QPalette.PlaceholderText: "#9ba6b3"}
            if not self.dark:
                colors.update({QPalette.Window: "#ffffff", QPalette.WindowText: "#1f2328",
                               QPalette.Base: "#ffffff", QPalette.Text: "#1f2328",
                               QPalette.Button: "#f3f5f8", QPalette.ButtonText: "#1f2328",
                               QPalette.AlternateBase: "#f5f7fa", QPalette.ToolTipBase: "#ffffff",
                               QPalette.ToolTipText: "#1f2328", QPalette.PlaceholderText: "#6e7781"})
            for role, color in colors.items():
                palette.setColor(role, QColor(color))
            palette.setColor(QPalette.Disabled, QPalette.Text, QColor("#76808d"))
            palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#76808d"))
            self.app.setPalette(palette)
        finally:
            self.busy = False
        self.refresh()

    def _scheme_changed(self, *_):
        if self.theme == "system":
            self.reload()

    def _value(self, obj, key, current, transform):
        if obj not in self.records:
            self.records[obj] = {}
            obj.destroyed.connect(lambda _=None, obj=obj: self.records.pop(obj, None))
        records = self.records[obj]
        original, previous = records.get(key, (current, current))
        if current != previous:
            original = current
        result = transform(original)
        records[key] = (original, result)
        return result

    def _style(self, value):
        if self.dark:
            # Keep white-on-accent buttons legible while using a lighter accent
            # for links and status text on a dark surface.
            value = re.sub(r"(?<![\w-])(color\s*:\s*)#16776a\b", r"\1#74c8b8", value, flags=re.I)
            # White foreground on accent buttons/badges must remain white.
            value = re.sub(r"#[0-9a-fA-F]{6}\b", lambda m: _DARK.get(m[0].lower(), m[0]), value)
            value = re.sub(r"(?<![\w-])(color\s*:\s*)#20242b", r"\1#ffffff", value)
        family = self.font.family().replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ")
        value = re.sub(r"font-family\s*:[^;}]+", lambda _: f"font-family: '{family}'", value)
        # Scale the designed type hierarchy, preserving headings and helper text.
        # Reversible source records keep repeated changes from compounding.
        return re.sub(r"font-size\s*:\s*([\d.]+)pt",
                      lambda match: f"font-size: {float(match[1]) * self.font.pointSizeF() / 10:g}pt", value)

    def _text(self, value):
        result = translate_text(value) if self.language == "en" else value
        # Only HTML style attributes are CSS; avoid changing filenames containing hex.
        return re.sub(r"style=(['\"])(.*?)\1", lambda m: "style=" + m[1] + self._style(m[2]) + m[1], result)

    def _property(self, obj, getter, setter, transform=None, key=None):
        current = getter()
        result = self._value(obj, key or setter.__name__, current, transform or self._text)
        if result != current:
            blocked = obj.blockSignals(True)
            try:
                setter(result)
            finally:
                obj.blockSignals(blocked)
            return True
        return False

    def _user_text(self, obj):
        if obj.property("appearance_user_text") or obj.property("appearance_translate") is False:
            return True
        if obj.objectName() in ("archName", "archBadge", "crumbLast"):
            return True
        parent = obj.parent()
        while parent is not None:
            if isinstance(parent, QComboBox) and parent.isEditable():
                return True
            if parent.objectName() == "crumbBar":
                return True
            # ProgressWindow.label contains a filename in the first div while
            # running. Its completion text is trusted UI text.
            if type(parent).__name__ == "ProgressWindow" and isinstance(obj, QLabel):
                return not getattr(parent, "_done", False)
            parent = parent.parent()
        return False

    def _widget(self, obj):
        if isinstance(obj, QWidget):
            if self._property(obj, obj.styleSheet, obj.setStyleSheet, self._style):
                # Qt caches the font inherited from an ancestor's stylesheet.
                # Re-polish its subtree once when a stylesheet actually changes.
                for child in [obj] + obj.findChildren(QWidget):
                    style = child.style()
                    style.unpolish(child)
                    style.polish(child)
                    child.updateGeometry()
            # QApplication supplies the base font; stylesheet rules supply the
            # scaled hierarchy. Reassigning each widget's font here would fight
            # inherited CSS on every paint and overwrite explicit icon fonts.
        if not isinstance(obj, (QWidget, QAction)):
            return
        protected = self._user_text(obj)
        for getter, setter in (("toolTip", "setToolTip"), ("statusTip", "setStatusTip"),
                               ("accessibleName", "setAccessibleName"), ("windowTitle", "setWindowTitle"),
                               ("placeholderText", "setPlaceholderText")):
            if hasattr(obj, getter) and not protected:
                self._property(obj, getattr(obj, getter), getattr(obj, setter))
        if isinstance(obj, (QLabel, QAbstractButton, QAction)) and not protected:
            self._property(obj, obj.text, obj.setText)
        elif isinstance(obj, QLabel) and protected:
            # Rich progress filenames retain their text, but their CSS must follow theme/font.
            self._property(obj, obj.text, obj.setText,
                           lambda s: re.sub(r"style=(['\"])(.*?)\1", lambda m: "style=" + m[1] + self._style(m[2]) + m[1], s))
        if isinstance(obj, (QGroupBox, QMenu)) and not protected:
            self._property(obj, obj.title, obj.setTitle)
        if isinstance(obj, QComboBox) and not obj.isEditable() and not protected:
            for i in range(obj.count()):
                self._property(obj, lambda i=i: obj.itemText(i), lambda s, i=i: obj.setItemText(i, s), key=("combo", i))
        if isinstance(obj, QTabBar):
            for i in range(obj.count()):
                self._property(obj, lambda i=i: obj.tabText(i), lambda s, i=i: obj.setTabText(i, s), key=("tab", i))
        if isinstance(obj, QListWidget) and obj.objectName() == "settingsNav":
            for i in range(obj.count()):
                item = obj.item(i)
                self._property(obj, item.text, item.setText, key=("settingsNav", i))
        if isinstance(obj, QTreeWidget):
            header = obj.headerItem()
            for i in range(obj.columnCount()):
                self._property(obj, lambda i=i: header.text(i), lambda s, i=i: header.setText(i, s), key=("header", i))
        if isinstance(obj, QSpinBox):
            self._property(obj, obj.specialValueText, obj.setSpecialValueText)
        if isinstance(obj, QStatusBar):
            # Qt's statusbar owns its message QTimer (verified on 6.11). Keep
            # its remaining deadline when replacing the visible message.
            remaining = [timer.remainingTime() for timer in obj.findChildren(QTimer)
                         if timer.isActive() and timer.remainingTime() >= 0]
            timeout = max(1, min(remaining)) if remaining else 0
            self._property(obj, obj.currentMessage,
                           lambda text: obj.showMessage(text, timeout), key="statusMessage")

    @Slot()
    def refresh(self):
        if self.busy:
            return
        self.busy = True
        try:
            for widget in self.app.allWidgets():
                self._widget(widget)
                for action in widget.actions():
                    self._widget(action)
        finally:
            self.busy = False

    def eventFilter(self, obj, event):
        if not self.busy and event.type() in (QEvent.Show, QEvent.Polish, QEvent.Paint,
                                               QEvent.WindowTitleChange, QEvent.ToolTipChange):
            self.busy = True
            try:
                self._widget(obj)
                if event.type() == QEvent.Show and isinstance(obj, QWidget):
                    for child in obj.findChildren(QObject):
                        self._widget(child)
            finally:
                self.busy = False
        return False


def apply_appearance(app: QApplication) -> AppearanceController | None:
    """Apply saved general settings now; worker callers are queued to the GUI thread.

    A controller is owned by QApplication for its entire lifetime. On the GUI
    thread this returns it, which also permits deterministic refresh in tests.
    """
    if QThread.currentThread() != app.thread():
        QTimer.singleShot(0, app, lambda: apply_appearance(app))
        return None
    controller = getattr(app, "_yzip_appearance", None)
    if controller is None:
        controller = AppearanceController(app)
        app._yzip_appearance = controller
    controller.reload()
    return controller
