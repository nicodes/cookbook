"""Read-only secret provenance policy with an explicitly prepared, hash-locked YAML SDK."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent))
from configuration import relative

import dependencies

COOKBOOK = Path(__file__).resolve().parents[1]
TOOLS = dependencies.tools_root()
REQUIREMENTS = TOOLS / 'helpers/source-secret-policy-requirements.txt'
CHECKER = TOOLS / 'helpers/source-secret-policy.py'
SDK = '.artifacts/sdk/source-policy'
SERVICE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z')


def declaration(root, config):
    value = config.get('lint', {}).get('secret_policy')
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) - {'application', 'workflows', 'scoped_env_dir', 'host_only', 'secrets_env_services'}
            or not {'application', 'workflows'} <= value.keys()):
        raise ValueError('source secret policy requires native workload/workflow inputs and bounded reader data')
    application = relative(root, value['application'], exists=True)
    workflows = value['workflows']
    if not isinstance(workflows, list) or not workflows or len(workflows) > 8 or len(set(workflows)) != len(workflows):
        raise ValueError('source secret policy requires a closed list of native delivery workflows')
    files = [relative(root, name, exists=True) for name in workflows]
    if any(not path.is_file() or path.stat().st_size > 1024 * 1024 for path in [application, *files]):
        raise ValueError('source policy inputs must be bounded regular native files')
    scoped = value.get('scoped_env_dir', 'secrets/current')
    if not isinstance(scoped, str):
        raise ValueError('scoped environment prefix must be literal source data')
    if scoped:
        relative(root, scoped)
    host = value.get('host_only', {})
    if not isinstance(host, dict) or len(host) > 32:
        raise ValueError('host-only files require explicitly declared service readers')
    for path, readers in host.items():
        relative(root, path)
        services(readers)
    readers = value.get('secrets_env_services', [])
    services(readers, empty=True)
    return {'application': value['application'], 'workflows': workflows, 'scoped_env_dir': scoped,
            'host_only': host, 'secrets_env_services': readers}


def services(values, *, empty=False):
    if (not isinstance(values, list) or (not values and not empty) or len(values) > 32
            or any(not isinstance(name, str) or not SERVICE.fullmatch(name) for name in values)
            or len(set(values)) != len(values)):
        raise ValueError('source secret policy readers must be distinct literal native service names')


def inputs(root):
    return dependencies.helper('source_policy').inputs(root)


def setup(root, config, environment, run=subprocess.run, *, required=False):
    if declaration(root, config) is not None or required:
        dependencies.helper('source_policy').setup(root, environment, run)


def interpreter(root):
    return dependencies.helper('source_policy').interpreter(root)


def check(root, config, environment, run):
    value = declaration(root, config)
    if value is None:
        return
    command = [interpreter(root), '-I', str(CHECKER), '--application', value['application'],
               '--scoped-env-dir', value['scoped_env_dir']]
    for path in value['workflows']:
        command += ['--workflow', path]
    for path, readers in value['host_only'].items():
        command += ['--host-only', path + ': ' + ','.join(readers)]
    if value['secrets_env_services']:
        command += ['--secrets-env-services', ','.join(value['secrets_env_services'])]
    run(command, root, environment)


if __name__ == '__main__':
    root = Path.cwd().resolve()
    if sys.argv[1:] == ['setup']:
        setup(root, {}, dict(os.environ), required=True)
    elif sys.argv[1:] == ['actions']:
        subprocess.run([interpreter(root), '-I', str(TOOLS / 'helpers/composite-actions.py'), str(root)], check=True,
                       env=dict(os.environ), timeout=60)
    elif sys.argv[1:] == ['test']:
        subprocess.run(['bash', str(TOOLS / 'tests/source-secret-policy.test.sh')], check=True,
                       env=dict(os.environ, SOURCE_POLICY_PYTHON=interpreter(root)), timeout=180)
    elif sys.argv[1:2] == ['check']:
        arguments = sys.argv[2:]
        sdk_root = root
        if arguments[:1] == ['--sdk-root']:
            if len(arguments) < 3:
                raise SystemExit('check --sdk-root requires a prepared source checkout and checker arguments')
            sdk_root = Path(arguments[1]).resolve(strict=True)
            arguments = arguments[2:]
        raise SystemExit(subprocess.run([interpreter(sdk_root), '-I', str(CHECKER), *arguments],
                                       cwd=root, env=dict(os.environ), timeout=180).returncode)
    else:
        raise SystemExit('usage: source_policy.py setup|actions|test|check [checker arguments]')
