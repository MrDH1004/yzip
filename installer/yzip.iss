; yzip installer (Inno Setup 6). Per-user install: no administrator rights, no UAC prompt.
; build.ps1 passes AppVersion (from version.py):  ISCC /DAppVersion=1.2.3 installer\yzip.iss
; First install registers the Explorer menu (and, if ticked, the archive file associations).
; Updates run this same setup with /VERYSILENT: it closes yzip, replaces the files, re-registers the menu
; (the shell extension DLL copy is per build) and starts the new version with --updated.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{B3F7E2A1-6C4D-4E8B-9A15-2D7C8E0F4A63}
AppName=yzip
AppVersion={#AppVersion}
AppVerName=yzip {#AppVersion}
AppPublisher=yzip
AppPublisherURL=https://github.com/MrDH1004/yzip
AppSupportURL=https://github.com/MrDH1004/yzip/issues
AppUpdatesURL=https://github.com/MrDH1004/yzip/releases
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\yzip
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=yzip-setup-{#AppVersion}
SetupIconFile=..\yzip.ico
UninstallDisplayIcon={app}\yzip.exe
UninstallDisplayName=yzip
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "assoc"; Description: "압축 파일(.zip, .7z, .tar 등)을 yzip으로 열기"
Name: "desktopicon"; Description: "바탕 화면에 바로 가기 만들기"; Flags: unchecked

[InstallDelete]
; libraries of the previous build must not linger next to the new ones
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\yzip\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\yzip"; Filename: "{app}\yzip.exe"
Name: "{userdesktop}\yzip"; Filename: "{app}\yzip.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\yzip.exe"; Parameters: "--register-shell"; Flags: runhidden waituntilterminated
Filename: "{app}\yzip.exe"; Parameters: "--associate"; Tasks: assoc; Flags: runhidden waituntilterminated
Filename: "{app}\yzip.exe"; Description: "yzip 실행"; Flags: nowait postinstall skipifsilent
Filename: "{app}\yzip.exe"; Parameters: "--updated"; Flags: nowait; Check: WizardSilent

[UninstallRun]
Filename: "{app}\yzip.exe"; Parameters: "--unregister-all"; Flags: runhidden waituntilterminated; RunOnceId: "yzipUnregister"

[UninstallDelete]
; copies of the Explorer extension (one per build); a copy Explorer still has loaded cannot be deleted
; at that moment and is left behind
Type: filesandordirs; Name: "{localappdata}\yzip\shell"
