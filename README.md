# 그룹웨어 문서 및 메일 백업 및 검색

Windows에서 **서버 관리자 SSH 계정**으로 접속해 지정 직원의 결재 DB 자료와 MBOX 전체를 백업하고, 서버 연결 없이 검색·열람합니다. **v0.4.1의 기본 방식은 SSH**이며 서버 웹페이지·PHP·직원 로그인 창을 사용하지 않습니다.

서버 기본값은 **192.168.0.100 / 포트 22 / SSH 계정 root**입니다. SSH 암호와 백업할 직원의 SEGIO ID는 별도 항목입니다. 이 방식은 관리자가 직원 자료를 백업하는 도구이며 직원 SEGIO 암호를 검증하지 않습니다.

**[SSH 사용 안내](docs/SSH_ARCHIVE.md)** · **[최신 소스 ZIP](https://github.com/soriel1004/segio/archive/refs/heads/main.zip)** · **[Windows 빌드·다운로드](https://github.com/soriel1004/segio/actions)**

**[Windows v0.4.1 직접 다운로드](https://github.com/soriel1004/segio/actions/runs/39402931283/artifacts/12255182407)** (GitHub 로그인 필요). [최종 Actions #25](https://github.com/soriel1004/segio/actions/runs/39402931283)에서 Linux·Windows 시험과 실행 파일 빌드가 통과했습니다.

## Windows에서 실행

1. GitHub Actions의 최신 **성공한 v0.4.1 이상** 실행에서 `SEGIOArchive-Windows`를 받습니다. GitHub 로그인이 필요합니다. 압축을 모두 풀고 `_internal`, `tools`를 포함한 폴더 전체를 유지합니다.
2. `SEGIOArchive.exe`를 실행하고 **SSH로 DB·MBOX 가져오기 (관리자)**를 사용합니다.
3. SSH 서버 `192.168.0.100`, 포트 `22`, 계정 `root`, **서버 SSH 암호**, 백업할 직원의 **SEGIO ID**, Windows 저장 위치를 입력합니다. 개인키 인증도 지원합니다.
4. **로그인하고 수집 시작**을 누르고 처음 접속하는 서버의 공개 지문을 확인합니다. 지문 확인 방법은 [SSH 안내](docs/SSH_ARCHIVE.md)에 있습니다.
5. 수집 후 **보관함 열기**에서 `segio-직원ID` 폴더를 선택하고 메일 제목·보낸 사람·받는 사람 또는 결재 내용을 검색합니다.

서버에 웹 연결 기능을 설치할 필요가 없습니다. 프로그램이 고정 추출 도구를 SSH로 비공개 임시 폴더에 올려 실행합니다. 기존 Python 2.6·psql·DB 설정을 사용하며 DB 암호·인증·원본 파일 권한을 변경하지 않습니다. 서버 관리자 암호·개인키 내용·DB 암호는 Windows 보관본에 저장하지 않습니다.

## 목록·검색 (v0.4.1)

첫 화면에서 **결재 / 메일 탭**, 각 건수와 백업 목록을 표시합니다. **검색** 버튼 또는 Enter로 검색하고 **전체 목록**으로 돌아갑니다. 기본 글씨는 **12px**, 목록은 100건씩 표시합니다.

**기존 백업은 v0.4.1 프로그램의 ‘보관함 열기’에서 해당 폴더를 한 번 선택하세요.** 서버 로그인·자료 재전송 없이 목록·검색 화면만 갱신합니다. 이후 `index.html` 직접 열기에서도 목록·검색·결재 열람이 됩니다. 메일 본문·첨부는 프로그램의 ‘보관함 열기’에서 봅니다. [자세한 갱신 안내](docs/SSH_ARCHIVE.md#목록검색-화면과-기존-백업-갱신-v041)

## 대용량 메일과 이어받기

- 서버에 MBOX 전체 복사본이나 전송 ZIP을 추가로 만들지 않고 **SFTP로 원본을 직접 전송**합니다. 서버 임시 공간에는 결재 보관본과 메일 목록·해시만 저장합니다.
- **이전 전송 이어받기**가 기본입니다. 같은 서버·직원·저장 위치로 다시 시작하면 동일한 스냅샷의 부분 파일부터 이어받고 SHA-256을 검사합니다.
- 메일은 SQLite에 제목·헤더·원본 위치만 색인합니다. 전체 본문·첨부를 중복 저장하거나 모든 메일 목록을 메모리에 올리지 않고 **100건씩 검색, 본문은 열 때 해석**합니다.
- **ZIP도 생성**은 선택 사항이며 기본 해제돼 있습니다. 선택하면 ZIP64로 추가 보관본을 만들며 그만큼 디스크 공간과 시간이 더 필요합니다.
- 서버 메일이 전송 중 변경되면 완료로 처리하지 않습니다. 변경이 적은 시간에 다시 시도하거나 이어받기를 해제해 새 스냅샷을 만듭니다.

40GB 원본이면 Windows에 원본 40GB와 결재 자료·검색 DB·여유 공간이 필요합니다. 실제 40GB 운영 자료는 별도 현장 시험이 필요합니다. 속도는 서버 디스크와 네트워크에 따라 달라집니다.

## 보관되는 자료

```text
segio-koro/
  index.html                   검색·열람 시작 화면
  search/part-*.js             파일 직접 열기용 분할 검색 자료
  catalog.sqlite3              검색 DB·결재 원본 기록·메일 위치
  catalog.json                 결재 목록
  manifest.json                범위·건수·파일 SHA-256
  documents/.../               결재 HTML·DB 원래 값·첨부
  mail/raw/...                 MBOX·EML·기타 파일 원본
  mail/sources.json            원래 이름·경로·파일 해시 대응
```

폴더 전체를 함께 보관합니다. 프로그램의 **보관함 열기**는 `127.0.0.1`에서만 동작하는 로컬 열람 화면을 사용합니다. 서버 SSH 연결이나 직원 계정은 열람 시 필요하지 않습니다. 메일 본문·첨부·개별 EML은 원본에서 읽으며, 전체 MBOX도 그대로 남습니다. 인식하지 못하는 고유 메일 파일은 원본을 보존하고 건수를 표시합니다. 32 MiB를 넘는 개별 메일은 원본 EML을 저장해 호환 메일 프로그램으로 열 수 있습니다.

결재는 지정 ID의 기안·결재선·대결·참조·자동회람·개인 수신 관계와 관련 이력을 읽기 전용 스냅샷으로 조회합니다. 부서 권한만으로 열리는 문서·별도 확장 업무는 현재 범위에 포함하지 않습니다. 전체 PostgreSQL DB 복제본은 아니며, 실제 화면별 접근 권한과 동일하다고 검증한 기능은 아닙니다.

**파일 무결성 검증**은 보관 파일의 SHA-256을 검사합니다. 대표 결재·메일·첨부와 누락 여부를 대조한 뒤 계정 삭제를 결정해야 합니다. `account_deletion_verified=false`가 유지되며 이 프로그램은 계정이나 원본 자료를 삭제하지 않습니다.

## 명령행과 기존 방식

```bash
python -m segio_archive collect-ssh --host 192.168.0.100 --port 22 --ssh-user root --user koro --output archives
python -m segio_archive view archives/segio-koro
python -m segio_archive verify archives/segio-koro
```

SSH 암호는 실행 후 숨김 입력합니다. `--key-file`, `--zip`, `--fresh`, `--no-mail`, `--no-approvals`를 지원합니다. 서버 경로 옵션은 [SSH 안내](docs/SSH_ARCHIVE.md)를 보세요. 경고가 있는 정상 수집은 종료 코드 2이며 받은 자료는 보관됩니다.

이전 직원 웹 로그인 방식은 **DB·MBOX 바로 가져오기**, 화면 수집은 **웹 화면 수집**으로 남아 있습니다. 이전 설치·오류 진단은 [웹 연결 안내](docs/WINDOWS_LOGIN.md), 관리자 CLI 추출은 [DB 보관 안내](docs/DB_ARCHIVE.md), DB 구조 확인은 [DB 진단 안내](docs/DB_EXPORT.md)를 보세요. SSH 기본 방식은 이 웹 연결 도구를 사용하지 않습니다.

## 개발·검증

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
SEGIO_TEST_PHP=php .venv/bin/python tools/run_ci_tests.py
```

GitHub Actions는 Linux·Windows에서 시험한 뒤 Windows EXE를 빌드합니다. `build_windows.ps1`로 Windows에서 직접 빌드할 수 있습니다. 합성 자료의 실제 SSH/SFTP 서버에서 백업·이어받기·지문 확인·메일 원본·제목 검색·본문·첨부를 검사합니다. 실제 Python 2.6.9와 임시 PostgreSQL 16.2에서도 SSH 서버 추출기를 검증했습니다. 자세한 결과와 한계는 [VALIDATION.md](VALIDATION.md)에 기록합니다.

사용자의 실제 CentOS 서버 SSH 접속·40GB 자료 추출과 Windows EXE 현장 실행은 별도 확인이 필요합니다. 업로드된 SEGIO의 인코딩 소스·실행 파일은 배포물에 포함하지 않습니다. 운영 암호·개인키·문서·메일을 채팅에 보내지 마세요.
