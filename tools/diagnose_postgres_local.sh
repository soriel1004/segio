#!/bin/bash
# CentOS 6 / Bash 4 / PostgreSQL 8.4 read-only diagnostics.
# Writes only a private report. Does not change authentication or database data.
set -u
umask 077
export LC_ALL=C
segio_report_dir=$(mktemp -d /tmp/segio-pg-diagnostic.XXXXXX) || exit 1
segio_report_file="$segio_report_dir/report.txt"
segio_data_dirs=(/var/lib/pgsql/data)

segio_run() {
    printf '\n=== %s ===\n' "$1"
    shift
    "$@"
    segio_status=$?
    printf '[exit_status=%s]\n' "$segio_status"
    return 0
}

segio_add_data_dir() {
    for segio_existing_dir in "${segio_data_dirs[@]}"; do
        [ "$segio_existing_dir" = "$1" ] && return
    done
    segio_data_dirs+=("$1")
}

segio_describe_pid() {
    case "$1" in ''|*[!0-9]*) return ;; esac
    # Old procps versions can treat commas after '=' as header text.
    segio_run "PROCESS_METADATA pid=$1" ps -p "$1" -o pid -o ppid -o user -o comm
    # PostgreSQL process titles expose role, DB and connection in these fields.
    # Never output full command lines or the SQL/query part of a title.
    ps -ww -p "$1" -o args | awk '
        $1 ~ /^(postgres|postmaster):$/ && NF>=4 {print "Backend title fields:",$1,$2,$3,$4; found=1}
        END {if(!found) print "Backend title unrecognized or process exited; arguments omitted"}
    '
    printf 'executable: '
    readlink "/proc/$1/exe" || true
    segio_cwd=$(readlink "/proc/$1/cwd") || segio_cwd=''
    printf 'working_directory: %s\n' "$segio_cwd"
    if [ -n "$segio_cwd" ] && [ -f "$segio_cwd/PG_VERSION" ]; then
        segio_add_data_dir "$segio_cwd"
    fi
}

segio_report() {
    printf 'SEGIO_PG_DIAGNOSTIC_V1\nDatabase: cell_000\n'
    printf 'No passwords, application rows, full process arguments or environment values are collected.\n'
    printf 'Authentication files, database passwords and service state are not changed.\n'
    segio_run RUNNING_OS_ACCOUNT id -un
    segio_run PSQL_LOCATION bash -c 'command -v psql'
    if command -v psql >/dev/null 2>&1; then
        segio_run PSQL_VERSION psql --version
    fi
    # Include custom binary names rather than filtering postgres/postmaster.
    segio_run PROCESS_NAMES_ONLY ps -e -o pid -o ppid -o user -o comm
    printf '\n=== CELL_000_ACTIVE_CONNECTIONS ===\n'
    # Fixed metadata fields only: never print a backend title's SQL tail.
    ps -ww -e -o args | awk '
        $1 ~ /^(postgres|postmaster):$/ && $3=="cell_000" && NF>=4 {
            print "db_user=" $2, "database=" $3, "connection=" $4; found=1
        }
        END {if(!found) print "No active cell_000 backend titles found"}
    '

    if [ "$(id -u)" = 0 ]; then
        segio_run POSTGRES_ACCOUNT_METADATA su - postgres -c '
            for segio_binding in PGHOST PGHOSTADDR PGPORT PGSERVICE PGSERVICEFILE PGPASSWORD PGPASSFILE; do
                if declare -p "$segio_binding" >/dev/null 2>&1; then
                    printf "%s=present (value omitted)\n" "$segio_binding"
                else
                    printf "%s=absent\n" "$segio_binding"
                fi
            done
            if [ -s "$HOME/.pgpass" ]; then
                printf ".pgpass=present (contents omitted)\n"
            else
                printf ".pgpass=absent_or_empty\n"
            fi
            command -v psql
            psql --version
        '
        segio_run LOCAL_SOCKET_AUTH_TEST su - postgres -c '
            unset PGHOST PGHOSTADDR PGSERVICE PGSERVICEFILE
            PGCONNECT_TIMEOUT=5 psql -X -w -h /tmp -p 5432 -U postgres -d cell_000 \
                -c "SELECT current_user, pg_catalog.current_database();"
        '
    else
        printf '\nNot root: postgres account checks skipped. Run as root on the server.\n'
    fi

    printf '\n=== SOCKET_LISTENERS ===\n'
    if command -v netstat >/dev/null 2>&1; then
        netstat -xlpn 2>&1 | awk '/\.s\.PGSQL/ || /denied|not found|not supported/ {print; found=1} END {if(!found) print "No PostgreSQL listener lines found"}'
    elif command -v ss >/dev/null 2>&1; then
        ss -xlpn 2>&1 | awk '/\.s\.PGSQL/ || /denied|not found|not supported/ {print; found=1} END {if(!found) print "No PostgreSQL listener lines found"}'
    else
        printf 'Neither netstat nor ss is available.\n'
    fi

    printf '\n=== STANDARD_SOCKET_FILES ===\n'
    segio_found_socket=0
    for segio_socket in /tmp/.s.PGSQL.* /var/run/postgresql/.s.PGSQL.* /run/postgresql/.s.PGSQL.*; do
        [ -S "$segio_socket" ] || continue
        segio_found_socket=1
        segio_run SOCKET_FILE_METADATA ls -ld "$segio_socket"
        if command -v fuser >/dev/null 2>&1; then
            segio_run SOCKET_OWNER_PID fuser "$segio_socket"
            for segio_pid in $(fuser "$segio_socket" 2>/dev/null); do
                segio_describe_pid "$segio_pid"
            done
        else
            printf 'fuser is not available.\n'
        fi
    done
    [ "$segio_found_socket" = 1 ] || printf 'No standard PostgreSQL socket files found.\n'

    for segio_dir in /var/lib/pgsql/*/data; do
        [ -d "$segio_dir" ] && segio_add_data_dir "$segio_dir"
    done
    printf '\n=== CLUSTER_FILE_METADATA ===\n'
    for segio_dir in "${segio_data_dirs[@]}"; do
        printf '\nCandidate data directory: %s\n' "$segio_dir"
        if [ ! -d "$segio_dir" ]; then
            printf 'Directory missing or inaccessible\n'
            continue
        fi
        segio_run DATA_DIRECTORY_METADATA ls -ld "$segio_dir"
        if [ -f "$segio_dir/PG_VERSION" ]; then
            printf 'PG_VERSION bytes: '
            wc -c < "$segio_dir/PG_VERSION"
            segio_version=$(sed -n '1p' "$segio_dir/PG_VERSION")
            if [[ "$segio_version" =~ ^[0-9]+(\.[0-9]+)*$ ]]; then
                printf 'PG_VERSION=%s\n' "$segio_version"
            else
                printf 'Unrecognized or empty PG_VERSION (contents omitted)\n'
            fi
        else
            printf 'PG_VERSION missing or inaccessible\n'
        fi
        if [ -f "$segio_dir/postmaster.pid" ]; then
            segio_pid=$(sed -n '1p' "$segio_dir/postmaster.pid")
            segio_describe_pid "$segio_pid"
        fi
        if [ -f "$segio_dir/pg_hba.conf" ]; then
            printf 'Authentication methods only (options omitted):\n'
            awk '
                $1=="local" {print $1,$2,$3,$4}
                $1=="host" && ($4=="127.0.0.1/32" || $4=="::1/128") {print $1,$2,$3,$4,$5}
            ' "$segio_dir/pg_hba.conf"
        else
            printf 'pg_hba.conf missing or inaccessible\n'
        fi
    done
    printf '\nEND_SEGIO_PG_DIAGNOSTIC_V1\n'
}

segio_report > "$segio_report_file" 2>&1
printf '\nSaved diagnostic report: %s\n' "$segio_report_file"
printf 'Please send report.txt only. No passwords or application records are included.\n'
