# Puts the native archive payloads yzip bundles into tools\ (not in git; build.ps1 and CI run this):
#   7z.exe + 7z.dll  7-Zip 26.04, retained only for formats not covered by the native backends
#   libarchive.dll   libarchive 3.8.10, self-contained MSVC x64 DLL with RAR/RAR5, ISO/UDF, CAB, LHA, ...
#   yzip_native.dll  yzip's Rust cdylib embedding the pinned unegg/unalz libraries (EGG / ALZ)
# Every downloaded binary is checked against a pinned SHA-256 before it enters the bundle.
param([switch]$Force)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$tools = Join-Path $root "tools"
$need = "7z.exe", "7z.dll", "libarchive.dll", "yzip_native.dll"
if (-not $Force -and -not ($need | Where-Object { -not (Test-Path (Join-Path $tools $_)) })) {
    Write-Output "tools: present"
    return
}

$SEVENZIP_URL = "https://github.com/ip7z/7zip/releases/download/26.04/7z2604-x64.msi"
$SEVENZIP_SHA256 = "0b01334a418654293513449f61d6bfc99e5196ca371e3c3dc961eda57cd535c6"
$LIBARCHIVE_URL = "https://github.com/li-ruijie/libarchive/releases/download/v3.8.10/libarchive-v3.8.10-windows-msvc-x64-static.zip"
$LIBARCHIVE_SHA256 = "fc6e8d9cb6160c1faab208816f9fcdfc595a6c1232603a8458dc636c2f083028"
$UNEGG_COMMIT = "9929009ca4d010e8cd88bb796f7a1868162026d4"
$UNALZ_COMMIT = "30b4ad40c5452c686df69b3b45554de3073e57a1"

# git / cargo log to stderr; in Windows PowerShell 5.1 that counts as an error record, so judge by exit code
function Run($exe, [string[]]$argv) {
    $ErrorActionPreference = "Continue"
    & $exe @argv 2>&1 | Select-Object -Last 2 | ForEach-Object { Write-Output "  $_" }
    $code = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($code) { throw "$exe $($argv -join ' ') failed (exit $code)" }
}

$work = Join-Path ([IO.Path]::GetTempPath()) "yzip_tools_$PID"
New-Item -ItemType Directory -Force $work, $tools, (Join-Path $tools "licenses") | Out-Null
try {
    # ---- 7-Zip ----
    $msi = Join-Path $work "7z.msi"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $SEVENZIP_URL -OutFile $msi -UseBasicParsing
    $got = (Get-FileHash $msi -Algorithm SHA256).Hash.ToLower()
    if ($got -ne $SEVENZIP_SHA256) { throw "7-Zip MSI checksum mismatch: $got" }
    $p = Start-Process msiexec -ArgumentList "/a", "`"$msi`"", "/qn", "TARGETDIR=`"$work\7z`"" -Wait -PassThru
    if ($p.ExitCode) { throw "msiexec /a failed (exit $($p.ExitCode))" }
    $src = Join-Path $work "7z\Files\7-Zip"
    Copy-Item "$src\7z.exe", "$src\7z.dll" $tools -Force
    Copy-Item "$src\License.txt" (Join-Path $tools "licenses\7-Zip License.txt") -Force
    Write-Output "tools: 7-Zip 26.04"

    # ---- libarchive ----
    $libzip = Join-Path $work "libarchive.zip"
    Invoke-WebRequest -Uri $LIBARCHIVE_URL -OutFile $libzip -UseBasicParsing
    $got = (Get-FileHash $libzip -Algorithm SHA256).Hash.ToLower()
    if ($got -ne $LIBARCHIVE_SHA256) { throw "libarchive checksum mismatch: $got" }
    $libdir = Join-Path $work "libarchive"
    Expand-Archive -LiteralPath $libzip -DestinationPath $libdir
    $libdll = Get-ChildItem $libdir -Recurse -Filter "libarchive.dll" | Select-Object -First 1
    if (-not $libdll) { throw "libarchive.dll is missing from the checked archive" }
    Copy-Item $libdll.FullName (Join-Path $tools "libarchive.dll") -Force
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/libarchive/libarchive/v3.8.10/COPYING" `
        -OutFile (Join-Path $tools "licenses\libarchive COPYING.txt") -UseBasicParsing
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/li-ruijie/libarchive/master/LICENSE" `
        -OutFile (Join-Path $tools "licenses\libarchive-prebuilt LICENSE.txt") -UseBasicParsing
    Write-Output "tools: libarchive 3.8.10 ($LIBARCHIVE_SHA256)"

    # ---- EGG / ALZ native module ----
    $cargo = (Get-Command cargo -ErrorAction SilentlyContinue).Source
    if (-not $cargo) { $cargo = Join-Path $env:USERPROFILE ".cargo\bin\cargo.exe" }
    if (-not (Test-Path $cargo)) { throw "Rust (cargo) is needed to build yzip_native.dll: https://rustup.rs" }
    $manifest = Join-Path $root "nativearchive\Cargo.toml"
    $vsdev = Get-ChildItem "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat" `
        -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($vsdev) {
        $cmdline = "call `"$($vsdev.FullName)`" -arch=x64 >nul && `"$cargo`" +stable-x86_64-pc-windows-msvc build --release --locked --manifest-path `"$manifest`""
        Run "cmd.exe" @("/d", "/s", "/c", $cmdline)
    } else {
        Run $cargo @("+stable-x86_64-pc-windows-msvc", "build", "--release", "--locked", "--manifest-path", $manifest)
    }
    Copy-Item (Join-Path $root "nativearchive\target\release\yzip_native.dll") (Join-Path $tools "yzip_native.dll") -Force
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/alkegi/unegg/$UNEGG_COMMIT/LICENSE" `
        -OutFile (Join-Path $tools "licenses\unegg LICENSE.txt") -UseBasicParsing
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/alkegi/unalz/$UNALZ_COMMIT/LICENSE" `
        -OutFile (Join-Path $tools "licenses\unalz LICENSE.txt") -UseBasicParsing
    Write-Output "tools: yzip_native.dll (unegg $UNEGG_COMMIT, unalz $UNALZ_COMMIT)"
} finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
