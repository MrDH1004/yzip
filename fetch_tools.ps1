# Puts the external extractors yzip bundles into tools\ (not in git; build.ps1 and CI run this):
#   7z.exe + 7z.dll  7-Zip 26.04 (LGPL + unRAR restriction): RAR/RAR5, ISO, UDF, CAB, MSI, ARJ, LZH, WIM, ...
#                    from the official x64 MSI (admin extract, nothing installed), checked against a pinned SHA-256
#   unegg.exe        EGG (MIT), built from a pinned commit: Store/Deflate/Bzip2/LZMA/AZO, AES/LEA, split volumes
#   unalz.exe        ALZ (MIT), built from a pinned commit: Store/Deflate/ALZ-Bzip2, ZipCrypto, .a00 volumes
# Needs git and Rust (cargo) for the last two; GitHub's Windows runners have both.
param([switch]$Force)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$tools = Join-Path $root "tools"
$need = "7z.exe", "7z.dll", "unegg.exe", "unalz.exe"
if (-not $Force -and -not ($need | Where-Object { -not (Test-Path (Join-Path $tools $_)) })) {
    Write-Output "tools: present"
    return
}

$SEVENZIP_URL = "https://github.com/ip7z/7zip/releases/download/26.04/7z2604-x64.msi"
$SEVENZIP_SHA256 = "0b01334a418654293513449f61d6bfc99e5196ca371e3c3dc961eda57cd535c6"
$RUST_TOOLS = @(
    @{ Name = "unegg"; Url = "https://github.com/alkegi/unegg"; Tag = "0.2.1"; Commit = "9929009ca4d010e8cd88bb796f7a1868162026d4" },
    @{ Name = "unalz"; Url = "https://github.com/alkegi/unalz"; Tag = "0.2.1"; Commit = "30b4ad40c5452c686df69b3b45554de3073e57a1" }
)

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

    # ---- EGG / ALZ ----
    $cargo = (Get-Command cargo -ErrorAction SilentlyContinue).Source
    if (-not $cargo) { $cargo = Join-Path $env:USERPROFILE ".cargo\bin\cargo.exe" }
    if (-not (Test-Path $cargo)) { throw "Rust (cargo) is needed to build unegg / unalz: https://rustup.rs" }
    foreach ($t in $RUST_TOOLS) {
        $dir = Join-Path $work $t.Name
        Run git @("clone", "--quiet", "--depth", "1", "--branch", $t.Tag, $t.Url, $dir)
        $head = (git -C $dir rev-parse HEAD).Trim()
        if ($head -ne $t.Commit) { throw "$($t.Name) $($t.Tag) is $head, expected $($t.Commit)" }
        Run $cargo @("build", "--release", "--locked", "--quiet", "--manifest-path", (Join-Path $dir "Cargo.toml"))
        Copy-Item (Join-Path $dir "target\release\$($t.Name).exe") $tools -Force
        Copy-Item (Join-Path $dir "LICENSE") (Join-Path $tools "licenses\$($t.Name) LICENSE.txt") -Force
        Write-Output "tools: $($t.Name) $($t.Tag)"
    }
} finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
