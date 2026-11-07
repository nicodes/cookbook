"""Prepare declared mobile and disposable Web exports as verified build outputs."""
import sdk_environment
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys

from configuration import relative
from recipe_contract import app_directory

import dependencies

COOKBOOK = Path(__file__).resolve().parents[1]
TOOLS = dependencies.tools_root()
PROTECTED = {'APP_VERSION', 'CI', 'GH_TOKEN', 'GITHUB_TOKEN', 'KOMIZO_ACTIONS_ROOT',
             'COMMIT', 'VERSION', 'DATE', 'SOURCE_COMMIT', 'SOURCE_REV', 'SOURCE_ROOT',
             'PATH', 'HOME', 'SHELL', 'ENV', 'BASH_ENV', 'PYTHONPATH', 'PYTHONHOME',
             'LD_PRELOAD', 'LD_LIBRARY_PATH', 'DYLD_INSERT_LIBRARIES', 'NODE_OPTIONS',
             'GOTOOLCHAIN', 'RUSTUP_AUTO_INSTALL'}


def records(root, config):
    app = config.get('components', {}).get('app', {})
    fixtures = app.get('fixture_exports', [])
    native = app.get('native_exports', [])
    if not isinstance(fixtures, list) or not isinstance(native, list):
        raise ValueError('Expo variants require native export records')
    if not fixtures and not native:
        return []
    directory = relative(root, app_directory(config), exists=True)
    result, outputs = [], set()
    for entry in fixtures:
        if (not isinstance(entry, dict) or set(entry) - {'output', 'environment', 'timeout'}
                or not isinstance(entry.get('output'), str)
                or not re.fullmatch(r'dist-[a-z0-9][a-z0-9-]{0,63}', entry['output'])):
            raise ValueError('Web test variants require an explicit disposable Expo output')
        values = entry.get('environment', {})
        if (not isinstance(values, dict) or len(values) > 64 or any(not isinstance(name, str)
                or not re.fullmatch('[A-Z][A-Z0-9_]{0,99}', name) or name in PROTECTED
                or name.startswith(('GITHUB_', 'MISE_', 'COOKBOOK_', 'REQUEST_', 'TOOLS_', 'FLEET_'))
                or not isinstance(value, str) or len(value) > 4096 or any(ord(character)<32 for character in value)
                for name, value in values.items())):
            raise ValueError('Web test build inputs must be literal native compiler variables')
        output = relative(directory, entry['output'])
        result.append(('web', directory, output, entry))
    for entry in native:
        if not isinstance(entry, dict) or set(entry) - {'platforms', 'output', 'timeout'}:
            raise ValueError('native Expo exports require platform and output data')
        platforms = entry.get('platforms')
        if (not isinstance(platforms, list) or not platforms or any(not isinstance(name, str) for name in platforms)
                or len(set(platforms)) != len(platforms) or not set(platforms) <= {'ios', 'android'}):
            raise ValueError('native Expo exports must select iOS or Android explicitly')
        output = relative(root, entry.get('output'))
        if (not output.is_relative_to(root / '.artifacts') or output == root / '.artifacts'
                or output.relative_to(root / '.artifacts').parts[0] in {'cookbook', 'verification', 'release'}):
            raise ValueError('native Expo outputs belong in the checkout generated artifact root')
        result.append(('native', directory, output, entry))
    for _, _, output, entry in result:
        timeout = entry.get('timeout', 900)
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise ValueError('Expo compiler invocations require a bounded deadline')
        if any(output == other or output.is_relative_to(other) or other.is_relative_to(output) for other in outputs):
            raise ValueError('Expo variant outputs must be distinct')
        outputs.add(output)
    return result


def artifact_paths(root, config):
    return [output.relative_to(root).as_posix() for _, _, output, _ in records(root, config)]


def validate(output):
    files = sorted(path for path in output.rglob('*') if path.is_file())
    if not files or any(path.is_symlink() for path in output.rglob('*')):
        raise ValueError('Expo variant must contain nonempty regular build outputs')
    if not any(path.suffix in {'.js', '.hbc', '.jsbundle'} and path.stat().st_size for path in files):
        raise ValueError('Expo variant produced no JavaScript or native bytecode bundle')
    manifest = {path.relative_to(output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    (output / 'variant-manifest.json').write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')


def compile_native(command, directory, environment, timeout):
    process = subprocess.Popen(command, cwd=directory, env=environment, start_new_session=os.name != 'nt')
    try:
        if process.wait(timeout=timeout):
            raise ValueError('native Expo compiler failed')
    finally:
        if process.poll() is None:
            if os.name == 'nt':
                process.terminate()
            else:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == 'nt':
                    process.kill()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait()


def build(root, config, environment, run, *, native_runner=compile_native):
    # Complete production mobile outputs before compiling any local-authority Web variant.
    plan = sorted(records(root, config), key=lambda record: record[0] == 'web')
    for kind, directory, output, entry in plan:
        if kind == 'web':
            values = entry.get('environment', {})
            cache_names = list(dict.fromkeys([*environment.get('FLEET_WEB_CACHE_ENV', '').split(), *values]))
            run(sdk_environment.python_entry(str(TOOLS / 'helpers/export-web.py'), '--app', str(directory), '--output', output.name, '--timeout', str(entry.get('timeout', 900))),
                root, environment | values | {'FLEET_WEB_CACHE_ENV': ' '.join(cache_names)})
        else:
            if output.exists():
                shutil.rmtree(output)
            command = ['bun', 'x', '--no-install', 'expo', 'export']
            if not environment.get('FLEET_WEB_CACHE'):
                command.append('--clear')
            for platform in entry['platforms']:
                command += ['--platform', platform]
            native_runner([*command, '--output-dir', str(output)], directory, environment, entry.get('timeout', 900))
        validate(output)
