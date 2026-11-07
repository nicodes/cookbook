"""Verify the effective Mise task definitions before native SDK execution."""
import json
import os
from pathlib import Path
import subprocess
import tomllib

import sdk_identity
import mise_entrypoint

MAX_OUTPUT = 2 << 20


def validate(root, declarations, records):
    """Compare data from pinned Mise, without running any task or product code."""
    root = Path(root).resolve()
    source = root / 'mise.toml'
    if (not isinstance(records, list) or len(records) != len(declarations)
            or any(not isinstance(record, dict) for record in records)):
        raise ValueError('effective local tasks differ from the canonical recipe inventory')
    by_name = {record.get('name'): record for record in records}
    if len(by_name) != len(records) or set(by_name) != set(declarations):
        raise ValueError('effective local tasks omit or repeat canonical recipe operations')
    for name, declared in declarations.items():
        record = dict(by_name[name])
        try:
            paths = [Path(record.pop('source')).resolve(),
                     *[Path(value).resolve() for value in record.pop('config_sources')]]
            directory = Path(record.pop('dir')).resolve()
        except (KeyError, TypeError, ValueError, OSError) as error:
            raise ValueError('effective task sources must be literal canonical root paths') from error
        if paths != [source, source] or directory != root:
            raise ValueError('effective task authority differs from the canonical root Mise source')
        run = declared['run']
        expected = {'name': name, 'aliases': [], 'description': declared.get('description', ''),
            'depends': [], 'daemons': None, 'depends_post': [], 'wait_for': [], 'env': [],
            'hide': declared.get('hide', False), 'global': False, 'raw': False,
            'interactive': False, 'sources': [], 'outputs': [], 'shell': declared.get('shell'),
            'quiet': False, 'silent': False, 'tools': {}, 'usage': '', 'timeout': None,
            'run': run if isinstance(run, list) else [run], 'args': [], 'file': None}
        if record != expected:
            raise ValueError('effective task behavior differs from its canonical recipe source: ' + name)
    return records


def check(root, environment=None):
    root = Path(root).resolve()
    source = sdk_identity.read_source(root)
    mise_entrypoint.decode(source)
    declarations = tomllib.loads(source.decode().replace('\r\n', '\n').replace('\r', '\n'))['tasks']
    environment = {**(os.environ if environment is None else environment),
        'MISE_AUTO_INSTALL': 'false', 'MISE_EXEC_AUTO_INSTALL': 'false',
        'MISE_TASK_RUN_AUTO_INSTALL': 'false', 'MISE_OFFLINE': 'true'}
    result = subprocess.run(['mise', 'tasks', 'ls', '--json', '--hidden', '--local'], cwd=root,
        env=environment, encoding='utf-8', capture_output=True, timeout=30)
    if result.returncode or len(result.stdout.encode()) > MAX_OUTPUT:
        raise ValueError('effective canonical task authority could not be verified without execution')
    return validate(root, declarations, json.loads(result.stdout))
