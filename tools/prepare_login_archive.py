#!/usr/bin/env python
"""Root prepares a DB snapshot for one SEGIO-login identity. Python 2.6+.

Only published ZIPs are readable by the existing web process. It never gets
db.conf, a DB password, sudo, a shell endpoint or access to arbitrary target IDs.
"""
from __future__ import print_function

import hashlib
import json
import os
import pwd
import re
import shutil
import sys
import tempfile
import time
import uuid
from optparse import OptionParser

from export_approval_db import run_export
from export_schema_from_segio_config import SafeError, discover_config

HERE = os.path.dirname(os.path.abspath(__file__))
MARKER = 'segio-login-archive/1'


def private_directory(path, gid):
    if os.path.lexists(path):
        if os.path.islink(path) or not os.path.isdir(path) or os.stat(path).st_uid != 0:
            raise SafeError('Unsafe installation directory; nothing overwritten.')
    else:
        os.mkdir(path, 0o750)
    os.chown(path, 0, gid)
    os.chmod(path, 0o750)


def atomic_file(path, data, gid, mode=0o640):
    if os.path.islink(path):
        raise SafeError('Symlink destination refused.')
    fd, temporary = tempfile.mkstemp(prefix='.prepare-', dir=os.path.dirname(path))
    try:
        stream = os.fdopen(fd, 'wb')
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        finally:
            stream.close()
        os.chown(temporary, 0, gid)
        os.chmod(temporary, mode)
        os.rename(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare(username, web_user, config, private_root='/var/lib/segio-archive',
            binary='/usr/bin/psql'):
    if os.geteuid() != 0:
        raise SafeError('Run as root; do not grant sudo to the web process.')
    if not re.match(r'^[A-Za-z0-9_.@-]{1,100}$', username) or username.lower() == 'guest':
        raise SafeError('Specify the exact employee SEGIO ID.')
    try:
        account = pwd.getpwnam(web_user)
    except KeyError:
        raise SafeError('Web service OS user not found. Confirm --web-user.')
    if account.pw_uid == 0:
        raise SafeError('A root web service is not supported. Use an unprivileged web user.')
    gid = account.pw_gid
    config = os.path.realpath(config)
    cell = os.path.dirname(os.path.dirname(config))
    site = os.path.join(cell, 'folder', 'html', 'segio')
    if not os.path.isdir(site):
        raise SafeError('SEGIO web directory not found below the configured cell.')
    private_root = os.path.abspath(private_root)
    # Fixed PHP literal substitution: no quotes, backslashes, symlinks or webroot paths.
    if not re.match(r'^/[A-Za-z0-9_./-]+$', private_root):
        raise SafeError('Unsupported private directory path.')
    if os.path.realpath(private_root) != private_root or private_root.startswith(os.path.join(cell, 'folder') + '/'):
        raise SafeError('The publication directory must be outside the webroot and free of symlinks.')
    marker = os.path.join(private_root, 'installation.json')
    if os.path.exists(private_root):
        try:
            with open(marker, 'rb') as stream:
                installed = json.loads(stream.read().decode('utf-8'))
            if installed != {'format': MARKER, 'cell': cell, 'web_user': web_user}:
                raise ValueError()
        except (IOError, ValueError):
            raise SafeError('Existing private directory is not this installation; preserved.')
    private_directory(private_root, gid)
    private_directory(os.path.join(private_root, 'users'), gid)
    private_directory(os.path.join(private_root, 'snapshots'), gid)
    if not os.path.exists(marker):
        atomic_file(marker, json.dumps({'format': MARKER, 'cell': cell, 'web_user': web_user}).encode('utf-8'), gid)
    endpoint_dir = os.path.join(site, 'archive')
    endpoint = os.path.join(endpoint_dir, 'bridge.php')
    if os.path.lexists(endpoint_dir) and (os.path.islink(endpoint_dir) or not os.path.isfile(endpoint)):
        raise SafeError('Existing archive web directory is not this bridge; preserved.')
    if os.path.isfile(endpoint):
        with open(endpoint, 'rb') as stream:
            if b'/* PHP 5.3 compatible. Identity comes only from SEGIO' not in stream.read(300):
                raise SafeError('Existing web endpoint is not this bridge; preserved.')
    old_mask = os.umask(0o077)
    work = tempfile.mkdtemp(prefix='segio-publish-')
    generation = None
    published = False
    try:
        folder, archive = run_export(config, username, work, cell=cell, binary=binary, progress=print)
        with open(os.path.join(folder, 'manifest.json'), 'rb') as stream:
            manifest = json.loads(stream.read().decode('utf-8'))
        version = uuid.uuid4().hex
        generation = os.path.join(private_root, 'snapshots', version)
        os.mkdir(generation, 0o750)
        destination = os.path.join(generation, 'archive.zip')
        digest = hashlib.sha256()
        source = open(archive, 'rb')
        target = open(destination, 'wb')
        try:
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                target.write(block)
                digest.update(block)
        finally:
            source.close()
            target.close()
        os.chown(destination, 0, gid)
        os.chmod(destination, 0o640)
        os.chown(generation, 0, gid)
        os.chmod(generation, 0o750)
        record = {'username': username, 'version': version, 'sha256': digest.hexdigest(),
                  'bytes': os.path.getsize(destination), 'counts': manifest['counts'],
                  'prepared_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                  'scope': 'administrator_prepared_personal_relations', 'account_deletion_verified': False}
        if not os.path.isdir(endpoint_dir):
            os.mkdir(endpoint_dir, 0o755)
            os.chmod(endpoint_dir, 0o755)
        with open(os.path.join(HERE, 'login_archive_bridge.php'), 'rb') as stream:
            php = stream.read().replace(b'__PRIVATE_ROOT__', private_root.encode('ascii'))
        atomic_file(endpoint, php, gid, 0o644)
        name = hashlib.sha256(username.encode('utf-8')).hexdigest() + '.json'
        atomic_file(os.path.join(private_root, 'users', name), json.dumps(record).encode('utf-8'), gid)
        published = True
        print('Published employee archive. Windows may log in as: ' + username)
        print('Endpoint: /segio/archive/bridge.php')
        print('The snapshot is fixed. Run this command again to refresh it.')
        print('Previous snapshots are retained privately; no source records were deleted.')
        return record
    finally:
        shutil.rmtree(work)
        if generation and not published:
            shutil.rmtree(generation)
        os.umask(old_mask)


def main():
    parser = OptionParser(usage='%prog --user SEGIO_ID --web-user WEB_OS_USER [--config /cell/etc/db.conf]')
    parser.add_option('--user')
    parser.add_option('--web-user')
    parser.add_option('--config')
    parser.add_option('--private-root', default='/var/lib/segio-archive')
    options, args = parser.parse_args()
    if args or not options.user or not options.web_user:
        parser.error('--user and --web-user are required; passwords are not accepted.')
    try:
        prepare(options.user, options.web_user, options.config or discover_config(), options.private_root)
        return 0
    except SafeError as exc:
        print(str(exc), file=sys.stderr)
    except Exception as exc:
        print('Preparation failed (%s). Raw diagnostics omitted.' % type(exc).__name__, file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
