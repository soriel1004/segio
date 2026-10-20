# Windows 로그인으로 현재 DB·MBOX 가져오기

Windows에서 **직원의 기존 SEGIO ID·암호**를 입력하면, 인증된 직원 ID로 `cell_000`을 새로 조회하고 그 직원의 MBOX 폴더 전체를 복사합니다. Windows에는 개인 결재 자료와 검색 DB(`catalog.sqlite3`), 원본 메일 파일, 오프라인 열람 화면을 만듭니다. 결재·메일 웹페이지를 순회하지 않으며 브라우저는 기존 SEGIO 로그인에만 사용합니다.

관리자는 연결 기능을 **서버에 한 번 설치**합니다. 직원별 ZIP을 미리 만들거나 자료가 바뀔 때마다 서버에서 추출 명령을 실행할 필요가 없습니다. 기존 `prepare_login_archive.py`와 `bridge.php`는 이전 버전 호환용이며 새 Windows 기본 모드는 사용하지 않습니다.

## 서버에 한 번 설치

[최신 프로젝트 ZIP](https://github.com/soriel1004/segio/archive/refs/heads/main.zip)을 풀고 **`tools` 폴더 전체**를 `/root/tools`에 복사합니다. GitHub HTML 페이지를 스크립트로 저장하지 않습니다. CentOS 6 기본 Python 2.6과 기존 `psql`을 사용하며 추가 Python 패키지 설치는 필요하지 않습니다.

웹 서비스의 **Linux 실행 계정**을 확인합니다. 직원 SEGIO ID나 PostgreSQL 계정과는 다릅니다.

```bash
ps -eo user,comm | awk '$2 ~ /shttpd|httpd|php/ {print}'
```

실제 실행 계정이 `segio`인 경우 root로 다음을 실행합니다. 다른 비-root 계정이면 `--web-user`를 바꿉니다.

```bash
cd /root/tools
python ./install_live_archive.py --web-user segio \
  --config /home/segio/cell000/etc/db.conf
```

`Installed live employee DB/MBOX export: /segio/archive/live.php`가 나오면 설치가 끝났습니다. 설치만으로 직원 자료를 추출하지 않습니다. 업데이트할 때도 최신 `tools` 전체를 복사한 뒤 같은 명령을 실행합니다. 진행 중인 다운로드가 없는 시간에 업데이트합니다.

메일 경로 기본값은 사용자가 알려준 실제 경로에 맞춘 다음 규칙입니다.

```text
/home/segio/cell000/home/{initial}/{user}/mbox
koro → /home/segio/cell000/home/k/koro/mbox
```

`{user}`는 서버가 인증한 정확한 직원 ID, `{initial}`은 그 첫 글자입니다. 다른 셀에서는 지정한 `db.conf`의 셀 경로를 기준으로 같은 규칙을 적용합니다. 규칙이 다르면 설치할 때 `--mail-template '/실제경로/{initial}/{user}/mbox'`를 지정합니다. Windows가 대상 ID나 파일 경로를 서버에 지정하는 기능은 없습니다.

설치 결과:

- 셀의 `folder/html/segio/archive/live.php`: 인증 확인, 추출 요청·진행 조회·다운로드.
- `/var/lib/segio-live-archive/code`: root 소유 고정 추출 코드. 웹 실행 그룹은 읽기만 가능.
- `/var/lib/segio-live-archive/config.json`: 기존 DB 설정 **경로**, 메일 경로 규칙, 요청 검증용 비밀값. DB 암호는 복사하지 않음.
- `/var/lib/segio-live-archive/jobs`: 웹 실행 계정만 접근하는 임시 전송 작업. 직원별 사전 보관본 저장소가 아님.

작업기는 기존 웹 Linux 계정으로 실행합니다. 이 계정이 기존 `db.conf`·문서 첨부·본인 메일 폴더를 읽을 수 있어야 하며 PHP의 `proc_open`을 사용할 수 있어야 합니다. root 웹 서비스는 지원하지 않습니다. 설치기는 기존 파일 권한·DB 암호·`pg_hba.conf`·메일 설정을 변경하거나 sudo 권한을 추가하지 않습니다. PostgreSQL을 Windows에 공개할 필요도 없습니다.

## Windows에서 가져오기

1. 최신 실행 ZIP을 새 폴더에 풀고 `SEGIOArchive.exe`를 실행합니다. 소스 ZIP은 Python 3.12 64비트 설치 후 `run_windows.bat`을 실행합니다.
2. 방식은 **DB·MBOX 바로 가져오기**로 둡니다. **결재 문서**, **메일**은 기본으로 둘 다 선택되며 하나만 가져올 수도 있습니다.
3. 기존 SEGIO 로그인 주소, 직원 ID(예: `koro`), 그 직원의 SEGIO 암호, 저장 폴더를 입력합니다. DB 암호나 서버 관리자 암호는 입력하지 않습니다.
4. 기존 HTTP 주소를 사용할 경우 **암호화되지 않은 HTTP 연결 허용**을 선택합니다. HTTPS에서는 인증서 검증을 유지합니다.
5. **로그인하고 수집 시작**을 누릅니다. 인증 확인 → 현재 DB 조회 → MBOX 전체 복사 → 다운로드·해시 검증 → Windows 색인 순서로 진행합니다.
6. **보관함 열기**에서 검색창에 **메일 제목**을 입력합니다. 종류를 `mail`로 선택하면 메일만 볼 수 있고 보낸 사람·받는 사람·본문도 검색합니다.

저장 이름은 **`segio-koro.zip`**, **`segio-koro`**입니다. 기존 이름이 있으면 `-2`, `-3`을 붙입니다. 다시 실행하면 당시 DB와 MBOX를 새로 가져옵니다. 암호·쿠키는 보관본이나 설정 파일에 저장하지 않습니다. 직접 로그인 옵션도 실제 인증 ID와 입력 ID가 다르면 거부합니다.

기본 로그인 주소:

```text
http://lssgroup.co.kr/icons/app/cms/?html=/home/login.html&shell=/index.shell:483
```

사내에서는 실제 로그인 주소의 호스트를 `192.168.0.100`으로 바꿀 수 있습니다. 전송이 암호화되지 않는 기존 HTTP 서비스는 신뢰하는 사내망에서 사용합니다.

## Windows에 보관되는 자료

```text
segio-koro/
  index.html                     결재·메일 제목/본문 검색
  catalog.sqlite3                개인 검색 DB와 결재 원본 기록
  catalog.json                   열람 목록
  manifest.json                  수집 범위·건수·크기·SHA-256
  documents/.../document.html     결재 본문·결재선
  documents/.../database.json     원래 결재 DB 값
  documents/.../attachments/...   결재 첨부
  mail/sources.json              원래 메일 경로와 보관 파일의 대응·해시
  mail/raw/...                   MBOX 폴더의 모든 일반 파일 원본
  mail/messages/...              메일별 화면·해제한 첨부
  mail/index-issues.json          메일 형식·대용량 색인 확인 사항
```

MBOX 폴더 안의 모든 일반 파일과 하위 폴더 파일을 **내용을 바꾸지 않고** 가져옵니다. Windows 파일명 제약 때문에 보관 이름은 번호를 사용하며 원래 상대 경로·파일명 바이트는 `sources.json`에 남습니다. 표준 MBOX는 `.mbox`, 개별 RFC822 메일은 `.eml`, 나머지는 `.bin`으로 보존합니다. 심볼릭 링크·특수 파일은 따라가지 않고 실패로 표시합니다. 복사 도중 메일이 변경·추가·삭제되면 완료로 처리하지 않으므로 변경이 적은 시간에 재시도합니다.

표준 MBOX/RFC822의 UTF-8·EUC-KR·CP949 제목과 MIME 본문·첨부를 색인합니다. 인식하지 못하는 고유 형식 파일도 원본은 보존하며 `index-issues.json`에 기록합니다. **실제 서버의 메일 파일 형식은 아직 대조하지 않아 모든 고유 형식의 제목 해독까지 검증한 상태는 아닙니다.** 개별 메일이 32 MiB보다 크면 제목·헤더만 색인하며 전체 본문·첨부는 원본 MBOX/EML에 보존합니다. 서버 원본은 수정하지 않습니다.

`cell_000`은 직원 공용 DB이므로 전체 DB를 직원 PC에 복제하지 않습니다. 인증 ID가 기안자·결재선·대결자·참조·자동회람·개인 수신자로 확인되는 결재와 해당 문서의 관련 기록·이력을 한 읽기 전용 DB 스냅샷으로 가져옵니다. 댓글·즐겨찾기·내부 저장 기록만으로 연결된 문서는 대상 선정에서 제외합니다. **이 범위를 직원에게 제공하도록 관리자가 설치하는 방식이며, 인코딩된 SEGIO 원본의 화면별 권한 판정과 동일하다고 검증한 기능은 아닙니다.** 부서 권한만으로 열리는 문서와 별도 확장 업무는 범위에 포함하지 않습니다.

DB 조회 뒤 첨부·메일을 복사하므로 DB와 파일의 동일 시점을 보장하는 전체 서버 백업은 아닙니다. 결재 PDF 자동 생성은 이 모드에 없으며 HTML을 인쇄해 PDF로 저장할 수 있습니다.

## 연결 확인과 오류

로그인한 브라우저에서 아래 주소를 열면 `protocol: segio-live-archive/1`, 본인의 `username`, `capabilities`가 나옵니다. 응답의 `csrf`는 전송하지 마세요.

```text
http://192.168.0.100/segio/archive/live.php?action=info
```

| 결과 | 조치 |
| --- | --- |
| 미설치 / 404 / HTML 응답 | 최신 설치 명령 결과와 셀 웹 경로·PHP 지원 확인 |
| `native_login_required` / `identity_conflict` | 기존 SEGIO 인증 `REMOTE_USER`가 새 PHP 경로에 전달되는지 확인. HTTP 헤더나 입력 ID를 인증값으로 대체하지 않음 |
| `export_process_unavailable` | 웹 실행 계정의 고정 코드 읽기·Python 실행 권한, PHP `proc_open` 확인 |
| `export_failed` | 웹 실행 계정의 DB 설정·첨부 읽기 권한, 기존 `psql` 접속 확인 |
| `mail_path_missing_or_symlink` | 본인의 실제 MBOX 경로·규칙·심볼릭 링크 여부 확인 |
| `mail_changed_retry` | 복사 중 메일 변경. 변경이 적은 시간에 다시 실행 |
| `export_already_running` / `export_busy` | 같은 직원은 한 작업, 전체는 네 작업까지 실행. 완료 후 재시도 |
| `csrf_required` / 세션 만료 | 새로 로그인해 재시도 |
| 해시 불일치 / 전송 중단 | 완료로 저장하지 않음. 다시 실행 |

임시 ZIP은 전달용입니다. 다운로드·검증이 끝나거나 사용자가 취소하면 본인 작업 정리를 요청합니다. 정리가 실패한 완료·실패 작업은 **다음 추출 요청 때** 24시간이 지난 항목을 삭제합니다. 작업기가 강제 종료돼 실행 중 상태로 남으면 관리자가 프로세스 종료를 확인한 뒤 해당 임시 작업을 정리해야 합니다. 원본 DB·메일을 지우는 기능은 없습니다.

네이티브 `$_ENV['REMOTE_USER']` 또는 `$_SERVER['REMOTE_USER']`만 인증값으로 사용합니다. 임의 대상 ID·SQL·명령·경로를 받지 않으며 POST에는 로그인 사용자에 결합한 CSRF 검증을 적용합니다. 다른 직원의 작업 번호로 조회·다운로드·정리는 할 수 없습니다.

운영 서버의 인증 전달과 실제 문서·메일 형식은 설치 후 현장 확인이 필요합니다. 오류가 나면 **오류 코드만** 전달하세요. 암호·쿠키·실제 보관 자료를 업로드할 필요는 없습니다. 계정 삭제 전에는 네트워크를 끊고 대표 문서·메일 제목·본문·첨부를 대조하고 서버 전체 백업을 별도로 확보합니다.
