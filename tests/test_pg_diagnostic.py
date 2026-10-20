import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(sys.platform == "linux", "CentOS diagnostic script requires Linux")
class LocalDiagnosticTests(unittest.TestCase):
    def test_report_captures_errors_without_secrets_or_query_arguments(self):
        script = Path(__file__).resolve().parents[1] / "tools/diagnose_postgres_local.sh"
        with tempfile.TemporaryDirectory(prefix="segio-diagnostic-test-") as tmp:
            root = Path(tmp)
            commands = {
                "id": '#!/bin/bash\nif [ "$1" = "-u" ]; then echo 0; else echo fixture_root; fi\n',
                "su": '#!/bin/bash\nexec /bin/bash -c "$4"\n',
                "psql": '#!/bin/bash\nif [ "$1" = "--version" ]; then echo "psql (PostgreSQL) 8.4.fixture"; exit; fi\nif [ -n "${PGHOSTADDR:-}" ]; then echo wrong-route; exit 9; fi\necho "psql: fe_sendauth: no password supplied" >&2\nexit 2\n',
                "ps": '''#!/bin/bash
# Reproduce the legacy procps header bug seen on the production server.
case "$*" in *"="*) echo ",ppid=,user=,comm="; echo 123; exit ;; esac
case "$*" in
  *"-o args"*)
    echo "COMMAND"
    echo "postgres: fixture_role cell_000 [local] PRIVATE_QUERY_MUST_NOT_APPEAR"
    echo "postmaster: fixture_tcp_role cell_000 127.0.0.1(1234) PRIVATE_QUERY_MUST_NOT_APPEAR"
    echo "unrelated: PRIVATE_QUERY_MUST_NOT_APPEAR cell_000 other"
    ;;
  *) echo "PID PPID USER COMMAND"; echo "123 1 fixture_user custom_pg_binary" ;;
esac
''',
                "fuser": '#!/bin/bash\necho "$SEGIO_DIAGNOSTIC_TEST_PID"\n',
                "netstat": '#!/bin/bash\necho "unix STREAM LISTENING 123/custom_pg /tmp/.s.PGSQL.fixture"\n',
            }
            for command, text in commands.items():
                p = root / command
                p.write_text(text, encoding="utf-8")
                p.chmod(0o700)
            fd, name = tempfile.mkstemp(prefix=".s.PGSQL.segio-test-", dir="/tmp")
            os.close(fd)
            os.unlink(name)
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(name)
            report_dir = None
            try:
                settings = dict(os.environ, PATH=str(root)+os.pathsep+os.environ["PATH"],
                                PGPASSWORD="PRIVATE_PASSWORD_MUST_NOT_APPEAR",
                                PGHOSTADDR="PRIVATE_ROUTE_MUST_NOT_APPEAR",
                                SEGIO_DIAGNOSTIC_TEST_PID=str(os.getpid()))
                result = subprocess.run(["/bin/bash", str(script)], cwd=root, env=settings,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = Path(result.stdout.split("Saved diagnostic report: ", 1)[1].splitlines()[0])
                report_dir = report.parent
                text = report.read_text(encoding="utf-8")
                self.assertIn("END_SEGIO_PG_DIAGNOSTIC_V1", text)
                self.assertIn("8.4.fixture", text)
                self.assertIn("PGPASSWORD=present (value omitted)", text)
                self.assertIn("fe_sendauth: no password supplied", text)
                self.assertIn("[exit_status=2]", text)
                self.assertIn("fixture_role cell_000 [local]", text)
                self.assertIn("123 1 fixture_user custom_pg_binary", text)
                self.assertIn("db_user=fixture_role database=cell_000 connection=[local]", text)
                self.assertIn("db_user=fixture_tcp_role database=cell_000 connection=127.0.0.1(1234)", text)
                self.assertNotIn(",ppid=,user=,comm=", text)
                for secret in ("PRIVATE_PASSWORD_MUST_NOT_APPEAR", "PRIVATE_ROUTE_MUST_NOT_APPEAR",
                               "PRIVATE_QUERY_MUST_NOT_APPEAR", "wrong-route"):
                    self.assertNotIn(secret, text)
                self.assertEqual(report.stat().st_mode & 0o777, 0o600)
                self.assertEqual(report.parent.stat().st_mode & 0o777, 0o700)
            finally:
                listener.close()
                os.unlink(name)
                if report_dir:
                    import shutil
                    shutil.rmtree(report_dir)
