# yzip

가볍고 빠른 Windows 압축 프로그램 (Python · PySide6).

- **형식**: ZIP(AES-256 암호, 분할, ZIP64), 7Z(AES-256, 이름 암호화), TAR / TAR.GZ / TAR.BZ2 / TAR.XZ / TAR.ZST,
  GZ / BZ2 / XZ / ZST를 생성하고, ZIPX / EXE(SFX) / LZH / ISO / ZPAQ 및 RAR·CAB·EGG·ALZ를 포함한 압축 형식을 읽고 풉니다.
  EXE는 자체 압축 실행 파일만 열며 일반 실행 파일은 보호합니다. 풀 때는 확장자가 아니라 파일 내용으로 형식을 알아봅니다.
- **빠름**: 멀티스레드 ZIP·AES·7z/xz(LZMA2) 압축과 풀기.
- **ZIP 복구**: 중앙 디렉터리가 손상되거나 일부 파일이 깨진 ZIP에서 CRC 검증을 통과한 파일을 찾아 별도의 복구 ZIP으로 저장합니다.
- **탐색기 우클릭**: `이름.zip으로 압축하기`(바로 압축), `yzip으로 압축하기…`(형식·암호 선택), 여기에/폴더에 풀기.
  Windows 11에서는 "더 많은 옵션 표시"(Shift+F10) 안에 있습니다.
- **작업 화면**: 홈에서 열기·압축을 시작하고, 대표 지원 형식과 생성 가능한 형식을 구분해 확인합니다.
  압축 파일 안에서는 현재 폴더의 파일 이름을 검색하고, 설정·업데이트·홈 이동을 바로 사용할 수 있습니다.
- **자동 업데이트**: yzip을 실행할 때(일반 실행 또는 압축 파일 실행) GitHub 릴리스를 확인하고, 새 버전이 있으면 팝업으로 알립니다. 사용자가 동의할 때만
  SHA-256을 검증한 설치 파일을 내려받아 업데이트합니다.

## 설치

[Releases](https://github.com/MrDH1004/yzip/releases)에서 `yzip-setup-X.Y.Z.exe`를 받아 실행합니다.
사용자 계정에 설치되므로 관리자 권한이 필요 없고, 설치하면 우클릭 메뉴가 등록됩니다.
현재 설치 파일은 코드 서명 없이 배포되므로 처음 실행할 때 SmartScreen 경고가 나올 수 있습니다.

## 환경설정

창 위쪽의 **환경설정**에서 설정합니다. 왼쪽의 8개 섹션에서 항목을 선택한 뒤 **저장**을 누릅니다.
파일 연결 섹션은 확장자 목록을 화면에 넓게 표시하며, 항목별 체크 또는 전체 선택·해제로 연결을 지정합니다.

- **일반**: 한국어·영어, 시스템·밝은·어두운 테마, 글꼴과 글자 크기.
- **파일 연결**: 지원 형식 전체 목록과 현재 연결 상태, 확장자별 선택, 전체 선택·해제, Windows 기본 앱 설정.
- **탐색기 메뉴**: 등록 여부, 압축·여기에 풀기·폴더에 풀기 항목, 여러 파일의 묶음·개별 압축.
- **압축**: 기본 형식·수준·저장 폴더·분할 크기, 완료 후 결과 폴더 열기.
- **압축 해제**: 기본 폴더·스마트 풀기·중복 파일 처리, 완료 후 결과 폴더 열기.
- **성능**: 낮은 우선순위, 작업 스레드 제한, 임시 폴더와 임시 파일 정리.
- **업데이트**: 현재 버전, 실행 시 자동 확인, 수동 확인과 마지막 확인 결과.
- **설정 관리**: 기본값 복원, JSON 설정 내보내기·가져오기, 오류 로그와 오픈소스 라이선스.

설정은 사용자 계정에 저장하며 암호는 저장하지 않습니다. Windows에서 사용자가 지정한 기본 앱은
확장자 등록만으로 바뀌지 않을 수 있으므로 파일 연결 섹션의 Windows 기본 앱 설정에서 선택합니다.
탐색기에서 파일 이름이 표시되는 ZIP 바로 압축 메뉴는 표시된 이름과 ZIP 형식을 유지합니다.

## 손상된 ZIP 복구

ZIP을 연 뒤 **더 보기 > 손상된 ZIP 복구…**를 선택합니다. 중앙 디렉터리가 손상돼 ZIP을 열 수 없는 경우에는
파일을 열 때 복구를 시도할지 묻습니다. 복구 결과는 기본적으로 원본 옆의 `파일명_recovered.zip`에 저장하며
원본 파일은 수정하지 않습니다.

현재 복구 범위는 암호화되지 않은 ZIP 및 ZIP64의 Store·Deflate 항목입니다. 정상 중앙 디렉터리가 있으면
그 목록에 있는 항목만 검사하고, 중앙 디렉터리를 읽을 수 없을 때 로컬 헤더를 찾아 복구합니다. 각 파일의 CRC와
원본 크기가 모두 맞는 항목만 새 ZIP에 넣습니다. 암호화된 항목, 지원하지 않는 압축 방식, CRC가 틀리거나 데이터가 잘린 항목은
건너뛰고 결과에 표시합니다. 손상된 데이터 자체를 추정해 고치거나 RAR·7z를 복구하는 기능은 포함하지 않습니다.

## Code signing policy

현재 Windows 설치 파일과 프로그램 파일은 코드 서명 없이 배포합니다.
당분간 별도의 코드 서명 서비스를 사용하지 않으며, 서명 적용 시 이 문서를 갱신합니다.

모든 릴리스는 태그된 소스에서 GitHub Actions가 빌드하고, 설치 파일과 SHA-256 검증 파일을 함께 제공합니다.
개인정보 처리는 아래 [개인정보 처리 방침](#개인정보-처리-방침)을 따릅니다.

## 개인정보 처리 방침

yzip은 다음 한 가지 경우를 빼고는 사용자가 요청하지 않는 한 어떤 정보도 네트워크로 보내지 않습니다.

- **업데이트 확인**: "업데이트 자동 확인"이 켜져 있으면 yzip을 실행할 때마다
  `api.github.com`에서 최신 릴리스 정보를 받아 옵니다. 일반 HTTPS 요청 정보(IP 주소, 프로그램 버전이 담긴
  User-Agent)만 전달되고, 파일 이름·압축 내용·사용 기록은 보내지 않습니다. 이 요청에는
  [GitHub 개인정보 처리 방침](https://docs.github.com/site-policy/privacy-policies/github-general-privacy-statement)이 적용됩니다.
- 설치 화면과 프로그램의 "더 보기 > 업데이트 자동 확인"에서 끌 수 있습니다.
- 업데이트 팝업에서 동의하면 GitHub 릴리스에서 설치 파일을 내려받아 SHA-256을 검증한 뒤 설치합니다.

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

## Native archive support

RAR/RAR5, ISO/UDF, CAB, LHA, CPIO, RPM and DEB are read in-process through the bundled libarchive DLL.
EGG and ALZ are read in-process through the bundled Rust module (`yzip_native.dll`, embedding `unegg` and `unalz`).
RAR, EGG and ALZ remain extraction-only; archive creation is not advertised for those formats. Encrypted RAR is detected and rejected safely because the bundled reader does not implement RAR decryption.

ZPAQ is read, tested and extracted through the bundled x64 `zpaq.exe` compatibility reader. ZIP/7Z self-extracting EXE files are accepted only when an archive signature is found inside the executable; ordinary EXE files are rejected.
The remaining 7-Zip-only formats use the bundled 7-Zip compatibility reader until a dedicated in-process backend is added.
