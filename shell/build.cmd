@echo off
rem Builds the x64 Explorer context-menu handler (yzipshell.dll) and the headless test probe (probe.exe) into shell\build.
setlocal
set "VS=%ProgramFiles(x86)%\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
call "%VS%" >nul || exit /b 1
cd /d "%~dp0"
if not exist build mkdir build
cl /nologo /LD /MT /O2 /EHsc /utf-8 /W3 /DUNICODE /D_UNICODE yzipshell.cpp /Fobuild\ /Fe:build\yzipshell.dll /link /DEF:yzipshell.def ole32.lib shell32.lib shlwapi.lib uuid.lib user32.lib gdi32.lib advapi32.lib || exit /b 1
cl /nologo /MT /O2 /EHsc /utf-8 /W3 /DUNICODE /D_UNICODE probe.cpp /Fobuild\ /Fe:build\probe.exe /link ole32.lib shell32.lib shlwapi.lib user32.lib || exit /b 1
echo BUILD OK
