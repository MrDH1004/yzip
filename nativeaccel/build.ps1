param([string]$OutputDirectory = (Join-Path $PSScriptRoot "build"))
$ErrorActionPreference = "Stop"

$root = Split-Path $PSScriptRoot -Parent
$tools = Join-Path $root "tools"
$pinsPath = Join-Path $PSScriptRoot "7zip-pins.json"
$pins = Get-Content -Raw $pinsPath | ConvertFrom-Json

function Assert-Sha256([string]$Path, [string]$Expected) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "required pinned file is missing: $Path" }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $Expected) { throw "SHA-256 mismatch for $Path`: expected $Expected, got $actual" }
}

Assert-Sha256 (Join-Path $tools "7z.dll") $pins.binary_sha256.'7z.dll'
foreach ($header in $pins.header_sha256.PSObject.Properties) {
    $relative = $header.Name.Replace('/', [IO.Path]::DirectorySeparatorChar)
    Assert-Sha256 (Join-Path $tools "7zip-src\$relative") $header.Value
}

$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path -LiteralPath $vswhere)) { throw "vswhere.exe was not found" }
$vsdir = (& $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath).Trim()
if (-not $vsdir) { throw "Visual C++ x64 build tools were not found" }
$vcvars = Join-Path $vsdir "VC\Auxiliary\Build\vcvars64.bat"
if (-not (Test-Path -LiteralPath $vcvars)) { throw "vcvars64.bat was not found: $vcvars" }

New-Item -ItemType Directory -Force $OutputDirectory | Out-Null
$outputDirectoryFull = [IO.Path]::GetFullPath($OutputDirectory)
$intermediate = Join-Path $PSScriptRoot "build"
New-Item -ItemType Directory -Force $intermediate | Out-Null
$builtDll = Join-Path $intermediate "yzip_accel.dll"
$output = Join-Path $outputDirectoryFull "yzip_accel.dll"
$source = Join-Path $PSScriptRoot "yzip_accel.cpp"
$command = "call `"$vcvars`" >nul && cd /d `"$intermediate`" && cl /nologo /LD /std:c++17 /EHsc /O2 /GL /W4 /WX /MT /utf-8 `"$source`" /link /LTCG /MACHINE:X64 /OUT:`"$builtDll`""
& cmd.exe /d /s /c $command
if ($LASTEXITCODE) { throw "MSVC x64 native accelerator build failed (exit $LASTEXITCODE)" }
if (-not (Test-Path -LiteralPath $builtDll -PathType Leaf)) { throw "MSVC reported success but DLL is missing: $builtDll" }
if ($builtDll -ne $output) { Copy-Item -LiteralPath $builtDll -Destination $output -Force }

Copy-Item -LiteralPath $pinsPath -Destination (Join-Path $outputDirectoryFull "7zip-pins.json") -Force
Write-Output "native accelerator: $output"
