"""Explicit setup of hash-locked, checkout-local Python dependencies."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from configuration import load, relative


def inputs(root, value):
    if not isinstance(value, dict) or set(value) not in ({'requirements', 'venv'}, {'requirements', 'target'}):
        raise ValueError('Python dependencies require requirements and one local environment')
    requirements = relative(root, value['requirements'], exists=True)
    directory = relative(root, value.get('venv', value.get('target')))
    if (not requirements.is_file() or requirements.stat().st_size > 262144
            or directory.relative_to(root).parts[:1] != ('.artifacts',)
            or len(directory.relative_to(root).parts) < 2):
        raise ValueError('Python dependencies require bounded source and a dedicated artifact environment')
    python = directory / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    identity = {'requirements': value['requirements'],
                'sha256': hashlib.sha256(requirements.read_bytes()).hexdigest(),
                'python': list(sys.version_info[:3])}
    return requirements, directory, python, identity


def setup(root, config, environment, run=subprocess.run):
    for value in config.get('components', {}).get('python', []):
        requirements, directory, python, identity = inputs(root, value)
        receipt = directory / '.cookbook-python.json'
        receipt.unlink(missing_ok=True)
        directory.parent.mkdir(parents=True, exist_ok=True)
        if 'venv' in value:
            run([sys.executable, '-m', 'venv', '--copies', str(directory)],
                cwd=root, env=environment, check=True, timeout=180)
            relative(root, python.relative_to(root).as_posix(), exists=True)
            command = [str(python), '-m', 'pip', 'install']
        else:
            command = [sys.executable, '-m', 'pip', 'install', '--target', str(directory), '--upgrade']
        run([*command, '--require-hashes', '--only-binary', ':all:', '-r', str(requirements)],
            cwd=root, env=environment, check=True, timeout=180)
        receipt.write_text(json.dumps(identity, sort_keys=True) + '\n')


def interpreter(root, config, name):
    entries = [value for value in config.get('components', {}).get('python', []) if value.get('venv') == name]
    if len(entries) != 1:
        raise ValueError('native tests must select one declared Python environment')
    _requirements, directory, python, identity = inputs(root, entries[0])
    receipt = relative(root, (directory / '.cookbook-python.json').relative_to(root).as_posix(), exists=True)
    if json.loads(receipt.read_text()) != identity:
        raise ValueError('Python dependencies are stale; run mise run setup')
    return str(relative(root, python.relative_to(root).as_posix(), exists=True))


if __name__ == '__main__':
    if sys.argv[1:] != ['setup']:
        raise SystemExit('usage: python_component.py setup')
    try:
        root = Path.cwd().resolve()
        setup(root, load(root), dict(os.environ))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from None
