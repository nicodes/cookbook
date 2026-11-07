#!/usr/bin/env python3
"""Contribute to the Cookbook provider with its separately pinned generic tools."""
import argparse
import json
import os
from pathlib import Path
import py_compile
import subprocess
import sys

import dependencies
import sdk_environment

ROOT = Path(__file__).resolve().parents[1]


def run(command, *, environment=None):
    values = sdk_environment.command_environment(command, ROOT,
        {**(environment or dict(os.environ)), 'COOKBOOK_CALLER_ROOT': str(ROOT)})
    subprocess.run(command, cwd=ROOT, env=values, check=True)


def source():
    if os.environ.get('GITHUB_ENV'):
        with open(os.environ['GITHUB_ENV'], 'a') as output:
            output.write('TOOLS_ROOT=' + str(dependencies.tools_root()) + '\n')
            output.write('TOOLS_REF=' + dependencies.tools_identity()['revision'] + '\n')


def setup():
    source()
    dependencies.helper('source_policy').setup(ROOT, dict(os.environ))
    run(['rustup', 'component', 'add', '--toolchain', '1.94.0', 'rustfmt', 'clippy'])


def lint():
    errors = dependencies.helper('documentation-policy').check(ROOT, history_required=True)
    if errors:
        raise ValueError('\n'.join(errors))
    run(['actionlint', *[str(path.relative_to(ROOT)) for path in sorted((ROOT / '.github/workflows').glob('*.yml'))]])
    for directory in ['cookbook', 'helpers', 'tests']:
        for path in (ROOT / directory).rglob('*.py'):
            compile(path.read_bytes(), str(path), 'exec')
    policy = dependencies.helper('source_policy')
    run([policy.interpreter(ROOT), '-I', str(dependencies.tools_root() / 'helpers/composite-actions.py'), str(ROOT)])


def build():
    destination = ROOT / '.artifacts/python-bytecode'
    for path in sorted((ROOT / 'cookbook').glob('*.py')):
        target = destination / (path.name + 'c')
        target.parent.mkdir(parents=True, exist_ok=True)
        py_compile.compile(str(path), cfile=str(target), doraise=True)


def unit():
    revision = dependencies.sdk_source.git(ROOT, 'rev-parse', 'HEAD')
    directory = ROOT / '.artifacts/test-sdk' / revision
    if not directory.exists():
        directory.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['git', '-C', str(ROOT), 'worktree', 'add', '--detach', str(directory), revision], check=True)
    dependencies.sdk_source.verify(directory, revision, repository='nicodes/cookbook')
    environment = {**os.environ, 'COOKBOOK_TEST_SOURCE': str(directory), 'COOKBOOK_TEST_REF': revision}
    run([sys.executable, '-I', '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py', '-v'],
        environment=environment)


def security():
    run([sys.executable, str(dependencies.tools_root() / 'helpers/source-secrets.py')])


def execute(operation):
    operations = {'source': source, 'setup': setup, 'build': build, 'test:lint': lint,
                  'test:unit': unit, 'test:security': security}
    if operation == 'test':
        for name in ['test:lint', 'test:unit', 'test:security']:
            operations[name]()
    elif operation == 'clean':
        dependencies.helper('clean').clean(ROOT, ['.artifacts/python-bytecode'])
    else:
        operations[operation]()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['source', 'setup', 'build', 'test', 'test:lint', 'test:unit', 'test:security', 'clean'])
    args = parser.parse_args()
    try:
        execute(args.operation)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')
