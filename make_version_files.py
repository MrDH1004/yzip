"""Version resources from version.py, so every binary carries ProductName "yzip" and the same ProductVersion
(SignPath enforces both): build/version_info.txt for PyInstaller (--version-file) and shell/build/version.h
for the shell extension's .rc."""
import os

from version import __version__

HERE = os.path.dirname(os.path.abspath(__file__))
nums = [int(x) for x in __version__.split(".")] + [0] * 4
v4 = tuple(nums[:4])

os.makedirs(os.path.join(HERE, "build"), exist_ok=True)
with open(os.path.join(HERE, "build", "version_info.txt"), "w", encoding="utf-8") as f:
    f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={v4}, prodvers={v4}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'yzip'),
      StringStruct('FileDescription', 'yzip archiver'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('InternalName', 'yzip'),
      StringStruct('LegalCopyright', 'Copyright (c) 2026 MrDH1004. MIT License.'),
      StringStruct('OriginalFilename', 'yzip.exe'),
      StringStruct('ProductName', 'yzip'),
      StringStruct('ProductVersion', '{__version__}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""")

os.makedirs(os.path.join(HERE, "shell", "build"), exist_ok=True)
with open(os.path.join(HERE, "shell", "build", "version.h"), "w", encoding="ascii") as f:
    f.write(f'#define YZ_VER {",".join(map(str, v4))}\n#define YZ_VER_STR "{__version__}"\n')
print(__version__)
