"""Developer integration check using a disposable PostgreSQL, never production.

Optional dependency: pgserver==0.1.4. Run with Python 3 on Linux.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile

import pgserver
import export_schema_from_segio_config as exporter


def main():
    binary = Path(pgserver.__file__).parent / "pginstall/bin/psql"
    password = "SYNTHETIC:CONFIG\\PASSWORD_NEVER_EXPORT"
    private_row = "SYNTHETIC_DOCUMENT_NEVER_EXPORT"
    with tempfile.TemporaryDirectory(prefix="segio-config-check-") as tmp:
        root = Path(tmp)
        with pgserver.get_server(root / "data", cleanup_mode="delete") as server:
            def run(sql, database=None):
                result = subprocess.run([str(binary), "-XAt", "-w", "-d", server.get_uri(database),
                                         "-v", "ON_ERROR_STOP=1"], input=sql,
                                        capture_output=True, text=True, timeout=30)
                if result.returncode:
                    raise RuntimeError("Disposable fixture query failed; details omitted")
                return result.stdout

            run("CREATE DATABASE cell_000; CREATE ROLE segio_fixture LOGIN SUPERUSER PASSWORD '%s';"
                % password)
            run("CREATE TABLE approval_document (id integer PRIMARY KEY, body text); "
                "INSERT INTO approval_document VALUES (1, '%s');" % private_row, "cell_000")
            hba = root / "data/pg_hba.conf"
            hba.write_text("local cell_000 segio_fixture password\n" + hba.read_text())
            # This changes only the disposable fixture, before running the helper.
            run("SELECT pg_reload_conf();")
            config = root / "db.conf"
            def write_config(secret):
                config.write_text("DBMS=postgresql\nHOST=%s\nDBNAME=cell_000\n"
                                  "USER=segio_fixture\nPW=%s\nOPTION=\n"
                                  % (server.get_postmaster_info().socket_dir, secret))
                config.chmod(0o600)

            write_config(password)
            originals = {p: p.read_bytes() for p in (hba, root / "data/pg_ident.conf",
                                                    root / "data/postgresql.conf", config)}
            roles_before = run("SELECT rolname, rolsuper, rolcanlogin FROM pg_roles ORDER BY rolname;")
            output_dir = None
            try:
                report = Path(exporter.export_schema(str(config), str(binary)))
                output_dir = report.parent
                text = report.read_text()
                assert "END_SEGIO_SCHEMA_REPORT_V1" in text
                assert "approval_document" in text
                assert password not in text and private_row not in text
                assert report.stat().st_mode & 0o777 == 0o600
                assert output_dir.stat().st_mode & 0o777 == 0o700
                assert not (output_dir / "pgpass").exists()
                assert all(p.read_bytes() == data for p, data in originals.items())
                assert run("SELECT count(*) FROM approval_document;", "cell_000").strip() == "1"
                assert run("SELECT rolname, rolsuper, rolcanlogin FROM pg_roles ORDER BY rolname;") == roles_before
                before = set(Path(tempfile.gettempdir()).glob("segio-schema-*"))
                write_config("SYNTHETIC_WRONG_PASSWORD_NEVER_EXPORT")
                try:
                    exporter.export_schema(str(config), str(binary))
                    raise AssertionError("Wrong password unexpectedly succeeded")
                except exporter.SafeError as error:
                    assert "PASSWORD_AUTHENTICATION_FAILED" in str(error)
                    assert "SYNTHETIC" not in str(error)
                assert set(Path(tempfile.gettempdir()).glob("segio-schema-*")) == before
                assert all(p.read_bytes() == data for p, data in originals.items() if p != config)
            finally:
                if output_dir:
                    shutil.rmtree(output_dir)
    print("PASS: existing-config password authentication, metadata only, private report, "
          "credential cleanup, unchanged DB/auth files, redacted wrong-password failure")


if __name__ == "__main__":
    main()
