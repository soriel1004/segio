#!/usr/bin/env python
"""Read-only live-export checks, executed as the web OS user. Python 2.6+."""
from __future__ import print_function
import json
import os
import shutil
import sys
import tempfile
from optparse import OptionParser

from export_approval_db import psql_readonly, quote_identifier, make_queries
from export_schema_from_segio_config import SafeError
from live_export_worker import read_json, failure_details, FAILURE_CODES, inventory, mail_path

HERE = os.path.dirname(os.path.abspath(__file__))


def diagnose(root, username=None):
    """Never print config values, stderr, document rows, mail headers or IDs."""
    checks = {}
    failed = False
    folder = None
    stage = 'initializing'
    try:
        config = read_json(os.path.join(root, 'config.json'))
        if os.path.abspath(config['root']) != os.path.abspath(root):
            raise SafeError('unsafe_job')
        checks['private_config'] = 'ok'
        folder = tempfile.mkdtemp(prefix='.diagnose-', dir=os.path.join(root, 'jobs'))
        checks['job_write'] = 'ok'
        stage = 'database'
        with open(os.path.join(HERE, 'approval_db_columns.json'), 'rb') as stream:
            model = json.loads(stream.read().decode('utf-8'))
        sql = ['\\set ON_ERROR_STOP on', '\\set VERBOSITY verbose',
               'BEGIN ISOLATION LEVEL SERIALIZABLE READ ONLY;',
               'SET LOCAL statement_timeout=15000;']
        for table in sorted(model):
            sql.append('SELECT %s FROM public.%s WHERE false;' %
                       (','.join(quote_identifier(c) for c in model[table]), quote_identifier(table)))
        # EXPLAIN validates joins/operators used by the actual export without
        # executing employee queries or returning any application records.
        queries = make_queries('__segio_diagnostic__', model, live_scope=True)
        for table in sorted(queries):
            sql.append('EXPLAIN '+queries[table]+';')
        sql.extend(['ROLLBACK;', '\\echo SEGIO_DB_EXPORT_COMPLETE'])
        psql_readonly(config['db_config'], folder, sql, config.get('psql', '/usr/bin/psql'))
        checks['database_connection_and_schema'] = 'ok'
        if username:
            stage = 'mail'
            entries = inventory(mail_path(config.get('mail_template'), username))
            for name, path, identity in entries:
                with open(path, 'rb') as stream:
                    stream.read(1)
            checks['mail_read'] = 'ok'
            checks['mail_files'] = len(entries)
        else:
            checks['mail_read'] = 'not_checked_no_user'
    except Exception as exc:
        code, diagnostic = failure_details(exc, stage)
        checks['error'] = code
        checks['diagnostic'] = diagnostic
        failed = True
    finally:
        if folder:
            shutil.rmtree(folder)
    # Only the allowlisted anonymous error record survives normal job cleanup.
    path = os.path.join(root, 'jobs', 'last-failure.json')
    try:
        value = read_json(path)
        code = value.get('phase')
        details = value.get('diagnostic', {})
        if code in FAILURE_CODES and isinstance(details, dict):
            checks['last_failure'] = {'code': code,
                'stage': details.get('stage') if details.get('stage') in
                    ('initializing', 'database', 'mail', 'packing', 'hashing') else 'unknown'}
    except (IOError, OSError, ValueError, SafeError):
        pass
    print(json.dumps(checks, sort_keys=True))
    return 1 if failed else 0


def main():
    parser = OptionParser()
    parser.add_option('--private-root', default='/home/segio/cell000/etc/segio-live-archive')
    parser.add_option('--web-user', help='Root only: switch to the actual non-root web account')
    parser.add_option('--user', help='Optional employee ID to check personal mail read access')
    options, args = parser.parse_args()
    if args:
        parser.error('Unexpected arguments; never pass passwords.')
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        if not options.web_user:
            parser.error('Run with --web-user segio; checks must not use root privileges.')
        import pwd
        account = pwd.getpwnam(options.web_user)
        if account.pw_uid == 0:
            parser.error('A non-root web account is required.')
        executable = sys.executable
        installed = os.path.join(os.path.abspath(options.private_root), 'code', 'diagnose_live_archive.py')
        command = [executable, installed, '--private-root', os.path.abspath(options.private_root)]
        if options.user:
            command.extend(['--user', options.user])
        os.initgroups(options.web_user, account.pw_gid)
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
        os.chdir('/')
        os.execv(executable, command)
    old_umask = os.umask(0o077)
    try:
        return diagnose(options.private_root, options.user)
    finally:
        os.umask(old_umask)


if __name__ == '__main__':
    sys.exit(main())
