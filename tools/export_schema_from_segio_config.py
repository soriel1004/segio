#!/usr/bin/env python
"""Read SEGIO's db.conf and export schema metadata without changing DB auth.

Uses Python 2.6-compatible syntax and the server's existing psql client.
Copy this file and inspect_approval_schema.sql together. Never source db.conf.
"""
from __future__ import print_function

import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
from optparse import OptionParser


class SafeError(Exception):
    def __init__(self, message, code=None):
        Exception.__init__(self, message)
        self.code = code


def read_config(path):
    result = {}
    with open(path, "rb") as stream:
        data = stream.read(65537)
    if len(data) > 65536 or b"\x00" in data:
        raise SafeError("Unsupported db.conf format; file contents omitted.")
    for line in data.splitlines():
        line = line.strip()
        if not line or line.startswith((b"#", b";")):
            continue
        key, separator, value = line.partition(b"=")
        key = key.strip().upper()
        if not separator or key not in (b"DBMS", b"HOST", b"DBNAME", b"USER", b"PW", b"OPTION"):
            raise SafeError("Unsupported db.conf field; contents omitted.")
        if key in result:
            raise SafeError("Duplicate db.conf field; contents omitted.")
        value = value.strip()
        if value[:1] in (b"'", b'"'):
            if len(value) < 2 or value[-1:] != value[:1]:
                raise SafeError("Unsupported quoting in db.conf; contents omitted.")
            value = value[1:-1]
        if b"\r" in value or b"\n" in value:
            raise SafeError("Unsupported multiline setting; contents omitted.")
        result[key] = value
    return result


def discover_config():
    candidates = set()
    for pattern in ("/home/*/*/etc/db.conf", "/home/*/etc/db.conf",
                    "/etc/shttpd/*/etc/db.conf", "/etc/shttpd/etc/db.conf",
                    "/opt/*/*/etc/db.conf", "/usr/local/*/*/etc/db.conf"):
        candidates.update(glob.glob(pattern))
    matches = []
    for path in sorted(candidates):
        try:
            settings = read_config(path)
        except (IOError, OSError, SafeError):
            continue
        if (settings.get(b"DBNAME") == b"cell_000" and
                settings.get(b"USER") and settings.get(b"PW")):
            matches.append(path)
    if len(matches) != 1:
        # Only filenames are displayed, never any setting values.
        for path in sorted(candidates):
            print("Candidate configuration file: " + path)
        raise SafeError("Expected one populated cell_000 db.conf; found %d. "
                        "Use --config with the confirmed file path. "
                        "Do not send db.conf or passwords." % len(matches))
    return matches[0]


def connection_settings(settings):
    database = settings.get(b"DBNAME") or b"cell_000"
    user = settings.get(b"USER", b"")
    password = settings.get(b"PW", b"")
    host = settings.get(b"HOST") or b"/tmp"
    if database != b"cell_000":
        raise SafeError("Configured database differs from cell_000; no connection attempted.")
    if not user or not password:
        raise SafeError("USER or PW is empty in db.conf; no password guessed. "
                        "Existing database credentials are still required.")
    if not re.match(b"^[A-Za-z0-9_.$-]+$", user):
        raise SafeError("Unsupported DB role format; value omitted.")
    if not os.path.isabs(host) and host not in (b"localhost", b"127.0.0.1", b"::1"):
        raise SafeError("Configured host is not a recognized local endpoint; no connection attempted.")
    if settings.get(b"OPTION"):
        raise SafeError("OPTION is populated; its meaning must be verified before connecting. "
                        "Value omitted; do not send credentials.")
    dbms = settings.get(b"DBMS", b"").lower()
    if dbms not in (b"", b"postgres", b"postgresql", b"pgsql"):
        raise SafeError("Unrecognized DBMS setting; no connection attempted.")
    try:
        return host.decode("ascii"), user.decode("ascii"), database.decode("ascii"), password
    except UnicodeDecodeError:
        raise SafeError("Unsupported local endpoint encoding; value omitted.")


def pgpass_escape(value):
    if not isinstance(value, bytes):
        value = value.encode("ascii")
    return value.replace(b"\\", b"\\\\").replace(b":", b"\\:")


def export_schema(config_path, binary="/usr/bin/psql"):
    host, user, database, password = connection_settings(read_config(config_path))
    sql_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inspect_approval_schema.sql")
    with open(sql_path, "rb") as stream:
        sql = stream.read()
    if b"END_SEGIO_SCHEMA_REPORT_V1" not in sql:
        raise SafeError("Schema SQL is incomplete; no connection attempted.")
    previous_umask = os.umask(0o077)
    folder = tempfile.mkdtemp(prefix="segio-schema-")
    passfile = os.path.join(folder, "pgpass")
    success = False
    try:
        # libpq versions/builds can match the explicit socket path or localhost
        # for Unix sockets. Cover both, for this database and role only.
        passhosts = [host]
        if host.startswith("/"):
            passhosts.append("localhost")
        with open(passfile, "wb") as stream:
            for passhost in passhosts:
                fields = (passhost, "5432", database, user, password)
                stream.write(b":".join([pgpass_escape(v) for v in fields]) + b"\n")
        environment = dict(os.environ)
        # Preserve unrelated process settings; discard inherited libpq routing,
        # credentials and options before applying the selected local config.
        for key in list(environment):
            if key.startswith("PG"):
                del environment[key]
        environment.update({"PGPASSFILE": passfile, "PGCONNECT_TIMEOUT": "5",
                            "PGOPTIONS": "-c default_transaction_read_only=on -c statement_timeout=15000",
                            "LC_ALL": "C"})
        command = [binary, "-X", "-w", "-h", host, "-p", "5432", "-U", user,
                   "-d", database, "-v", "ON_ERROR_STOP=1"]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=environment)
        output, error = process.communicate(sql)
        if process.returncode:
            reason = "PSQL_ERROR"
            for token, label in ((b"password authentication failed", "PASSWORD_AUTHENTICATION_FAILED"),
                                 (b"no password supplied", "NO_PASSWORD_SUPPLIED"),
                                 (b"could not connect", "CONNECTION_FAILED"),
                                 (b"permission denied", "PERMISSION_DENIED")):
                if token in error:
                    reason = label
                    break
            # Never echo raw client errors, which can contain config secrets.
            raise SafeError("Schema export failed: %s (psql exit %d). "
                            "No authentication settings changed." % (reason, process.returncode))
        if b"END_SEGIO_SCHEMA_REPORT_V1" not in output:
            raise SafeError("Schema report is incomplete; no report saved.")
        report = os.path.join(folder, "segio-db-structure.txt")
        with open(report, "wb") as stream:
            stream.write(output)
        success = True
        return report
    finally:
        if os.path.exists(passfile):
            os.unlink(passfile)
        if not success:
            shutil.rmtree(folder)
        os.umask(previous_umask)


def main():
    parser = OptionParser(usage="%prog [--config /path/to/cell/etc/db.conf]")
    parser.add_option("--config", dest="config_path", help="SEGIO db.conf path; never a password")
    options, args = parser.parse_args()
    if args:
        parser.error("Only --config is accepted. Do not pass credentials.")
    try:
        path = options.config_path or discover_config()
        print("Using configuration file: " + path)
        report = export_schema(path)
        print("Saved schema report: " + report)
        print("Send segio-db-structure.txt only. Do not send db.conf or passwords.")
        return 0
    except SafeError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (IOError, OSError):
        print("Could not read the input files or execute psql. "
              "Keep both tools files together and verify the --config path.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
