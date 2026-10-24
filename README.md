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
코드 서명이 적용되기 전까지는 처음 실행할 때 SmartScreen 경고가 나올 수 있습니다 ([Code signing policy](#code-signing-policy)).

## Code signing policy

Windows 설치 파일과 프로그램 파일은 SignPath Foundation의 무료 오픈소스 코드 서명으로 서명할 예정입니다(신청 중).
승인 후에는 다음 문구가 적용됩니다:

> Free code signing provided by [SignPath.io](https://about.signpath.io/), certificate by [SignPath Foundation](https://signpath.org/)

- **빌드**: 서명되는 파일은 모두 이 저장소의 태그된 소스에서 GitHub Actions(`.github/workflows/release.yml`)가 빌드합니다.
  서명 대상은 yzip 자체 바이너리(`yzip.exe`, `yzipshell.dll`, 설치 파일)뿐이며, 함께 배포되는 외부 라이브러리
  (Python, Qt 등)는 원본 그대로입니다.
- **역할**
  - Committers and reviewers: [MrDH1004](https://github.com/MrDH1004)
  - Approvers: [MrDH1004](https://github.com/MrDH1004) — 모든 서명 요청은 릴리스마다 수동 승인합니다.
- **개인정보**: 아래 [개인정보 처리 방침](#개인정보-처리-방침)을 따릅니다.

## 개인정보 처리 방침

yzip은 다음 한 가지 경우를 빼고는 사용자가 요청하지 않는 한 어떤 정보도 네트워크로 보내지 않습니다.

- **업데이트 확인**: "업데이트 자동 확인"이 켜져 있으면 프로그램 창을 열 때 하루 한 번
  `api.github.com`에서 최신 릴리스 정보를 받아 옵니다. 일반 HTTPS 요청 정보(IP 주소, 프로그램 버전이 담긴
  User-Agent)만 전달되고, 파일 이름·압축 내용·사용 기록은 보내지 않습니다. 이 요청에는
  [GitHub 개인정보 처리 방침](https://docs.github.com/site-policy/privacy-policies/github-general-privacy-statement)이 적용됩니다.
- 설치 화면과 프로그램의 "더 보기 > 업데이트 자동 확인"에서 끌 수 있습니다.
- 업데이트를 선택하면 GitHub 릴리스에서 설치 파일을 내려받아 SHA-256을 검증한 뒤 설치합니다.

## 제거

설정 > 앱 > 설치된 앱 > yzip > 제거. 우클릭 메뉴와 파일 연결도 함께 원래대로 돌아갑니다.

## 라이선스

[MIT](LICENSE). 함께 배포되는 외부 구성 요소는 각자의 라이선스를 따릅니다
(Python: PSF, Qt/PySide6: LGPL-3.0, py7zr: LGPL-2.1, pyzipper: MIT, PyCryptodome: BSD/Public Domain).

## 빌드

```powershell
pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File build.ps1   # 셸 확장(MSVC) → exe → 설치 파일(Inno Setup 6)
python run_headless_tests.py                          # 화면 없이 전체 테스트
```

버전은 `version.py` 하나에서 관리합니다.
