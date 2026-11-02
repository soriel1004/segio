"""Generate Windows EXE resources and VERSION.txt from the application's version."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from segio_archive import APPLICATION_NAME, __version__


def write_version(resource: Path, text: Path):
    if not re.fullmatch(r'\d+\.\d+\.\d+', __version__):
        raise ValueError('Windows version must contain three numeric components.')
    numbers = tuple(map(int, __version__.split('.'))) + (0,)
    if any(number > 65535 for number in numbers):
        raise ValueError('Windows version components must fit in 16 bits.')
    strings = {
        'FileDescription': APPLICATION_NAME,
        'FileVersion': __version__,
        'InternalName': 'SEGIOArchive',
        'OriginalFilename': 'SEGIOArchive.exe',
        'ProductName': APPLICATION_NAME,
        'ProductVersion': __version__,
    }
    entries = ',\n'.join('            StringStruct(%r, %r)' % item for item in strings.items())
    payload = '''# UTF-8 Windows VERSIONINFO, generated; do not edit the version here.
VSVersionInfo(
  ffi=FixedFileInfo(filevers=%r, prodvers=%r, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('041204B0', [
%s
        ])]), VarFileInfo([VarStruct('Translation', [1042, 1200])])])
''' % (numbers, numbers, entries)
    resource.parent.mkdir(parents=True, exist_ok=True)
    resource.write_text(payload, encoding='utf-8')
    commit = os.environ.get('GITHUB_SHA', 'local')
    if commit != 'local' and not re.fullmatch(r'[0-9a-f]{7,40}', commit):
        commit = 'local'
    built = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    text.parent.mkdir(parents=True, exist_ok=True)
    text.write_text(f'{APPLICATION_NAME}\nVersion: {__version__}\nBuild commit: {commit}\nBuild UTC: {built}\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--resource', type=Path, required=True)
    parser.add_argument('--text', type=Path, required=True)
    args = parser.parse_args()
    write_version(args.resource, args.text)
