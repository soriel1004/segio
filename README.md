# SEGIO 문서·메일 보관 프로그램

Windows에서 직원의 **SEGIO ID·암호**로 로그인해 현재 결재 DB 자료와 서버 MBOX 전체를 가져오고, 계정 없이 검색·열람하는 프로그램입니다. 기본 방식은 **DB·MBOX 바로 가져오기**입니다. 결재·메일 웹페이지를 순회하지 않으며 기존 SEGIO 로그인에만 브라우저를 사용합니다.

**서버에는 연결 기능을 한 번 설치합니다.** 이후 Windows에서 요청할 때마다 인증된 직원의 자료를 새로 추출합니다. 직원별 보관 ZIP을 미리 만들 필요가 없습니다. 자세한 명령과 오류 확인은 **[서버 설치·Windows 실행 안내](docs/WINDOWS_LOGIN.md)**를 보세요.

## 서버에 한 번 설치

[최신 소스 ZIP](https://github.com/soriel1004/segio/archive/refs/heads/main.zip)의 **`tools` 전체**를 CentOS 서버 `/root/tools`에 복사합니다. 실제 웹 서비스의 Linux 실행 계정이 `segio`인 경우 root로 실행합니다.

```bash
cd /root/tools
python ./install_live_archive.py --web-user segio \
  --config /home/segio/cell000/etc/db.conf
```

다른 비-root 웹 실행 계정이면 `--web-user`를 바꿉니다. 기존 DB 설정을 사용하며 PostgreSQL 암호·인증 설정·원본 권한은 변경하지 않습니다. 웹 실행 계정이 기존 DB 설정·첨부·메일을 읽을 수 있어야 합니다. 원격 PostgreSQL 공개나 Windows에 DB 암호 저장은 필요하지 않습니다.

메일 기본 경로는 **`/home/segio/cell000/home/{initial}/{user}/mbox`**입니다. `koro`는 사용자가 알려준 `/home/segio/cell000/home/k/koro/mbox`에 대응합니다. 서버가 확인한 로그인 사용자만 대상이 됩니다.

## Windows에서 실행

1. [GitHub Actions](https://github.com/soriel1004/segio/actions)에서 최신 **성공한 v0.3.0 이상** 실행을 열고 `SEGIOArchive-Windows` 아티팩트를 받습니다. 압축을 풀어 폴더 전체를 유지하고 `SEGIOArchive.exe`를 실행합니다.
2. 소스 ZIP으로 실행하려면 Python 3.12 64비트를 설치한 뒤 새 폴더에 압축을 풀고 `run_windows.bat`을 실행합니다. 로그인 창을 열기 전에 Chromium을 다운로드하지 않습니다.
3. **DB·MBOX 바로 가져오기**에서 서버 주소, 직원 SEGIO ID·암호, 저장 폴더를 입력합니다. **결재 문서**, **메일**은 둘 다 기본 선택이며 하나만 선택할 수도 있습니다.
4. **로그인하고 수집 시작**을 누릅니다. 로그인 → 현재 DB 읽기 → MBOX 전체 복사 → 다운로드·무결성 검증 → Windows 검색 DB 생성 순서로 진행합니다.
5. **보관함 열기**에서 검색창에 **메일 제목**을 입력하면 해당 메일이 표시됩니다. 종류를 `mail`로 선택하면 메일만 검색하며 보낸 사람·받는 사람·본문도 검색합니다.

저장 이름은 **`segio-koro`**, **`segio-koro.zip`**처럼 짧게 만듭니다. 이름이 겹치면 `-2`, `-3`을 붙여 기존 자료를 보존합니다. 다시 실행하면 당시 자료를 새로 조회합니다.

기본 로그인 주소:

```text
http://lssgroup.co.kr/icons/app/cms/?html=/home/login.html&shell=/index.shell:483
```

사내망에서는 실제 로그인 주소의 호스트를 `192.168.0.100`으로 바꿀 수 있습니다. 채팅 주소의 `&amp;`는 자동 정규화합니다. HTTP는 **암호화되지 않은 HTTP 연결 허용**을 명시적으로 선택하고 신뢰하는 사내망에서 사용합니다. HTTPS 인증서 검증은 유지합니다. DB·관리자 암호는 입력하지 않으며 직원 암호·쿠키도 저장하지 않습니다.

로그인에는 설치된 Edge → Chrome → 전용 Chromium 순서로 사용 가능한 브라우저를 찾고 별도 임시 프로필을 사용합니다. 기존 개인 브라우저를 종료할 필요는 없습니다. 과거 `.browser\__dir_lock` 다운로드 오류가 발생했다면 최신 소스를 새 폴더에 풀어 실행하세요. Edge·Chrome이 없는 PC에서만 `install_browser_windows.bat`으로 전용 브라우저를 선택 설치합니다.

## 가져오는 자료

- **결재 DB**: 기안·결재선·대결·참조·자동회람·개인 수신 관계의 문서와 관련 이력·댓글·표시 정보. 본문·결재선 HTML, 원래 DB 값, 첨부를 보관합니다.
- **메일 원본 전체**: 직원 MBOX 폴더 아래 모든 일반 파일과 하위 폴더 파일을 바이트 그대로 보존합니다. 원래 이름·상대 경로와 보관 파일의 대응 및 해시는 `mail/sources.json`에 저장합니다.
- **메일 제목 검색**: 표준 MBOX/RFC822의 UTF-8·EUC-KR·CP949 제목·헤더·본문·첨부를 Windows에서 색인합니다. 서버 웹메일을 열거나 읽음 상태를 바꾸지 않습니다.
- **개인 로컬 DB**: `catalog.sqlite3`에 검색 정보와 결재 원본 JSON을 넣습니다. 서버 DB 암호나 다른 직원의 무관한 자료는 포함하지 않습니다.

`cell_000`은 직원 공용 DB입니다. 전체 DB 복제 대신 로그인 직원에게 관련된 결재 행을 한 읽기 전용 DB 스냅샷으로 추출합니다. 댓글·즐겨찾기·내부 저장 기록만으로 연결된 문서는 대상 선정에서 제외합니다. 설치 관리자가 승인하는 개인 기록 보관 범위이며, 인코딩된 SEGIO 원본의 화면별 권한 판정과 동일하다고 검증된 기능은 아닙니다. 부서 권한만으로 열리는 문서·확장 업무는 포함하지 않습니다.

메일의 알 수 없는 고유 형식 파일도 원본은 가져오고 `mail/index-issues.json`에 남깁니다. 실제 서버의 고유 파일 형식과 모든 제목의 해독 여부는 현장 대조가 필요합니다. 개별 메일이 32 MiB보다 크면 제목·헤더만 색인하며 전체 본문·첨부는 원본 MBOX/EML에 보존합니다. 복사 중 메일이 변경되면 완료로 처리하지 않으므로 변경이 적은 시간에 다시 실행합니다. 심볼릭 링크와 특수 파일은 거부합니다.

결재·메일 원본을 서버에서 삭제하거나 수정하지 않습니다. DB 조회와 파일 복사는 서로 다른 시점이므로 전체 서버 복구용 백업은 별도로 필요합니다. 이 모드에서 PDF 자동 생성은 제공하지 않으며 결재 HTML을 브라우저로 인쇄할 수 있습니다.

## 보관 폴더와 열람

```text
segio-koro/
  index.html                   결재·메일 검색·열람
  catalog.sqlite3              개인 검색 DB와 결재 원본 기록
  catalog.json                 검색 목록
  manifest.json                범위·건수·파일 해시
  documents/.../               결재 HTML·원래 DB 값·첨부
  mail/raw/...                 MBOX 전체 원본 파일
  mail/sources.json            원래 메일 경로와 보관 파일 대응
  mail/messages/...            메일별 화면·첨부
  mail/index-issues.json        메일 색인 확인 사항
```

폴더 전체를 함께 복사합니다. `index.html`은 일반 브라우저에서 직접 열 수 있습니다. 프로그램의 **보관함 열기**는 127.0.0.1에만 연결되는 임시 열람 서버를 사용하며 인터넷·SEGIO 계정 없이 동작합니다. 생성 화면에서는 원래 스크립트·폼·외부 이미지를 제거합니다. 원본 첨부는 해당 형식의 Windows 프로그램으로 열고 MBOX/EML은 호환 메일 프로그램으로 가져올 수 있습니다.

**파일 무결성 검증**은 받은 파일의 SHA-256을 확인합니다. 운영 서버에 존재하던 자료를 모두 받았는지는 대표 문서·메일함·건수를 별도 대조해야 합니다. `account_deletion_verified`는 항상 `false`이며 이 프로그램은 계정을 삭제하거나 계정 삭제 영향을 승인하지 않습니다. 삭제 전 네트워크를 끊고 본문·결재선·서명·첨부·메일 제목을 확인하고, 서버 전체 백업과 복제 환경의 삭제 영향 확인을 진행하세요.

## 명령행·기존 방식

```powershell
.venv\Scripts\python.exe -m segio_archive collect-db --server "http://lssgroup.co.kr/icons/app/cms/?html=/home/login.html&shell=/index.shell:483" --user koro --output "D:\SEGIO보관함" --allow-http
.venv\Scripts\python.exe -m segio_archive view "D:\SEGIO보관함\segio-koro"
.venv\Scripts\python.exe -m segio_archive verify "D:\SEGIO보관함\segio-koro"
```

암호는 숨김 입력합니다. `collect-db` 옵션은 `--no-mail`, `--no-approvals`, `--manual-login`, `--headless`입니다. 경고가 있는 수집은 종료 코드 2이며 받은 자료는 보관됩니다. 무결성 검사는 정상 0, 오류 2입니다.

관리자 직접 DB 일괄 추출은 [DB 보관 안내](docs/DB_ARCHIVE.md), 구조·접속 진단은 [DB 구조 확인 안내](docs/DB_EXPORT.md)를 보세요. 이전 `prepare_login_archive.py`의 사전 ZIP 전달과 새 실시간 모드는 별개입니다. 선택형 **웹 화면 수집 (결재·메일)** / 명령 `collect`도 이전 호환용으로 남아 있습니다. 이 방식의 메일은 화면과 확보 가능한 EML을 보관하며 원문 미확보·조회에 따른 읽음 상태 변경 가능성을 경고합니다. 현재 요청의 기본 DB·MBOX 모드에서는 웹메일 수집을 사용하지 않습니다.

## 개발·검증과 Windows 빌드

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
SEGIO_TEST_PHP=php .venv/bin/python tools/run_ci_tests.py
```

GitHub Actions는 Python 3.12·PHP 8.4·Playwright로 Linux와 Windows에서 시험한 뒤 Windows EXE를 빌드합니다. `build_windows.ps1`로 Windows에서 직접 빌드할 수도 있습니다. 패키지에는 서버 설치 도구와 안내도 포함합니다. 생성 EXE의 현장 실행과 운영 SEGIO 연결은 별도 확인이 필요합니다.

시험은 합성 DB·메일과 실제 PHP 연결 코드를 사용합니다. 실시간 재조회, MBOX 원본 보존, 한글 제목·MIME 첨부·오프라인 검색, 다른 계정 차단, CSRF·경로·파일 검증을 포함합니다. 검증 기록은 [VALIDATION.md](VALIDATION.md)에 있습니다. 서버 코드는 Python 2.6/PHP 5.3 호환 API로 작성했으나 실제 구버전 런타임은 운영 설치 후 확인해야 합니다.

업로드된 SEGIO의 Zend 인코딩 소스·실행 파일을 배포물에 포함하거나 수정하지 않았습니다. 운영 사이트는 이 클라우드 연결에서 403 응답을 받아 실제 운영 로그인·자료 추출은 검증하지 못했습니다. 운영 암호·실제 문서·메일을 채팅으로 보내지 마세요.
