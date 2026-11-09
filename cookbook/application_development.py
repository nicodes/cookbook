#!/usr/bin/env python3
"""Shared application development, database isolation and recipe-dispatched RPC."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tomllib

import sdk_environment

from configuration import load, relative, runtime_configuration

import dependencies

COOKBOOK = Path(__file__).resolve().parents[1]
TOOLS = dependencies.tools_root()
ENVIRONMENT = re.compile(r'[A-Z][A-Z0-9_]*\Z')


def native_entry(root, entry, environment, *, sdk=False, long_running=False, allowed_roots=None, protected_environment=()):
    if not isinstance(entry, dict) or set(entry) - {'runner', 'path', 'cwd', 'args', 'timeout', 'environment', 'phase', 'stages'}:
        raise ValueError('invalid native product component entry')
    path = relative(root, entry.get('path'), exists=True)
    if not path.is_file() or Path(entry['path']).parts[0] not in (allowed_roots or {'app', 'api', 'internal', 'tests'}):
        raise ValueError('native product generators and assertions must remain in their owning component')
    runner = {'python': sys.executable, 'node': 'node', 'bun': 'bun', 'bash': 'bash'}.get(entry.get('runner'))
    if runner is None:
        raise ValueError('unknown native product component runner')
    arguments = entry.get('args', [])
    if not isinstance(arguments, list) or any(not isinstance(value, str) or '\0' in value for value in arguments):
        raise ValueError('native product arguments must be literal strings')
    timeout = None if long_running else entry.get('timeout', 300)
    if not long_running and (type(timeout) is not int or not 1 <= timeout <= 3600):
        raise ValueError('native product operations require bounded deadlines')
    values = entry.get('environment', {})
    if not isinstance(values, dict) or any(not ENVIRONMENT.fullmatch(key) or not isinstance(value, str) for key, value in values.items()):
        raise ValueError('native product environment must use literal named values')
    if any(key in protected_environment or key.startswith(('GITHUB_','MISE_','COOKBOOK_','REQUEST_')) for key in values):
        raise ValueError('native components may not override protected execution or SDK authority')
    values = {key: value.replace('{revision}', environment['APP_VERSION']) for key, value in values.items()}
    command = [runner, str(path), *arguments]
    if sdk:
        environment = sdk_environment.command_environment(command, root, environment)
    return command, relative(root, entry.get('cwd', '.'), exists=True), environment | values, timeout


def native(root, entries, environment, *, api, sdk=False, long_running=False, allowed_roots=None, protected_environment=()):
    for entry in entries:
        command,cwd,values,timeout=native_entry(root,entry,environment,sdk=sdk,long_running=long_running,
            allowed_roots=allowed_roots,protected_environment=protected_environment)
        api.run(command,cwd,values,timeout)


def development_project(root, config=None):
    # Preserve the existing checkout identity; retained database volumes continue
    # to belong to exactly the same development project after this migration.
    config = load(root) if config is None else config
    prefix = config.get('dev', {}).get('project_prefix', 'komizo')
    if not isinstance(prefix, str) or not re.fullmatch('[a-z][a-z0-9-]{0,31}', prefix):
        raise ValueError('development project identity requires a bounded native prefix')
    return prefix + '-' + hashlib.sha256(os.path.normpath(str(root)).encode()).hexdigest()[:16]


def select_port(preferred, attempts=100):
    if type(preferred) is not int or type(attempts) is not int or not 1 <= preferred <= 65535 or not 1 <= attempts <= 1000:
        raise ValueError('development port and bounded attempt count must be positive native integers')
    for port in range(preferred, min(preferred + attempts, 65536)):
        with socket.socket() as listener:
            try:
                listener.bind(('127.0.0.1', port))
            except OSError:
                continue
            return port
    raise ValueError('no available checkout development port')


def launch_development(root, config, environment, *, api):
    values = config.get('dev', {})
    if values.get('kind') == 'expo':
        directory = relative(root, values.get('directory', 'app'), exists=True)
        port = api.select_port(values.get('port', 8081), values.get('port_attempts', 100))
        print('Development app: http://127.0.0.1:' + str(port), flush=True)
        api.run(['bun', '--no-env-file', 'run', '--cwd', str(directory), 'start', '--port', str(port)],
            root, environment | {'EXPO_NO_DOTENV': '1'})
    elif values.get('kind') == 'native' and values.get('program'):
        api.native(root, [values['program']], environment, long_running=True)
    else:
        raise ValueError('development requires an implemented Expo or native product driver')


def clean_outputs(root, config, environment, *, api):
    api.stop_services(root, config, environment)
    paths = config.get('clean', {}).get('paths', [])
    if not paths:
        raise ValueError('clean requires explicit generated output paths')
    api.helper('clean').clean(root, paths)


def stop_services(root, config, environment, *, api):
    if config.get('dev', {}).get('cleanup'):
        api.native(root, [config['dev']['cleanup']], environment)


def isolated_operation(root, config, environment, operation, *, api):
    selected = relative(root, config.get('dev', {}).get('identity', 'ghenv.toml'), exists=True)
    base = [api.pinned_ghenv(root, environment), 'local', '--', 'env', 'DEV_PROJECT=' + api.development_project(root, config),
            'TOOLS_ROOT=' + str(TOOLS), sys.executable, str(Path(__file__).resolve()), operation]
    if selected.name != 'ghenv.toml' or selected.parent != root:
        raise ValueError('application development identity uses the owned root ghenv.toml')
    return base


def pinned_ghenv(root, environment):
    pins = tomllib.loads((root / 'mise.toml').read_text()).get('tools', {})
    if 'github:nicodes/ghenv' not in pins:
        raise ValueError('development identity requires an exact caller-owned ghenv SDK pin')
    directory = Path(subprocess.check_output(['mise', 'where', 'github:nicodes/ghenv'],
                     cwd=root, env=environment, text=True).strip()).resolve()
    executable = Path(subprocess.check_output(['mise', 'which', 'ghenv'],
                      cwd=root, env=environment, text=True).strip()).resolve()
    if not executable.is_relative_to(directory) or not executable.is_file():
        raise ValueError('ghenv must resolve inside its installed pinned SDK')
    return str(executable)


def development(root, config, environment, operation, *, api):
    supervisor = sdk_environment.python_entry(str(TOOLS / 'helpers/dev.py'))
    if operation == 'dev:logs':
        api.run([*supervisor, 'logs', '--state', '.local/dev'], root, environment)
        return
    if not config.get('dev'):
        raise ValueError('application does not implement a development service')
    if operation == 'dev:stop':
        api.run([*supervisor, 'stop', '--state', '.local/dev'], root, environment)
        cleanup = api.isolated_operation(root, config, environment, '_stop-services')
        api.run([*supervisor, 'clean', '--state', '.local/dev', '--', *cleanup], root, environment)
        return
    cleanup = api.isolated_operation(root, config, environment, '_stop-services')
    ghenv = api.pinned_ghenv(root, environment)
    mode = environment.get('DEV_MODE', 'auto')
    if mode not in {'auto', 'local', 'preview'}:
        raise ValueError('development identity must be local, preview or the configured automatic mode')
    native_config = runtime_configuration().prepare(root, 'ghenv.toml', 'ghenv')
    command = [ghenv, 'dev', '--config', str(native_config), '--mode', mode, '--', 'env',
               'DEV_PROJECT=' + api.development_project(root, config), 'TOOLS_ROOT=' + str(TOOLS),
               sys.executable, str(Path(__file__).resolve()), '_dev']
    preparation = [ghenv, 'local', '--', 'env', 'COOKBOOK_STAGE=local', *sdk_environment.python_entry(COOKBOOK / 'cookbook/dispatch.py', 'setup')]
    arguments = [*supervisor, 'run', '--state', '.local/dev', '--prepare', json.dumps(preparation),
                 '--cleanup', json.dumps(cleanup), '--policy-input', 'mise.toml',
                 '--policy-input', 'cookbook.toml', '--policy-input', 'ghenv.toml']
    if config['dev'].get('capture_output', False):
        arguments += ['--log', '.local/dev/output.log']
    api.run([*arguments, '--', *command], root, environment)


def database(root, config, environment, operation, *, api):
    name = operation.split(':', 1)[1]
    entry = config.get('database', {}).get(name)
    if not isinstance(entry, dict):
        raise ValueError('database operation has no implemented native product entrypoint')
    arguments = [*api.isolated_operation(root, config, environment, '_database'), '--suite', name]
    if name == 'reset':
        arguments = sdk_environment.python_entry(str(TOOLS / 'helpers/dev.py'), 'clean', '--state', '.local/dev', '--', *arguments)
    api.run(arguments, root, environment)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['_dev','_stop-services','_clean','_database'])
    parser.add_argument('--suite')
    arguments=parser.parse_args()
    root=Path.cwd().resolve()
    try:
        import recipe
        config=load(root)
        if config.get('recipe') not in {'react-native','godot-game'}:
            raise ValueError('application lifecycle requires its declared owning recipe')
        api=recipe.adapter(config)
        environment=recipe.configure_environment(root,config)
        if arguments.operation in {'_dev','_database'} and hasattr(api,'check_development_sdks'):
            api.check_development_sdks(root,config,environment,arguments.operation,arguments.suite)
        if arguments.operation=='_dev':
            api.launch_development(root,config,environment)
        elif arguments.operation=='_stop-services':
            api.stop_services(root,config,environment)
        elif arguments.operation=='_clean':
            api.clean_outputs(root,config,environment)
        else:
            if arguments.suite not in {'status','migrate','seed','reset'}:
                raise ValueError('database RPC requires one implemented standard operation')
            entry=config.get('database',{}).get(arguments.suite)
            if not isinstance(entry,dict):raise ValueError('database operation is not implemented')
            api.native(root,[entry],environment)
    except (OSError,ValueError,KeyError,RuntimeError,subprocess.SubprocessError) as error:
        parser.exit(1,str(error)+'\n')


if __name__=='__main__':
    main()
