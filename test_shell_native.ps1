# Builds and runs the native menu tests in a process-private registry sandbox.
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    $vsRoot = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if (-not $vsRoot) { throw 'Visual C++ build tools are required' }
    $vcvars = Join-Path $vsRoot 'VC\Auxiliary\Build\vcvars64.bat'
    New-Item -ItemType Directory -Path 'shell\build' -Force | Out-Null
    $compile = 'call "' + $vcvars + '" >nul && cl /nologo /MT /O2 /EHsc /utf-8 /W3 /DUNICODE /D_UNICODE shell\test_menu.cpp /Foshell\build\test_menu.obj /Fe:shell\build\test_menu.exe /link ole32.lib shell32.lib shlwapi.lib uuid.lib user32.lib gdi32.lib advapi32.lib'
    cmd /c $compile
    if ($LASTEXITCODE) { throw 'Native menu test compilation failed' }
    & '.\shell\build\test_menu.exe' (Join-Path $PSScriptRoot 'README.md') (Join-Path $PSScriptRoot 'testdata\normal.alz')
    if ($LASTEXITCODE) { throw 'Native menu preferences test failed' }
} finally { Pop-Location }
