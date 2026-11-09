"""Version 1 data-only SDK discovery; safe to mirror before loading an SDK."""
import os
from pathlib import Path
import re
import stat
import subprocess
import tomllib

MAX_SOURCE = 262144
SHA = re.compile(r'[a-f0-9]{40}\Z')
SDK_ENTRY = '''import pathlib, runpy, sys
entry = pathlib.Path(sys.argv[1])
sys.argv = sys.argv[1:]
sys.path.insert(0, str(entry.parent))
runpy.run_path(str(entry), run_name="__main__")
'''


def python_entry(executable, entry, *arguments):
    """Build isolated argv after the caller has verified this SDK source."""
    return [str(executable), '-I', '-B', '-c', SDK_ENTRY, str(entry), *arguments]


def identity(source):
    if isinstance(source, bytes):
        source = source.decode('utf-8')
    if not isinstance(source, str) or len(source.encode()) > MAX_SOURCE or '\0' in source:
        raise ValueError('SDK identity requires bounded UTF-8 Mise source')
    values = tomllib.loads(source.replace('\r\n', '\n').replace('\r', '\n')).get('vars', {})
    if (not isinstance(values, dict) or type(values.get('tools_contract')) is not int
            or values['tools_contract'] != 1 or not isinstance(values.get('tools_ref'), str)
            or not SHA.fullmatch(values['tools_ref'])):
        raise ValueError('SDK source requires one versioned immutable Mise identity')
    return {'version': 1, 'repository': 'nicodes/tools', 'revision': values['tools_ref']}


def no_links(path):
    path = Path(path).absolute()
    if any(value.is_symlink() for value in [path, *path.parents]):
        raise ValueError('SDK source must not traverse links')
    return path


def _signature(info, *, cross=False):
    result = (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
              getattr(info, 'st_uid', None), info.st_size, info.st_mtime_ns)
    return result if cross else (*result, info.st_ctime_ns)


def read_regular(path):
    path = no_links(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SOURCE:
        raise ValueError('SDK discovery requires bounded regular Mise source')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as source:
        opened = os.fstat(source.fileno())
        data = source.read(MAX_SOURCE + 1)
        closed = os.fstat(source.fileno())
    after = path.lstat()
    if (len(data) > MAX_SOURCE or _signature(before) != _signature(after)
            or _signature(opened) != _signature(closed)
            or _signature(before, cross=True) != _signature(opened, cross=True)):
        raise ValueError('SDK identity source changed while reading')
    return data


def read_source(root):
    return read_regular(Path(root) / 'mise.toml')


def declared(source):
    if isinstance(source, bytes):
        source = source.decode('utf-8')
    if not isinstance(source, str) or len(source.encode()) > MAX_SOURCE:
        raise ValueError('SDK identity requires bounded UTF-8 Mise source')
    values = tomllib.loads(source.replace('\r\n', '\n').replace('\r', '\n')).get('vars', {})
    if not isinstance(values, dict):
        raise ValueError('Mise variables must be source data')
    return bool(set(values) & {'tools_contract', 'tools_ref'})


def source_identity(root):
    return identity(read_source(root))


def location(root, revision, supplied=None):
    if not isinstance(revision, str) or not SHA.fullmatch(revision):
        raise ValueError('SDK source requires one immutable source pin')
    return no_links(supplied if supplied else Path(root) / '.artifacts/tools' / revision)


def git(directory, *arguments):
    return subprocess.check_output(['git', '--no-replace-objects', '-c', 'core.hooksPath=' + os.devnull,
        '-c', 'core.fsmonitor=false', '-C', str(directory), *arguments], text=True, timeout=30).strip()


def canonical_origin(value):
    if not isinstance(value, str):
        raise ValueError('SDK origin must name one canonical GitHub repository')
    match = re.fullmatch(r'(?:https://github[.]com/|git@github[.]com:|ssh://git@github[.]com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:[.]git)?', value)
    if match is None or any(part in {'.', '..'} for part in match[1].split('/')):
        raise ValueError('SDK origin must name one canonical GitHub repository')
    return match[1].lower()


def verify(directory, revision, *, repository='nicodes/tools'):
    if (not isinstance(repository, str)
            or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)
            or any(part in {'.', '..'} for part in repository.split('/'))):
        raise ValueError('SDK source requires its fixed provider repository identity')
    directory = no_links(directory)
    if (not directory.is_dir() or git(directory, 'rev-parse', 'HEAD') != revision
            or Path(git(directory, 'rev-parse', '--show-toplevel')).resolve() != directory.resolve()):
        raise ValueError('SDK source must be the exact clean pinned checkout')
    dirty = git(directory, 'status', '--porcelain', '--untracked-files=all', '--ignored=matching')
    if dirty:
        raise ValueError('SDK source must be the exact clean pinned checkout; changed paths: ' + repr(dirty[:512]))
    origin = git(directory, 'config', '--local', '--get-all', 'remote.origin.url')
    if canonical_origin(origin) != repository.lower():
        raise ValueError('SDK source origin differs from its fixed provider repository')
    return directory


def prepared(root, expected_ref=None, supplied=None):
    record = source_identity(root)
    revision = record['revision']
    if expected_ref is not None and expected_ref != revision:
        raise ValueError('SDK source differs from the exact caller-declared pin')
    return verify(location(root, revision, supplied), revision)
