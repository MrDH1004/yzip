"""Shared Qt surfaces. Point sizes are based on a 10 pt user font.

Appearance owns live palette conversion and scales these sizes proportionally.
"""

COMMON_QSS = """
QWidget { font-family: 'Segoe UI', 'Malgun Gothic'; font-size: 10pt; color: #202D35; }
QDialog { background: #F4F6F7; }
QLabel { background: transparent; }
QPushButton, QToolButton { background: #FFFFFF; color: #202D35; border: 1px solid #DCE3E6;
    border-radius: 6px; padding: 7px 12px; }
QPushButton:hover, QToolButton:hover { background: #F4F6F7; border-color: #64737D; }
QPushButton:pressed, QToolButton:pressed { background: #DCE3E6; }
QPushButton:focus, QToolButton:focus, QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
    border: 2px solid #16776A; }
QPushButton:disabled, QToolButton:disabled { color: #64737D; background: #F4F6F7; }
QPushButton:default, #homeMain, QToolButton#primary, #updBtn { background: #16776A;
    color: #FFFFFF; border: 1px solid #16776A; font-weight: 600; }
QPushButton:default:hover, #homeMain:hover, QToolButton#primary:hover, #updBtn:hover {
    background: #116457; border-color: #116457; }
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit { background: #FFFFFF; color: #202D35;
    border: 1px solid #DCE3E6; border-radius: 5px; padding: 6px; selection-background-color: #16776A;
    selection-color: #FFFFFF; }
QCheckBox, QRadioButton { spacing: 8px; padding: 3px 0; }
QMenu { background: #FFFFFF; color: #202D35; border: 1px solid #DCE3E6; padding: 5px; }
QMenu::item { padding: 8px 26px 8px 12px; }
QMenu::item:selected { background: #F4F6F7; }
QMenu::item:disabled { color: #64737D; }
QMenu::separator { height: 1px; background: #DCE3E6; margin: 4px 8px; }
QTreeWidget { border: none; background: #FFFFFF; alternate-background-color: #F4F6F7;
    color: #202D35; selection-background-color: #16776A; selection-color: #FFFFFF; }
QTreeWidget::item { min-height: 32px; padding: 0 6px; }
QTreeWidget::item:hover { background: #F4F6F7; color: #202D35; }
QTreeWidget::item:selected { background: #16776A; color: #FFFFFF; }
QHeaderView::section { background: #F4F6F7; color: #64737D; border: none;
    border-bottom: 1px solid #DCE3E6; padding: 9px 8px; font-size: 9pt; }
QProgressBar { background: #DCE3E6; border: none; border-radius: 3px; min-height: 6px; max-height: 6px; }
QProgressBar::chunk { background: #16776A; border-radius: 3px; }
QToolTip { background: #FFFFFF; color: #202D35; border: 1px solid #DCE3E6; padding: 6px; }
"""

MAIN_QSS = COMMON_QSS + """
QMainWindow, #home, #homeScroll, #listPage { background: #F4F6F7; }
#homeScroll { border: none; }
#appHeader { background: #FFFFFF; border-bottom: 1px solid #DCE3E6; }
#brand { font-size: 17pt; font-weight: 700; }
#brandHint, #appVersion, #homeHint, #homeFoot, #homeCreateFormats, #homeVersion, #homeActionHint, #archMeta, #searchSummary {
    color: #64737D; font-size: 9pt; }
#homeTitle { font-size: 22pt; font-weight: 600; }
#dropTitle { font-size: 14pt; font-weight: 600; }
#homeDrop { border: 2px dashed #DCE3E6; border-radius: 10px; background: #FFFFFF; }
#homeDrop[hot="true"] { border-color: #16776A; background: #F4F6F7; }
#homeMain, #homeBtn { padding: 11px 22px; }
#formatsLink { color: #16776A; background: transparent; border: none; padding: 4px 0; font-size: 9pt; }
#archHead { background: #FFFFFF; }
#archName { font-size: 15pt; font-weight: 600; }
#archBadge { color: #FFFFFF; font-size: 10pt; font-weight: 700; border-radius: 6px; }
#lockPill { color: #16776A; border: 1px solid #16776A; border-radius: 4px; padding: 2px 7px; font-size: 9pt; }
#crumbBar, #searchBar { background: #FFFFFF; border-bottom: 1px solid #DCE3E6; }
#crumbBar QToolButton { border: none; padding: 5px 7px; }
#crumbLast { font-weight: 600; }
#crumbSep { color: #64737D; }
QToolBar { background: #FFFFFF; border: none; border-bottom: 1px solid #DCE3E6; padding: 6px 14px; spacing: 6px; }
QStatusBar { background: #FFFFFF; color: #64737D; border-top: 1px solid #DCE3E6; font-size: 9pt; }
#updBar { background: #F4F6F7; border-bottom: 1px solid #DCE3E6; }
"""

PROGRESS_QSS = COMMON_QSS + """
QDialog#progressWindow { background: #FFFFFF; }
QToolButton { padding: 4px; }
QToolButton:checked { border-color: #16776A; background: #F4F6F7; }
#progressHint { color: #64737D; font-size: 9pt; }
"""

DIALOG_QSS = COMMON_QSS + """
#dialogTitle { font-size: 16pt; font-weight: 600; }
#dialogHint { font-size: 9pt; color: #64737D; }
QDialogButtonBox { padding-top: 12px; }
"""

PREFERENCES_QSS = DIALOG_QSS + """
#settingsDialog { background: #F4F6F7; }
#settingsTitle { font-size: 20pt; font-weight: 600; }
#settingsSubtitle { font-size: 9pt; color: #64737D; }
#settingsBody, QTabWidget::pane { background: #FFFFFF; border: 1px solid #DCE3E6; border-radius: 6px; }
#settingsNav { background: #F4F6F7; border: none; outline: none; padding: 4px; }
#settingsNav::item { padding: 10px 12px; margin-bottom: 3px; border-radius: 5px; }
#settingsNav::item:hover { background: #DCE3E6; }
#settingsNav::item:selected { background: #16776A; color: #FFFFFF; }
#settingsSectionTitle { font-size: 14pt; font-weight: 600; }
#settingsSectionHint, #settingsFooterHint { font-size: 9pt; color: #64737D; }
#settingsDivider { background: #DCE3E6; border: none; max-height: 1px; }
#settingsScroll, #settingsPage { background: #FFFFFF; border: none; }
#settingsAssocList::item { min-height: 24px; }
#settingsButtons { background: transparent; padding-top: 12px; }
#primaryButton { background: #16776A; color: #FFFFFF; border-color: #16776A; font-weight: 600; }
#primaryButton:hover { background: #116457; border-color: #116457; }
"""
