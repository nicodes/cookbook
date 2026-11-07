"""Native Node package operations; callers select scripts, never shell task strings."""
import sdk_environment
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from configuration import relative
from recipe_schema import NODE_FIELDS as FIELDS, node_package as declaration

import dependencies

COOKBOOK = Path(__file__).resolve().parents[1]
TOOLS = dependencies.tools_root()
SCRIPT = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.:-]*\Z')


def validate(package, root):
    package = declaration(package)
    directory = relative(root, package.get('directory'), exists=True)
    if not directory.is_dir():
        raise ValueError('Node package directory must be a native source directory')
    manifest = json.loads((directory / 'package.json').read_text())
    for name in ['build', 'typecheck', 'lint', 'test', 'format', 'format_check']:
        if name in package:
            value = package[name]
            script = manifest.get('scripts', {}).get(value) if isinstance(value, str) else None
            if not isinstance(value, str) or not SCRIPT.fullmatch(value) or not isinstance(script, str) or not script.strip():
                raise ValueError('Node operation must select an existing native package script: ' + name)
    for name in ['test_arguments', 'test_paths', 'format_paths']:
        values = package.get(name, [])
        if not isinstance(values, list) or any(not isinstance(value, str) or '\0' in value for value in values):
            raise ValueError('Node native arguments and paths must be literal lists')
        if name != 'test_arguments':
            for value in values:
                relative(directory, value, exists=True)
    if package.get('test_paths') and (package.get('manager', 'npm') != 'bun' or package.get('test')):
        raise ValueError('direct native test paths require Bun without a competing test script')
    return package, directory


def run_script(package, directory, script, environment, run, arguments=()):
    manager = package.get('manager', 'npm')
    command = [manager, 'run', script]
    if arguments:
        command += (['--'] if manager == 'npm' else []) + list(arguments)
    run(command, directory, environment)


def install(root, packages, environment, run):
    for value in packages:
        package, directory = validate(value, root)
        command = ['bun', 'install', '--frozen-lockfile'] if package.get('manager', 'npm') == 'bun' else [
            'npm', 'ci', '--no-audit', '--no-fund']
        if package.get('ignore_scripts'):
            command.append('--ignore-scripts')
        run(command, directory, environment)


def build(root, packages, environment, run):
    count = 0
    for value in packages:
        package, directory = validate(value, root)
        if package.get('build'):
            run_script(package, directory, package['build'], environment, run)
            count += 1
    return count


def link(root, config, environment, run):
    """Resolve tracked native fixture imports without executing their behavior."""
    paths = config.get('components', {}).get('module_link', [])
    if not isinstance(paths, list) or any(not isinstance(name, str) for name in paths):
        raise ValueError('native module link inputs require bounded source paths')
    if not paths:
        return
    selected = [relative(root, name, exists=True) for name in paths]
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
    programs = []
    for name in tracked:
        if not name or Path(name).suffix != '.mjs':
            continue
        path = relative(root, name, exists=True)
        if any(path == source or path.is_relative_to(source) for source in selected):
            programs.append(str(path))
    if not programs:
        raise ValueError('native module link inputs contain no tracked JavaScript modules')
    with tempfile.TemporaryDirectory(prefix='native-module-link-') as directory:
        run(['bun', 'build', '--target=node', '--packages=external', '--outdir', directory, *programs], root, environment)


def lint(root, packages, environment, run):
    count = 0
    for value in packages:
        package, directory = validate(value, root)
        if package.get('expo'):
            executable = ['bun', 'x', '--no-install'] if package.get('manager', 'npm') == 'bun' else ['npm', 'exec', '--no', '--']
            # Validate the installed frozen SDK's bundled table, independent of
            # changing hosted recommendations. The CLI still rejects mismatches.
            compatibility = environment | {'EXPO_OFFLINE': '1'}
            compatibility.pop('EXPO_NO_DEPENDENCY_VALIDATION', None)
            run([*executable, 'expo', 'install', '--check'], directory, compatibility)
            count += 1
        for key in ['typecheck', 'lint', 'format_check']:
            if package.get(key):
                run_script(package, directory, package[key], environment, run)
                count += 1
    return count


def test(root, packages, environment, run):
    count = 0
    for value in packages:
        package, directory = validate(value, root)
        if package.get('glob_compatibility'):
            run(['node', str(TOOLS / 'helpers/glob-compatibility.mjs'), str(directory)], root, environment)
            count += 1
        if package.get('test'):
            run_script(package, directory, package['test'], environment, run, package.get('test_arguments', []))
            count += 1
        elif package.get('test_paths'):
            run(['bun', 'test', *package.get('test_arguments', []),
                 *[str(relative(directory, path, exists=True)) for path in package['test_paths']]], directory, environment)
            count += 1
    return count


def audit(root, packages, environment, run):
    count = 0
    for value in packages:
        package, directory = validate(value, root)
        if package.get('manager', 'npm') == 'npm':
            run(['npm', 'audit', '--audit-level=high'], directory, environment)
        else:
            run(sdk_environment.python_entry(str(TOOLS / 'helpers/audit.py'), str(directory)), root, environment)
        count += 1
    return count


def format(root, packages, environment, run):
    count = 0
    for value in packages:
        package, directory = validate(value, root)
        if package.get('format'):
            run_script(package, directory, package['format'], environment, run)
            count += 1
        elif package.get('format_paths'):
            raw = subprocess.check_output(['git', '-C', str(directory), 'ls-files', '-z', '--',
                                           *package['format_paths']], timeout=30)
            files = []
            for encoded in raw.split(b'\0'):
                if not encoded:
                    continue
                name = encoded.decode('utf-8')
                path = relative(directory, name, exists=True)
                if (path.suffix in {'.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.json', '.css', '.scss', '.html'}
                        and path.name not in {'package-lock.json', 'npm-shrinkwrap.json'}):
                    files.append(str(path))
            if not files:
                raise ValueError('Node formatting paths matched no tracked supported source files')
            run(['prettier', '--write', *files], directory, environment)
            count += 1
    return count
