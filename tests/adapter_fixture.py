"""Unit-only activation seam for existing native argv/output fixtures.

The source checks and native subprocesses remain real. Exact installed-tool
activation is covered separately by native Mise/SDK controls, so a fixture's
intentional compiler stub can continue proving the original adapter commands.
"""
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tomllib
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cookbook'))
import sdk_identity
import sdk_environment

sdk_identity.verify(ROOT, os.environ['COOKBOOK_TEST_REF'])
real_run = subprocess.run
real_environment = sdk_environment.command_environment
OPERATIONS = {
    'dispatch.py': {'build', 'validate', 'test', '_tests', 'test:lint', 'test:unit', 'test:integration', 'test:security'},
    'rust_cli.py': {'build', '_build', 'validate', 'test', '_test', 'test:lint', 'test:unit', 'test:integration', 'test:security'},
    'godot_game.py': {'build', '_build', 'validate', 'test', '_test', 'test:lint', 'test:unit', 'test:integration', 'test:security'},
}
PROBES = {'python', 'rust', 'actionlint', 'github:aviorstudio/gdlint', 'pipx:gdtoolkit'}


def allowed(entry, arguments):
    entry = Path(entry)
    if (entry.parent != ROOT / 'cookbook' or not arguments
            or arguments[0] not in OPERATIONS.get(entry.name, set())
            or (arguments[0] == '_test' and arguments[1:] not in [
                ['--suite', name] for name in ['lint', 'unit', 'integration', 'security']])
            or (arguments[0] != '_test' and len(arguments) != 1)):
        raise ValueError('unit adapter accepts only the existing finite compiler/engine controls')


def activation(command, root, environment):
    if command[:1] == ['cargo']:
        return dict(environment)
    return real_environment(command, root, environment)


def nested(command, *arguments, **options):
    if isinstance(command, (list, tuple)):
        if command[:2] == ['mise', 'where'] and len(command) == 3 and command[2] in PROBES:
            pins = tomllib.loads((Path(options.get('cwd', Path.cwd())) / 'mise.toml').read_text()).get('tools', {})
            if command[2] not in pins:
                raise ValueError('fixture SDK presence probe still requires its declared source pin')
            return subprocess.CompletedProcess(command, 0, stdout=str(ROOT / 'unit-only-presence-probe'), stderr='')
        for index, value in enumerate(command):
            if not isinstance(value, str):
                continue
            path = Path(value)
            if path.is_absolute() and path.parent == ROOT / 'cookbook' and path.suffix == '.py':
                allowed(path, list(command[index + 1:]))
                command = [sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                           str(path), *command[index + 1:]]
                break
    return real_run(command, *arguments, **options)


entry, *arguments = sys.argv[1:]
allowed(entry, arguments)
sys.argv = [entry, *arguments]
with patch.object(sdk_environment, 'command_environment', side_effect=activation), \
        patch.object(subprocess, 'run', side_effect=nested):
    runpy.run_path(entry, run_name='__main__')
