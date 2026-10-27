# yzip 빌드: 버전 리소스 → 셸 확장 DLL → PyInstaller exe → 설치 파일(Inno Setup) → SHA-256.
#   -Stage app        DLL과 exe까지 (CI는 이 결과를 SignPath로 서명한 뒤 installer 단계를 실행)
#   -Stage installer  dist\yzip 으로 설치 파일만
#   -Stage all        전부 (기본값)
# 로컬 서명: setup_signing.ps1이 만든 자체 서명 인증서(CurrentUser\My)가 있으면 그것으로 서명하고, 없으면 서명 없이
# 빌드합니다. 타임스탬프는 넣지 않습니다(외부 서버로 해시를 보내지 않음). 공인 서명은 GitHub Actions + SignPath.
param([ValidateSet("all", "app", "installer")][string]$Stage = "all")
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert -ErrorAction SilentlyContinue |
    Where-Object Subject -eq "CN=yzip (self-signed test)" | Select-Object -First 1

function Sign($file) {
    if (-not $cert) { Write-Output "로컬 인증서 없음 - 서명 생략: $file"; return }
    $s = Set-AuthenticodeSignature -FilePath $file -Certificate $cert -HashAlgorithm SHA256
    if ($s.Status -ne "Valid") { throw "서명 실패: $file ($($s.StatusMessage))" }
    Write-Output "서명 완료: $file"
}

# PyInstaller / ISCC log to stderr; in Windows PowerShell 5.1 that counts as an error record, so judge by exit code
function Run($exe, [string[]]$argv) {
    $ErrorActionPreference = "Continue"
    & $exe @argv 2>&1 | Select-Object -Last 1
    $code = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($code) { throw "$exe 실패 (exit $code)" }
}

Push-Location $root
try {
    $version = (python make_version_files.py).Trim()
    if ($Stage -in "all", "app") {
        & "$root\fetch_tools.ps1"  # tools\: 7-Zip compatibility reader, libarchive, yzip_native (pinned)
        cmd /c "`"$root\shell\build.cmd`" 2>nul" | Select-Object -Last 1
        if ($LASTEXITCODE) { throw "셸 확장 빌드 실패" }
        Sign "$root\shell\build\yzipshell.dll"
        # setuptools is excluded: nothing needs it at run time, but bundled, its runtime hook imported it on
        # every launch (~0.15 s of the right-click delay).
        # --clean: a cached build\yzip\yzip.exe kept the old icon after yzip.ico changed.
        Run python @("-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed", "--name", "yzip",
            "--icon", "yzip.ico", "--version-file", "build\version_info.txt", "--add-data", "yzip.ico;.",
            "--add-binary", "shell\build\yzipshell.dll;.", "--add-binary", "tools\7z.exe;tools",
            "--add-binary", "tools\7z.dll;tools", "--add-binary", "tools\libarchive.dll;tools",
            "--add-binary", "tools\yzip_native.dll;tools", "--add-data", "tools\licenses;tools\licenses",
            "--hidden-import", "cliarchive", "--hidden-import", "nativearchive", "--hidden-import", "nativearchive_ffi",
            "--hidden-import", "nativearchive_libarchive", "--hidden-import", "nativearchive_legacy", "--hidden-import", "minifmt",
            "--exclude-module", "PySide6.QtWebEngineCore",
            "--exclude-module", "PySide6.QtQml", "--exclude-module", "PySide6.Qt3DCore",
            "--exclude-module", "setuptools", "--exclude-module", "pkg_resources",
            "--exclude-module", "_distutils_hack", "--hidden-import", "yzip_gui", "yzip_main.py")
        Sign "$root\dist\yzip\yzip.exe"
    }
    if ($Stage -in "all", "installer") {
        $iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") |
            Where-Object { Test-Path $_ } | Select-Object -First 1
        if (-not $iscc) { throw "Inno Setup 6 (ISCC.exe)가 없습니다" }
        Run $iscc @("/Q", "/DAppVersion=$version", "$root\installer\yzip.iss")
        $setup = "$root\dist\yzip-setup-$version.exe"
        Sign $setup
        # the updater checks this; CI writes it again after the installer is signed
        $hash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
        [IO.File]::WriteAllText("$setup.sha256", "$hash  yzip-setup-$version.exe`n")
        Write-Output "설치 파일: $setup"
    }
} finally { Pop-Location }
Write-Output "빌드 완료 ($version, $Stage)"
