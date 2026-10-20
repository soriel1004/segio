"""Developer check against a disposable local PostgreSQL, never an existing server.

Optional development dependency: pgserver==0.1.4 (Linux).
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


def main():
    import pgserver

    binary = Path(pgserver.__file__).parent / "pginstall/bin/psql"
    sql = (Path(__file__).parent / "inspect_approval_schema.sql").read_text(encoding="utf-8")
    secret_row = "SYNTHETIC_PRIVATE_DOCUMENT_NEVER_EXPORT"
    secret_default = "SYNTHETIC_DEFAULT_NEVER_EXPORT"
    fixture = f"""
    CREATE TABLE approval_member_info (uid text PRIMARY KEY, password text);
    CREATE TABLE approval_document (
        no integer PRIMARY KEY, uid text REFERENCES approval_member_info(uid),
        title text, context text, internal_value text DEFAULT '{secret_default}');
    CREATE TABLE approval_attachment (no integer PRIMARY KEY,
        doc_no integer REFERENCES approval_document(no), fpath text);
    CREATE VIEW approval_doc_view AS SELECT no, uid, title FROM approval_document;
    INSERT INTO approval_member_info VALUES ('fixture-user', '{secret_row}');
    INSERT INTO approval_document(no, uid, context) VALUES (1, 'fixture-user', '{secret_row}');
    """
    with tempfile.TemporaryDirectory(prefix="segio-schema-check-") as tmp:
        with pgserver.get_server(Path(tmp) / "data", cleanup_mode="delete") as server:
            def run(statement):
                return subprocess.run([str(binary), "-X", "-d", server.get_uri(),
                                       "-v", "ON_ERROR_STOP=1"], input=statement,
                                      capture_output=True, text=True, timeout=30)
            setup = run(fixture)
            if setup.returncode:
                raise RuntimeError("Could not create synthetic test tables")
            report = run(sql)
            if report.returncode:
                raise RuntimeError("Schema SQL failed: " + report.stderr)
            for marker in ["END_SEGIO_SCHEMA_REPORT_V1", "approval_document",
                           "foreign_key", "approval_attachment", "approval_doc_view"]:
                assert marker in report.stdout, marker
            assert secret_row not in report.stdout
            assert secret_default not in report.stdout
            assert "fixture-user" not in report.stdout
            # A write inserted into the report's transaction must actually fail.
            injected = sql.replace("SET LOCAL statement_timeout = 15000;",
                                   "INSERT INTO approval_document(no) VALUES(2);")
            assert run(injected).returncode != 0
            # A machine-readable count confirms that neither path changed the rows.
            count = subprocess.run([str(binary), "-XAt", "-d", server.get_uri(),
                                    "-c", "SELECT count(*) FROM approval_document;"],
                                   capture_output=True, text=True, timeout=30)
            assert count.returncode == 0 and count.stdout.strip() == "1"
            print("PASS: metadata output, secret exclusion, read-only enforcement, unchanged data")
            version = run("SHOW server_version;")
            print(version.stdout.strip())


if __name__ == "__main__":
    main()
