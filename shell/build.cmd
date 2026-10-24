@echo off
rem Builds the x64 Explorer context-menu handler (yzipshell.dll) and the headless test probe (probe.exe) into shell\build.
rem Finds any Visual Studio / Build Tools with the C++ x64 tools via vswhere (local machine and GitHub runners).
rem build\version.h must exist first (make_version_files.py).
setlocal
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
for /f "usebackq delims=" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSDIR=%%i"
if not defined VSDIR exit /b 1
call "%VSDIR%\VC\Auxiliary\Build\vcvars64.bat" >nul || exit /b 1
cd /d "%~dp0"
if not exist build mkdir build
rc /nologo /fo build\yzipshell.res yzipshell.rc || exit /b 1
cl /nologo /LD /MT /O2 /EHsc /utf-8 /W3 /DUNICODE /D_UNICODE yzipshell.cpp build\yzipshell.res /Fobuild\ /Fe:build\yzipshell.dll /link /DEF:yzipshell.def ole32.lib shell32.lib shlwapi.lib uuid.lib user32.lib gdi32.lib advapi32.lib || exit /b 1
cl /nologo /MT /O2 /EHsc /utf-8 /W3 /DUNICODE /D_UNICODE probe.cpp /Fobuild\ /Fe:build\probe.exe /link ole32.lib shell32.lib shlwapi.lib user32.lib || exit /b 1
echo BUILD OK
