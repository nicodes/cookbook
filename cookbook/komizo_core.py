"""Prepare the recipe provider's single immutable native workload renderer."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib

import artifact_packet
from configuration import relative
import sdk_source

ROOT = Path(__file__).resolve().parents[1]
SDK = '.artifacts/sdk/komizo-core'


def pin():
    value = tomllib.loads(artifact_packet.read_regular(ROOT / 'komizo-core.toml', 4096).decode())
    if (set(value) != {'version', 'repository', 'revision'} or value['version'] != 1
            or value['repository'] != 'nicodes/komizo' or not re.fullmatch('[a-f0-9]{40}', value['revision'])):
        raise ValueError('workload rendering requires the recipe provider exact core source pin')
    return value


def staged(environment):
    value = environment.get('KOMIZO_BOX')
    if not value:
        return None
    if environment.get('GITHUB_ACTIONS') == 'true':
        raise ValueError('Actions rendering must use its accepted immutable core SDK')
    path = Path(value)
    if not path.is_absolute() or artifact_packet.regular(path).st_mode & 0o022:
        raise ValueError('staged core renderer must be an explicit regular non-writable executable')
    return path


def rust_version():
    return tomllib.loads((ROOT / 'mise.toml').read_text())['tools']['rust']


def compiler_environment(environment):
    blocked = {'RUSTUP_TOOLCHAIN','RUSTC','RUSTC_WRAPPER','RUSTC_WORKSPACE_WRAPPER','CARGO_BUILD_RUSTC',
               'RUSTC_BOOTSTRAP','RUSTFLAGS','CARGO_ENCODED_RUSTFLAGS'}
    return {key: value for key, value in environment.items() if key not in blocked}


def rust(root, environment):
    required = rust_version()
    actual = subprocess.check_output(['mise', 'exec', 'rust@' + required, '--', 'rustc', '--version', '--verbose'], cwd=root, env=compiler_environment(environment),
                                     text=True, timeout=30)
    values = dict(line.split(': ', 1) for line in actual.splitlines() if ': ' in line)
    if values.get('release') != required or not re.fullmatch('[A-Za-z0-9_-]{1,128}', values.get('host', '')):
        raise ValueError('core renderer requires the recipe provider reviewed Rust SDK')
    return required, values['host']


def locations(root, selected, toolchain, host):
    directory = relative(root, SDK)
    source = directory / 'source' / selected['revision']
    target = directory / 'build' / (selected['revision'] + '-' + toolchain + '-' + host)
    return directory, source, target


def binary(root, environment):
    override = staged(environment)
    if override is not None:
        return override
    selected = pin()
    toolchain, host = rust(root, environment)
    directory, source, target = locations(root, selected, toolchain, host)
    sdk_source.verify(source, selected['revision'], repository=selected['repository'])
    receipt = artifact_packet.read_json(directory / 'renderer.json', 32768)
    path = directory / 'bin' / (selected['revision'] + '-' + toolchain + '-' + host) / ('komizo-box.exe' if sys.platform == 'win32' else 'komizo-box')
    digest, _ = artifact_packet.digest_file(path)
    expected = {'version': 1, 'repository': selected['repository'], 'revision': selected['revision'],
                'rust': toolchain, 'host': host, 'binary_sha256': digest, 'accepted_main': True}
    if receipt != expected:
        raise ValueError('core renderer SDK is missing, changed or belongs to another toolchain; run setup')
    return path


def setup(root, config, environment, run):
    if not config.get('components', {}).get('assets') or sys.platform != 'linux':
        return None
    return prepare(root, environment, run)


def prepare(root, environment, run):
    override = staged(environment)
    if override is not None:
        return override
    selected = pin()
    run(['mise', 'install', 'rust@' + rust_version()], root, compiler_environment(environment), timeout=300)
    toolchain, host = rust(root, environment)
    directory, source, target = locations(root, selected, toolchain, host)
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    relation = subprocess.check_output(['gh', 'api', 'repos/' + selected['repository'] + '/compare/'
        + selected['revision'] + '...main', '--jq', '.status'], cwd=root, env=environment,
        text=True, timeout=30).strip()
    if relation not in {'ahead', 'identical'}:
        raise ValueError('the workload renderer source must be accepted on main before recipe adoption')
    if not source.exists():
        source.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        run(['git', 'init', str(source)], root, environment, timeout=30)
        run(['git', '-C', str(source), 'remote', 'add', 'origin',
             'https://github.com/' + selected['repository'] + '.git'], root, environment, timeout=30)
        run(['git', '-c', 'core.hooksPath=' + os.devnull, '-C', str(source), 'fetch',
             '--depth', '1', '--no-recurse-submodules', 'origin', selected['revision']],
            root, environment, timeout=180)
        run(['git', '-c', 'core.hooksPath=' + os.devnull, '-C', str(source), 'checkout',
             '--detach', selected['revision']], root, environment, timeout=30)
    sdk_source.verify(source, selected['revision'], repository=selected['repository'])
    try:
        return binary(root, environment)
    except (ValueError, FileNotFoundError):
        pass
    target.mkdir(parents=True, mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='komizo-renderer-build-') as working:
        run(['mise', 'exec', 'rust@' + toolchain, '--', 'cargo', 'build', '--locked', '--release', '--manifest-path', str(source / 'Cargo.toml'),
             '--bin', 'komizo-box', '--target-dir', str(target)], Path(working), compiler_environment(environment), timeout=900)
    destination = sdk_source.no_links(directory / 'bin' / (selected['revision'] + '-' + toolchain + '-' + host))
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    executable = destination / 'komizo-box'
    with tempfile.NamedTemporaryFile(prefix='.renderer-', dir=destination, delete=False) as output:
        temporary = Path(output.name)
        with (target / 'release/komizo-box').open('rb') as incoming:
            shutil.copyfileobj(incoming, output)
        output.flush()
        os.fsync(output.fileno())
    temporary.chmod(0o755)
    os.replace(temporary, executable)
    digest, _ = artifact_packet.digest_file(executable)
    receipt = {'version': 1, 'repository': selected['repository'], 'revision': selected['revision'],
               'rust': toolchain, 'host': host, 'binary_sha256': digest, 'accepted_main': True}
    (directory / 'renderer.json').write_text(json.dumps(receipt, sort_keys=True) + '\n')
    return binary(root, environment)
