# yzip

가볍고 빠른 Windows 압축 프로그램 (Python · PySide6).

- **형식**: ZIP(AES-256 암호, 분할, ZIP64), 7Z(AES-256, 이름 암호화), TAR / TAR.GZ / TAR.BZ2 / TAR.XZ / TAR.ZST,
  GZ / BZ2 / XZ / ZST. 풀 때는 확장자가 아니라 파일 내용으로 형식을 알아봅니다.
- **빠름**: 멀티스레드 ZIP·AES·7z/xz(LZMA2) 압축과 풀기.
- **탐색기 우클릭**: `이름.zip으로 압축하기`(바로 압축), `yzip으로 압축하기…`(형식·암호 선택), 여기에/폴더에 풀기.
  Windows 11에서는 "더 많은 옵션 표시"(Shift+F10) 안에 있습니다.
- **간단한 창**: 압축 파일을 폴더처럼 둘러보고 풀기. 나머지 기능은 "더 보기"에.
- **자동 업데이트**: GitHub 릴리스를 하루 한 번 확인하고, SHA-256을 검증한 설치 파일로 조용히 업데이트합니다.

## 설치

[Releases](https://github.com/MrDH1004/yzip/releases)에서 `yzip-setup-X.Y.Z.exe`를 받아 실행합니다.
사용자 계정에 설치되므로 관리자 권한이 필요 없고, 설치하면 우클릭 메뉴가 등록됩니다.
설치 파일은 자체 서명 인증서로 서명되어 있어 처음 실행할 때 SmartScreen 경고가 나올 수 있습니다.

## 빌드

```powershell
pip install PySide6 pyzipper pycryptodomex py7zr pyinstaller
powershell -ExecutionPolicy Bypass -File build.ps1   # 셸 확장(MSVC) → exe → 설치 파일(Inno Setup 6)
python run_headless_tests.py                          # 화면 없이 전체 테스트
```

버전은 `version.py` 하나에서 관리합니다.
