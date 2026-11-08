"""Explicitly install selected existing caller pins, then bind exact Python."""
import os
from pathlib import Path
import shutil
import subprocess

import dependencies
import effective_tasks
import sdk_environment


def install(root, selected, environment=None):
    root = Path(root).resolve()
    environment = {**(os.environ if environment is None else environment),
                   'MISE_AUTO_INSTALL': 'false', 'MISE_EXEC_AUTO_INSTALL': 'false',
                   'MISE_TASK_RUN_AUTO_INSTALL': 'false'}
    pinned = dependencies.helper('pinned-tools')
    effective_tasks.check(root, environment)
    declared = pinned.pins(root)
    names = dependencies.helper('select-tools').select(root, selected) if selected else list(declared)
    if 'python' not in names:
        raise ValueError('Cookbook CI bootstrap must prepare the exact caller Python SDK')
    selectors = []
    for name in names:
        value = declared[name]
        version = value.get('version') if isinstance(value, dict) else value
        if not isinstance(version, str) or not pinned.PIN.fullmatch(version):
            raise ValueError('SDK installation requires each existing immutable caller pin')
        selectors.append(name + '@' + version)
    environment = sdk_environment.install_environment(root, environment, ' '.join(names))
    subprocess.run(['mise', 'install', *selectors], cwd=root, env=environment, check=True)
    values = pinned.activation(root, [('python', 'python3')], environment)
    executable = Path(shutil.which('python3', path=values['PATH'])).resolve()
    expected = declared['python']
    expected = expected.get('version') if isinstance(expected, dict) else expected
    actual = subprocess.check_output([str(executable), '-I', '-B', '-c',
        "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
        cwd=root, env=values, text=True, timeout=30).strip()
    if actual != expected or any(character in str(executable) for character in '\r\n\0'):
        raise ValueError('native Cookbook Python differs from the exact installed caller pin')
    sdk_environment.publish_install_environment(values)
    if os.environ.get('GITHUB_ENV'):
        with open(os.environ['GITHUB_ENV'], 'a') as output:
            output.write('COOKBOOK_BOOTSTRAP_PYTHON=' + str(executable) + '\n')
    if os.environ.get('GITHUB_PATH'):
        with open(os.environ['GITHUB_PATH'], 'a') as output:
            output.write(str(executable.parent) + '\n')
    return values


if __name__ == '__main__':
    install(Path.cwd(), os.environ.get('CALLER_TOOLS', ''))
