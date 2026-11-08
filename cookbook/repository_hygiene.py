"""Reject retired active recipe artifacts using source paths, never their values."""
from pathlib import Path, PurePosixPath
import subprocess


ARTIFACTS = ('third-party-actions', 'third_party_actions', 'fleet-policy',
             'fleet_policy', 'engineering-plan', 'engineering_plan')
PATH_FIELDS = {'path', 'directory', 'source', 'destination', 'output', 'outputs',
               'files', 'source_files', 'root_files', 'inputs', 'artifacts',
               'configuration', 'metadata', 'record', 'config', 'definition',
               'policy', 'include', 'includes', 'env_file', 'env_files',
               'environment_file', 'environment_files'}
NEGATIVE_FIELDS = {'exclude', 'excludes', 'exclude_filter', 'exclude_patterns',
                   'ignore', 'ignored', 'ignore_patterns', 'ignored_paths'}


def retired(path):
    if not isinstance(path, str) or '://' in path or any(c in path for c in '\0\r\n'):
        return False
    parts = PurePosixPath(path.replace('\\', '/')).parts
    parents = tuple(part.casefold() for part in parts[:-1])
    if any(part in {'vendor', 'third_party', 'third-party', 'node_modules', 'history', 'testdata'}
           for part in parents):
        return False
    if any(part in {'test', 'tests'} and any(value in {'fixtures', 'data', 'test_data'}
           for value in parents[index + 1:]) for index, part in enumerate(parents)):
        return False
    name = parts[-1].casefold() if parts else ''
    return name.startswith('.env') or any(name == item or name.startswith(item + '.') for item in ARTIFACTS)


def check_configuration(value, *, path_field=False, location='recipe'):
    if isinstance(value, dict):
        for name, child in value.items():
            key = name.casefold()
            if key in NEGATIVE_FIELDS or key.startswith('forbidden_'):
                continue
            selected = path_field or key in PATH_FIELDS
            if selected and retired(name):
                raise ValueError('retired active source artifact configured in ' + location)
            check_configuration(child, path_field=selected, location=location + '.' + name)
    elif isinstance(value, list):
        for child in value:
            check_configuration(child, path_field=path_field, location=location)
    elif path_field and retired(value):
        raise ValueError('retired active source artifact configured in ' + location)


def check_tracked(root):
    root = Path(root).resolve()
    if not (root / '.git').exists():
        return  # Pure generation fixtures have no owning Git index.
    actual = subprocess.check_output(['git', '-C', str(root), 'rev-parse', '--show-toplevel'],
                                     text=True, timeout=30).strip()
    if Path(actual).resolve() != root:
        raise ValueError('repository hygiene requires the owning source root')
    names = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z'], timeout=30)
    if len(names) > 8 << 20:
        raise ValueError('tracked source path inventory exceeds its bound')
    rejected = sorted({name.decode('utf-8') for name in names.split(b'\0') if name and
                       retired(name.decode('utf-8')) and
                       ((root / name.decode('utf-8')).exists() or (root / name.decode('utf-8')).is_symlink())})
    if rejected:
        raise ValueError('retired active source artifacts: ' + ', '.join(rejected[:10]))
