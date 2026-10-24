# 자체 서명 코드 서명 인증서를 한 번 만들고 이 PC(현재 사용자)에서 신뢰하도록 등록합니다.
# 개인 키는 Windows 인증서 저장소(CurrentUser\My)에만 있고 파일로 내보내지 않습니다.
# Root 저장소에 추가할 때 Windows가 확인 창을 띄웁니다("예"를 눌러야 함).
$ErrorActionPreference = "Stop"
$subject = "CN=yzip (self-signed test)"
$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | Where-Object Subject -eq $subject | Select-Object -First 1
if (-not $cert) {
    $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject $subject -CertStoreLocation Cert:\CurrentUser\My `
        -KeyAlgorithm RSA -KeyLength 3072 -HashAlgorithm SHA256 -NotAfter (Get-Date).AddYears(5)
    Write-Output "인증서 생성: $($cert.Thumbprint)"
}
$pub = Join-Path $env:TEMP "yzip_signing_public.cer"   # 공개 인증서만 (개인 키 없음)
Export-Certificate -Cert $cert -FilePath $pub | Out-Null
foreach ($store in "Root", "TrustedPublisher") {
    $have = Get-ChildItem "Cert:\CurrentUser\$store" | Where-Object Thumbprint -eq $cert.Thumbprint
    if (-not $have) {
        Import-Certificate -FilePath $pub -CertStoreLocation "Cert:\CurrentUser\$store" | Out-Null
        Write-Output "신뢰 저장소에 등록: $store"
    }
}
Remove-Item $pub
Write-Output "준비 완료: $($cert.Subject) / $($cert.Thumbprint)"
