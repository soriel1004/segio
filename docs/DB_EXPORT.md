# DB에서 결재 문서를 일괄 추출하는 방식

서버 내부에서 PostgreSQL을 읽기 전용으로 조회해 대상 퇴사자의 문서를 일괄 추출하고, 관련 서버 파일과 함께 Windows 보관함으로 옮깁니다. 실제 추출 실행은 [서버 DB 일괄 보관 안내](DB_ARCHIVE.md)를 참고하세요. 아래는 그 준비에 사용한 구조 확인·인증 진단 절차와 조사 기록입니다.

사용자가 전달한 완성된 구조 보고서에서 `cell_000`, PostgreSQL `8.4.20`, UTF8, 결재 관련 관계 32개와 읽기 전용 트랜잭션 종료를 확인했습니다. 이 구조를 기준으로 DB 직접 추출기와 정적 HTML/ZIP 보관함을 구현했습니다. 운영 서버의 구조 조회는 성공했으며 실제 문서 추출·Windows 열람·계정 삭제 영향은 아직 운영 자료로 검증하지 않았습니다. 모든 사용자 권한 범위나 원래 인쇄 서식의 동일성을 보장하는 단계는 아닙니다.

## 서버에서 구조 확인

### 기존 SEGIO 접속 설정 재사용

운영 서버 보고서에서 로컬 인증이 `password`로 확인되었으므로, 먼저 SEGIO의 기존 DB 접속 설정을 재사용합니다. 프로세스 조회에서 `No active cell_000 backend titles found`가 나와도 DB가 중지되었다거나 SEGIO가 다른 DB를 사용한다고 단정할 수 없습니다. 짧은 연결은 조회 순간에 보이지 않을 수 있습니다.

업로드한 배포 소스의 `cell000/etc/db.conf`에는 `DBMS`, `HOST`, `DBNAME`, `USER`, `PW`, `OPTION` 항목이 있습니다. 배포본의 값은 비어 있으므로 운영 서버 파일에도 실제 값이 저장되어 있다고 보장하지는 않습니다. 설치 스크립트의 기본 셀 경로는 `/home/segio/cell000`이며 설치 시 변경할 수 있습니다.

[프로젝트 ZIP](https://github.com/soriel1004/segio/archive/refs/heads/main.zip)을 내려받아 다음 두 파일을 서버의 같은 폴더에 복사합니다.

- `tools/export_schema_from_segio_config.py`
- `tools/inspect_approval_schema.sql`

두 파일이 현재 폴더에 있는 상태에서 root로 실행합니다. CentOS의 기존 Python과 `/usr/bin/psql`을 사용하며 추가 패키지를 설치하지 않습니다. 도구는 Python 2.6에서 사용할 수 있는 문법과 API로 작성했으며 실제 Python 2.6 실행 검증은 아직 하지 않았습니다.

```bash
python ./export_schema_from_segio_config.py
```

`/home`, `/etc/shttpd`, `/opt`, `/usr/local` 아래의 일반적인 셀 경로에서 `DBNAME=cell_000`이고 `USER`·`PW`가 채워진 설정이 정확히 하나인 경우 선택합니다. 조건에 맞는 파일을 찾지 못하거나 여러 개면 접속하지 않고 발견한 후보 파일의 **경로만** 표시합니다. 실제 파일 경로를 알고 있다면 명시할 수 있습니다.

```bash
python ./export_schema_from_segio_config.py --config /home/segio/cell000/etc/db.conf
```

설정 파일은 코드로 실행하지 않고 허용된 키와 단순 값만 읽습니다. 다른 DB 이름·원격 호스트·의미를 확인하지 못한 `OPTION`·알 수 없는 `DBMS`·빈 자격증명은 추측하지 않고 중단합니다. 호스트가 비어 있으면 보고서에서 확인된 `/tmp` 소켓을 사용하고, 포트는 확인된 5432만 사용합니다. 한 번의 기존 자격증명 접속이 실패하면 암호를 재시도하거나 다른 계정으로 우회하지 않습니다.

암호는 권한 700의 임시 폴더 안에 권한 600의 전용 libpq 암호 파일로 전달하며, 프로세스 명령줄·화면·보고서에 출력하지 않습니다. 성공·오류 처리 후 이 임시 암호 파일을 지웁니다. 기존 `.pgpass`, `pg_hba.conf`, `pg_ident.conf`, DB 역할·암호·서비스 상태는 변경하지 않습니다. 원본 설정에 암호가 없거나 애플리케이션에서 별도로 변환하는 값이면 이 경로로 접속하지 못할 수 있으며 관리자 접속 정보를 별도로 확보해야 합니다. 설정 파일이나 암호 자체를 전달할 필요는 없습니다.

성공하면 `/tmp/segio-schema-임의값/segio-db-structure.txt` 경로가 `Saved schema report:` 뒤에 표시됩니다. 이 **`segio-db-structure.txt`만** 전달합니다. 문서·첨부·메일 원본 추출은 아직 수행하지 않으며 구조 메타데이터만 수집합니다. 실패하면 화면의 오류 문구를 전달하세요.

### 운영 서버에서 추가로 확인된 인증 상태

사용자 DB 이름은 `cell_000`입니다. `/tmp` 소켓으로 접속해도 `fe_sendauth: no password supplied`가 발생했고, `/var/lib/pgsql/data/pg_hba.conf`에서 다음 인증 방식이 확인되었습니다.

```text
local all all password
host all all 127.0.0.1/32 password
host all all ::1/128 password
```

따라서 현재 서버는 로컬 연결도 DB 암호를 요구합니다. 사용자가 별도 암호를 설정하지 않았다는 설명만으로 무암호 접속이 가능한 상태라고 판단할 수 없습니다. **아래의 기본 로컬 인증 명령만으로는 현재 오류를 해결할 수 없습니다.**

먼저 root에서 실행 중인 `cell_000` 연결의 DB 계정과 연결 방식만 확인합니다. SQL 본문이나 암호를 출력하지 않습니다.

```bash
ps -ww -e -o args | awk '
    $1 ~ /^(postgres|postmaster):$/ && $3=="cell_000" && NF>=4 {
        print "db_user=" $2, "database=" $3, "connection=" $4; found=1
    }
    END {if(!found) print "No active cell_000 backend titles found"}
'
```

활성 연결을 찾지 못하면 `No active cell_000 backend titles found`가 출력됩니다. 이는 서비스 중지나 앱 연결 방식의 증거가 아니며, SEGIO 결재 목록을 새로고침하는 동안 다시 확인할 수 있습니다. `[local]`은 Unix 소켓이며 IP 주소·포트 표시는 TCP 접속입니다. 이 결과를 바탕으로 기존 SEGIO 접속 정보 재사용 또는 관리자용 로컬 OS 신원 인증을 결정합니다. 관리자용 규칙을 추가할 경우에도 `postgres` DB 계정으로 접속하는 다른 Unix 소켓 클라이언트가 있는지 확인해야 합니다. HBA의 DB 계정 조건은 클라이언트 OS 계정 조건과 같지 않으므로 잘못 바꾸면 현재 접속에 영향을 줄 수 있습니다.

`password`를 일괄 `trust`로 바꾸거나 DB 암호를 초기화하는 명령은 제공하지 않습니다. 기존 애플리케이션 접속 방식을 확인한 뒤 변경 범위·백업·설정 재읽기·검증 절차를 정합니다.

### PostgreSQL 8.4: 프로세스 조회만 출력되지 않는 경우

사용자가 `PG_VERSION`은 `8.4`이며 첫 번째 프로세스 조회만 출력되지 않았다고 추가 확인했습니다. 이를 PostgreSQL 미설치나 서비스 중지의 근거로 삼지 않습니다. 기존 `/tmp` 소켓 연결에서는 서버가 암호 인증 오류를 반환했습니다.

프로세스 이름·활성 연결 조건에 의존하는 명령 대신 `tools/diagnose_postgres_local.sh`로 메타데이터와 명령 오류를 한 번에 모읍니다. 이 파일을 서버에 복사한 뒤 root에서 실행합니다.

#### 다운로드한 파일에 `<!DOCTYPE html>`이 나오는 경우

`command not found`, `syntax error near unexpected token newline`, `<!DOCTYPE html>`이 나오면 실행 파일에 웹페이지 HTML이 들어 있는 것입니다. GitHub 화면을 ‘다른 이름으로 저장’하는 방식 대신 [프로젝트 ZIP](https://github.com/soriel1004/segio/archive/refs/heads/main.zip)을 Windows에서 내려받아 압축을 풀고, 그 안의 `tools/diagnose_postgres_local.sh` 원본 파일을 서버로 복사하세요. 기존 파일은 `.html` 등 다른 이름으로 보존한 뒤 새 원본 파일과 구분할 수 있습니다.

서버에서 첫 줄을 확인합니다.

```bash
head -n 1 diagnose_postgres_local.sh
```

정상 파일의 첫 줄은 정확히 `#!/bin/bash`입니다. HTML이나 다른 내용이면 실행하지 말고 ZIP에서 파일을 다시 복사합니다. 구문 검사에 성공했을 때만 Bash로 실행합니다.

```bash
bash -n diagnose_postgres_local.sh && bash diagnose_postgres_local.sh
```

참고로 Git 커밋 `f458bfec92a5d7238eac64af3982f9c9895f6978`에 저장된 이 스크립트 원본의 SHA-256은 `2257fd9aedfd1ea08f8fd974f1d15016241aa5290184132bddf830102ca51a7c`입니다. 이 특정 버전 파일은 `sha256sum diagnose_postgres_local.sh`로 대조할 수 있습니다. 파일의 줄바꿈이나 내용을 편집하면 해시가 바뀌므로 ZIP 원본을 그대로 전송하세요.

#### 정상 파일 실행

```bash
bash diagnose_postgres_local.sh
```

프로젝트 폴더 전체를 복사했다면 `bash tools/diagnose_postgres_local.sh`로 실행합니다. 화면에 표시되는 `/tmp/segio-pg-diagnostic.임의값/report.txt` 파일만 전달하면 됩니다.

스크립트는 프로세스의 이름·PID, 실제 PostgreSQL 소켓과 소유 PID, 해당 실행 파일·작업 경로, 후보 데이터 디렉터리의 버전·인증 방식, `postgres` 계정의 접속 설정 존재 여부, 한 번의 명시적인 로컬 연결 확인을 기록합니다. 명령 실패·파일 부재도 보고서에 표시합니다. 프로세스 이름이 배포판 기본값과 달라도 소켓/PID로 확인하도록 구성했습니다.

암호 값·문서 행·전체 프로세스 명령줄·전체 환경 변수·인증 옵션·개인키를 출력하지 않습니다. PostgreSQL 암호·인증 파일·서비스 상태를 변경하지 않으며, 권한 700의 임시 폴더와 권한 600의 진단 보고서만 생성합니다.

사용자가 전달한 운영 서버 보고서에서는 `psql 8.4.20`, 실행 파일 `/usr/bin/postgres`, 데이터 디렉터리 `/var/lib/pgsql/data`와 `PG_VERSION=8.4`, `/tmp/.s.PGSQL.5432`의 실행 중인 리스너를 확인했습니다. `postgres` OS 계정에는 검사한 접속 환경 변수와 비어 있지 않은 기본 `.pgpass`가 없었고, 명시적인 소켓 연결도 암호를 요구했습니다. 따라서 암호 없는 관리자 접속은 아직 확보되지 않았습니다.

같은 보고서의 `ps` 출력에는 `,ppid=,user=,comm=`라는 헤더와 PID만 표시되었습니다. 구형 procps에서 `-o pid=,ppid=,...`를 원하는 다중 필드로 처리하지 않는 호환성 문제를 수정해 필드마다 별도의 `-o`를 사용하며, 백엔드 조회도 헤더 재정의 없이 `-o args`를 사용합니다. 수정된 스크립트는 모의 구버전 출력과 비밀 값 제외를 검사했으며 운영 서버에서 재실행한 결과는 아직 받지 않았습니다. 위의 짧은 조회 명령만 실행해도 다음 단계에 필요한 현재 DB 계정과 연결 방식을 확인할 수 있으므로 스크립트 전체를 다시 전달할 필요는 없습니다.

### 기본 로컬 인증이 확인된 환경의 실행 절차

아래는 **CentOS 6 서버에 root로 로그인한 상태에서 운영체제의 `postgres` 계정으로 전환하여 기본 로컬 인증을 사용하는 절차**입니다. `-h` 없이 서버 내부 Unix 소켓으로 연결하고 `-w`로 암호 입력을 요청하지 않습니다. `-w`는 인증 검사를 끄거나 빈 암호를 보내는 옵션이 아닙니다.

서버에 설치된 `psql`을 사용합니다. PostgreSQL 암호 생성·변경이나 `pg_hba.conf` 수정은 하지 않습니다. 서버 운영체제·PostgreSQL·Python 업그레이드나 외부 DB 접속 설정도 필요하지 않습니다.

1. GitHub ZIP 안의 `tools/inspect_approval_schema.sql`을 서버에 복사합니다. 아래는 파일을 현재 폴더의 `inspect_approval_schema.sql`이라는 이름으로 복사한 기준입니다. `su` 실행 전에 root 셸이 SQL 파일을 열어 전달하므로 `postgres` 계정에 root 폴더의 접근 권한을 줄 필요가 없습니다.
2. 실제 DB 이름을 모르면 먼저 다음 명령으로 확인합니다.

```bash
su - postgres -c "psql -X -w -U postgres -d postgres -c '\l'"
```

3. 다음 명령의 `실제DB이름`을 확인한 SEGIO DB 이름으로 바꿉니다. `postgres`는 관리용 DB일 수 있으므로 결재 데이터가 그 안에 있다고 가정하지 않습니다. 결과 파일을 덮어쓰지 않도록 관리자 셸에서 실행하는 부분을 다음과 같이 묶습니다.

```bash
(
  umask 077
  set -C
  su - postgres -c "psql -X -w -U postgres -d '실제DB이름' -v ON_ERROR_STOP=1" \
    < inspect_approval_schema.sql \
    > segio-db-structure.txt
)
```

`segio-db-structure.txt`가 이미 있으면 덮어쓰기를 거부합니다. 이 경우 출력 이름을 `segio-db-structure-2.txt` 등 새 이름으로 바꿉니다. 프로젝트 폴더 구조를 그대로 복사했다면 입력 경로를 `tools/inspect_approval_schema.sql`로 바꾸세요.

연결부터 간단히 확인하려면 다음 명령을 사용할 수 있습니다. 기본 로컬 인증이 정상이라면 DB 암호를 묻지 않고 접속 역할과 DB 이름이 `postgres`로 출력됩니다.

```bash
su - postgres -c "psql -X -w -U postgres -d postgres -c 'SELECT current_user, current_database();'"
```

이 절차는 기본 Unix 소켓 연결과 로컬 `postgres` 인증이 구성되어 있다는 전제입니다. 별도 암호를 설정하지 않았다는 사실만으로 Windows에서 TCP 접속도 암호 없이 된다고 판단하지 않습니다. `no password supplied`, `Ident authentication failed`, `Peer authentication failed` 또는 소켓 연결 오류가 나오면 로컬 인증·서비스 상태를 확인해야 합니다. 오류 문구를 확인하고 원인을 진단하며, 자동으로 암호를 설정하거나 `trust`로 인증을 완화하지 않습니다. root가 아닌 계정에서 `su`가 묻는 암호는 운영체제 계정 인증일 수 있으므로, 위 명령은 root 터미널에서 실행하세요.

성공하면 `segio-db-structure.txt`의 끝에 `END_SEGIO_SCHEMA_REPORT_V1` 표시가 있습니다. 마지막 `transaction_read_only` 값은 `on`이어야 합니다. 오류가 난 부분 결과를 완성된 보고서로 간주하지 마세요.

생성된 **`segio-db-structure.txt`만** 전달하면 다음 구현 단계의 테이블 연결 관계를 확인할 수 있습니다. DB 이름·테이블명·컬럼명·키 구조가 포함됩니다. 실제 결재 본문·메일·사용자 행·암호 값·컬럼 기본값·저장 함수 본문·뷰 정의는 추출하지 않습니다. 실제 DB 덤프나 설정 파일을 올릴 필요는 없습니다.

이 SQL은 임시 PostgreSQL 16.2에서 메타데이터 출력·값 제외·읽기 전용 동작을 검증했습니다. CentOS 6의 실제 DB 버전에서는 아직 실행하지 않았습니다. 오류가 나면 오류 내용에 맞춰 구버전 호환성을 조정해야 합니다.

## 확인할 관계

업로드된 배포본의 평문 보조 코드에서 다음 이름을 확인했습니다.

- `approval_document`: 결재 문서 관련 테이블
- `approval_document_backup`: 내부 문서 백업함 관련 테이블
- `approval_doc_view`: 문서 조회 관련 관계
- `approval_format_info`: 문서 서식 정보
- `approval_member_info`: 회원 정보
- `approval_send_box`: 문서 전달·수신 관련 테이블

이 목록은 운영 DB에 동일한 구조가 존재함을 보장하지 않습니다. 보고서는 `approval_`로 시작하는 실제 관계의 컬럼과 키를 보여 주며, 이름이 다른 관계나 권한이 없는 컬럼은 추가 조사가 필요할 수 있습니다. 외래키가 없어도 애플리케이션 코드에서 연결·삭제를 처리할 수 있습니다.

실제 구조에 맞춰 기안·결재 참여·참조·수신 관계를 종합해 문서 ID 집합을 만들고, 대상 문서의 본문·서식·결재선·의견·첨부를 묶어 가져오는 방식으로 구현합니다. 작성자 컬럼 하나만 필터링하면 본인이 결재하거나 수신한 문서가 누락될 수 있습니다.

## DB와 함께 보관할 서버 파일

배포본의 평문 보조 코드에는 다음 경로가 보입니다. 운영 서버의 실제 SEGIO 셀 경로를 기준으로 확인해야 합니다.

```text
<셀 경로>/data/approval/upload/
<셀 경로>/data/approval/app_images/
```

첨부·본문 이미지 외에 직인·개인 서명·회사 로고 등이 다른 위치에 있을 수 있습니다. DB 구조와 실제 렌더링을 확인해 경로를 정합니다. 파일을 폴더째 임의로 사용자 보관함에 넣기보다 대상 문서가 참조하는 파일을 선별하고, 누락·크기·해시를 기록합니다. 이 구조 확인 SQL은 서버 파일을 읽거나 복사하지 않습니다.

## 인증과 메일

SEGIO 사용자의 ID·암호와 PostgreSQL 접속 계정은 별개입니다. 서버에서 관리자가 실행하는 일괄 추출기는 승인된 DB 역할로 연결하고 대상 직원 ID로 범위를 정해야 합니다. 개인 ID·암호로 사용하게 하는 UI를 유지하려면 기존 웹 인증을 한 번 수행한 뒤 DB에서 검증된 해당 사용자 범위를 적용하는 별도 연결이 필요합니다. DB 로그인 성공을 그룹웨어 사용자 인증으로 간주하지 않습니다.

메일은 이 배포본에서 Postfix가 SEGIO 전용 `_mail` 처리기로 전달하도록 설정되어 있습니다. 메일 원문이 PostgreSQL에 저장된다고 확인하지 않았으므로 결재 DB 추출과 별도로 메일 저장 구조를 확인해야 합니다.

DB 일괄 추출로 바꾸더라도 실제 자료 대조·오프라인 열람·복제 환경의 계정 삭제 영향 시험은 별도로 수행합니다. 읽기 전용 일관된 DB 스냅샷과 서버 파일의 수집 시점을 맞추는 방식도 실제 운영 조건에 맞춰 정해야 합니다.
