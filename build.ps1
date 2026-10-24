# yzip 전체 빌드: 셸 확장 DLL 빌드 → DLL 서명 → PyInstaller → yzip.exe 서명 → 설치 파일(Inno Setup) → 서명 + SHA-256.
# 서명 인증서는 setup_signing.ps1이 만든 자체 서명 인증서(CurrentUser\My)를 씁니다. 없으면 서명 없이 빌드합니다.
# 타임스탬프는 넣지 않습니다(외부 서버로 해시를 보내지 않음; 테스트용 인증서라 의미도 없음).
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
    Where-Object Subject -eq "CN=yzip (self-signed test)" | Select-Object -First 1

function Sign($file) {
    if (-not $cert) { Write-Output "인증서 없음 - 서명 생략: $file"; return }
    $s = Set-AuthenticodeSignature -FilePath $file -Certificate $cert -HashAlgorithm SHA256
    if ($s.Status -ne "Valid") { throw "서명 실패: $file ($($s.StatusMessage))" }
    Write-Output "서명 완료: $file"
}

cmd /c "`"$root\shell\build.cmd`" 2>nul" | Select-Object -Last 1
if ($LASTEXITCODE) { throw "셸 확장 빌드 실패" }
Sign "$root\shell\build\yzipshell.dll"

Push-Location $root
try {
    # setuptools is excluded: nothing needs it at run time, but bundled, its runtime hook imported it on every
    # launch (~0.15 s of the right-click delay).
    # PyInstaller logs to stderr; in Windows PowerShell 5.1 that counts as an error record, so judge by exit code
    $ErrorActionPreference = "Continue"
    cmd /c "python -m PyInstaller --noconfirm --onedir --windowed --name yzip --icon yzip.ico --add-data `"yzip.ico;.`" --add-binary `"shell\build\yzipshell.dll;.`" --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtQml --exclude-module PySide6.Qt3DCore --exclude-module setuptools --exclude-module pkg_resources --exclude-module _distutils_hack --hidden-import yzip_gui yzip_main.py 2>&1" |
        Select-Object -Last 1
    $code = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($code) { throw "PyInstaller 실패 (exit $code)" }
} finally { Pop-Location }
Sign "$root\dist\yzip\yzip.exe"

# installer: version from version.py, then sign it and publish its SHA-256 next to it (the updater checks it)
$version = (Select-String -Path "$root\version.py" -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 (ISCC.exe)가 없습니다" }
$ErrorActionPreference = "Continue"
& $iscc /Q "/DAppVersion=$version" "$root\installer\yzip.iss"
$code = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($code) { throw "설치 파일 빌드 실패 (exit $code)" }
$setup = "$root\dist\yzip-setup-$version.exe"
Sign $setup
$hash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
[IO.File]::WriteAllText("$setup.sha256", "$hash  yzip-setup-$version.exe`n")
Write-Output "설치 파일: $setup"
Write-Output "빌드 완료 ($version)"
