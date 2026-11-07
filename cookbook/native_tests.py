"""Invoke owned native test programs from bounded data, without shell task strings."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import re
import sdk_environment

from configuration import RUNNERS, relative

import dependencies

COOKBOOK = Path(__file__).resolve().parents[1]
TOOLS = dependencies.tools_root()


def runner_tools(entries):
    """Derive scalar Mise SDKs from already declared native test runners.

    Shells and pinned engine/archive capabilities retain their separate setup
    and readiness guards; they are not caller Mise SDK identifiers.
    """
    selected = set()
    for entry in entries:
        runner = entry['runner']
        if runner not in RUNNERS:
            raise ValueError('recipe does not implement native test runner: ' + str(runner))
        if runner in {'python', 'python-unittest'}:
            selected.add('python')
        elif runner in {'node', 'node-test'}:
            selected.add('node')
        elif runner in {'bun', 'bun-test'}:
            selected.add('bun')
    return selected


def godot_runner():
    spec = importlib.util.spec_from_file_location('cookbook_native_godot', TOOLS / 'helpers/godot-test.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_entries(root, config, environment, suite, custom_runners=None, command_wrapper=None):
    entries = config.get('tests', {}).get(suite, [])
    if not entries:
        raise ValueError('test group has no declared product checks: ' + suite)
    custom_runners = custom_runners or {}
    for entry in entries:
        program = relative(root, entry['path'], exists=True)
        cwd = relative(root, entry.get('cwd', '.'), exists=True)
        kind = entry['runner']
        arguments = entry.get('args', [])
        timeout = entry.get('timeout', 30 if kind.startswith('godot') else 300)
        if kind in custom_runners:
            custom_runners[kind](root, entry, environment)
            continue
        if kind in {'godot', 'godot-suite'}:
            native = godot_runner()
            if kind == 'godot-suite':
                native.suite(environment['GODOT_BIN'], cwd, program, entry['sentinel'], timeout, environment)
            else:
                native.run(environment['GODOT_BIN'], cwd, program, entry.get('sentinel'), timeout, arguments, environment,
                           **entry.get('policy', {}))
            continue
        python = sys.executable
        if entry.get('python_environment'):
            import python_component
            python = python_component.interpreter(root, config, entry['python_environment'])
        if kind in {'python', 'node', 'bun', 'bash'}:
            executable = {'python': python, 'node': 'node', 'bun': 'bun', 'bash': 'bash'}[kind]
            command = [executable, str(program), *arguments]
        elif kind == 'python-unittest':
            directory = program if program.is_dir() else program.parent
            pattern = entry.get('pattern', 'test*.py' if program.is_dir() else program.name)
            if not list(directory.rglob(pattern)):
                raise ValueError('Python test discovery matched no native test files')
            command = [python, '-m', 'unittest', 'discover', '-s', str(directory), '-p', pattern, *arguments]
        elif kind == 'node-test':
            files = sorted(program.rglob(entry.get('pattern', '*.test.*'))) if program.is_dir() else [program]
            if not files or any(path.is_symlink() or not path.is_file() for path in files):
                raise ValueError('Node test discovery requires regular native test files')
            command = ['node', '--test', *arguments, *map(str, files)]
        elif kind == 'bun-test':
            command = ['bun', 'test', '--bail', *arguments, str(program)]
        else:
            raise ValueError('recipe does not implement native test runner: ' + kind)
        if command_wrapper:
            command = command_wrapper(command, environment)
        values = sdk_environment.command_environment(command, root, environment)
        if kind == 'python-unittest':
            result = subprocess.run(command, cwd=cwd, env=values, timeout=timeout, capture_output=True, text=True)
            print(result.stdout, end='')
            print(result.stderr, end='', file=sys.stderr)
            summaries = re.findall(r'^Ran ([0-9]+) tests? in ', result.stderr, re.M)
            if summaries and int(summaries[-1]) == 0:
                raise ValueError('Python unittest discovery did not run any test cases')
            result.check_returncode()
            if not summaries:
                raise ValueError('Python unittest did not report its completed test cases')
        else:
            subprocess.run(command, cwd=cwd, env=values, timeout=timeout, check=True)
