"""Explicit setup and activation of caller-pinned Godot extension SDKs."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tomllib
import urllib.request

from configuration import relative
from recipe_contract import RUST_WEB_DIRECTORY

PIN_PATTERNS = {
    'RUST_NIGHTLY': r'nightly-\d{4}-\d{2}-\d{2}',
    'EMSCRIPTEN_VERSION': r'\d+\.\d+\.\d+',
    'EMSDK_SOURCE_COMMIT': r'[a-f0-9]{40}',
    'EMSDK_ARCHIVE_SHA256': r'[a-f0-9]{64}',
    'LIBCLANG_VERSION': r'\d+\.\d+\.\d+',
}


def inputs(root, config):
    value = config.get('components', {}).get('godot', {}).get('rust_web')
    if value is None:
        return None
    if not isinstance(value, dict) or value:
        raise ValueError('Godot extension SDKs use their canonical directory, with pins in Mise only')
    directory = relative(root, RUST_WEB_DIRECTORY)
    pins = tomllib.loads((root / 'mise.toml').read_text()).get('env', {})
    selected = {}
    for key, expression in PIN_PATTERNS.items():
        value = pins.get(key)
        if not isinstance(value, str) or not re.fullmatch(expression, value):
            raise ValueError('extension SDKs require an exact caller Mise pin: ' + key)
        selected[key] = value
    return directory, selected


def bootstrap(archive, destination, pins, *, restore=False):
    if archive.is_symlink() or not archive.is_file() or hashlib.sha256(archive.read_bytes()).hexdigest() != pins['EMSDK_ARCHIVE_SHA256']:
        raise ValueError('Emscripten bootstrap archive differs from its reviewed checksum')
    with tarfile.open(archive) as source:
        plan = []
        roots = set()
        seen = set()
        for member in source.getmembers():
            path = Path(member.name)
            if not path.parts or path.is_absolute() or '..' in path.parts or '\\' in member.name:
                raise ValueError('unsafe Emscripten bootstrap archive path')
            roots.add(path.parts[0])
            name = Path(*path.parts[1:])
            if not name.parts:
                continue
            if name.as_posix() in seen:
                raise ValueError('duplicate Emscripten bootstrap member')
            seen.add(name.as_posix())
            target = destination / name
            if any(parent.is_symlink() for parent in [target, *target.parents]):
                raise ValueError('Emscripten bootstrap files may not contain links')
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError('unsupported Emscripten bootstrap archive entry')
            data = source.extractfile(member).read()
            if target.exists() and (not target.is_file() or target.read_bytes() != data):
                raise ValueError('modified Emscripten bootstrap file: ' + name.as_posix())
            if not restore and not target.is_file():
                raise ValueError('missing Emscripten bootstrap file; run mise run setup')
            plan.append((target, data, member.mode))
        if len(roots) != 1 or not plan:
            raise ValueError('Emscripten bootstrap requires one nonempty source tree')
        # Codeload's archive digest is the reviewed byte authority. When a Git
        # source identity is embedded in the archive, require the declared pin too.
        embedded = source.pax_headers.get('comment')
        if embedded and re.fullmatch(r'[a-f0-9]{40}', embedded) and embedded != pins['EMSDK_SOURCE_COMMIT']:
            raise ValueError('Emscripten archive source identity differs from its reviewed pin')
        if restore:
            for target, data, mode in plan:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                target.chmod(mode & 0o777)


def run(command, root, environment, timeout):
    return subprocess.run(command, cwd=root, env=environment, check=True, timeout=timeout)


def setup(root, config, environment, execute=run):
    declared = inputs(root, config)
    if declared is None:
        return environment
    directory, pins = declared
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / 'emsdk-source.tar.gz'
    if not archive.is_file() or archive.is_symlink() or hashlib.sha256(archive.read_bytes()).hexdigest() != pins['EMSDK_ARCHIVE_SHA256']:
        url = 'https://codeload.github.com/emscripten-core/emsdk/tar.gz/refs/tags/' + pins['EMSCRIPTEN_VERSION']
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read(4 * 1024 * 1024)
        if hashlib.sha256(data).hexdigest() != pins['EMSDK_ARCHIVE_SHA256']:
            raise ValueError('downloaded Emscripten bootstrap failed its reviewed checksum')
        if archive.is_symlink():
            raise ValueError('SDK bootstrap archive may not be a link')
        archive.write_bytes(data)
    sdk = directory / ('emsdk-' + pins['EMSCRIPTEN_VERSION'])
    bootstrap(archive, sdk, pins, restore=True)
    values = {**environment, 'TAR_OPTIONS': environment.get('TAR_OPTIONS', '') + ' --no-same-owner'}
    for operation in ['install', 'activate']:
        execute([str(sdk / 'emsdk'), operation, pins['EMSCRIPTEN_VERSION']], root, values, 600)
    clang = directory / 'clang-venv'
    execute([sys.executable, '-m', 'venv', '--copies', str(clang)], root, environment, 180)
    python = clang / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    relative(root, python.relative_to(root).as_posix(), exists=True)
    execute([str(python), '-m', 'pip', 'install', '--disable-pip-version-check', 'libclang==' + pins['LIBCLANG_VERSION']], root, environment, 180)
    (directory / 'sdk.json').write_text(json.dumps({'schema': 1, 'pins': pins}, sort_keys=True) + '\n')
    return activate(root, config, environment)


def activate(root, config, environment):
    declared = inputs(root, config)
    if declared is None:
        return environment
    directory, pins = declared
    for key, value in pins.items():
        if environment.get(key) not in {None, value}:
            raise ValueError('ambient SDK selection differs from the caller Mise pin: ' + key)
    receipt = relative(root, (directory / 'sdk.json').relative_to(root).as_posix(), exists=True)
    if json.loads(receipt.read_text()) != {'schema': 1, 'pins': pins}:
        raise ValueError('extension SDK setup is stale; run mise run setup')
    sdk = directory / ('emsdk-' + pins['EMSCRIPTEN_VERSION'])
    bootstrap(directory / 'emsdk-source.tar.gz', sdk, pins)
    activation = relative(root, (sdk / 'emsdk_env.sh').relative_to(root).as_posix(), exists=True)
    # Only the reviewed SDK's own activation program is sourced. Paths are
    # positional arguments; no caller-owned task or shell text is evaluated.
    command = ['bash', '-c', 'set -euo pipefail; source "$1" >/dev/null 2>&1; exec "$2" -c \'import json,os;print(json.dumps(dict(os.environ)))\'',
               'cookbook-emsdk', str(activation), sys.executable]
    activated = json.loads(subprocess.check_output(command, cwd=root, env=environment, text=True, timeout=30))
    activated.update(pins)
    for key, value in environment.items():
        if key.startswith('GITHUB_'):
            activated[key] = value
    python = directory / 'clang-venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    relative(root, python.relative_to(root).as_posix(), exists=True)
    probe = 'import importlib.metadata,json,pathlib,clang;print(json.dumps({"version":importlib.metadata.version("libclang"),"native":str(pathlib.Path(clang.__file__).parent/"native")}))'
    clang = json.loads(subprocess.check_output([str(python), '-c', probe], cwd=root, env=activated, text=True, timeout=30))
    if clang['version'] != pins['LIBCLANG_VERSION'] or not Path(clang['native']).is_dir():
        raise ValueError('installed libclang does not match the caller SDK pin')
    activated['LIBCLANG_PATH'] = clang['native']
    version = subprocess.check_output(['emcc', '--version'], cwd=root, env=activated, text=True, timeout=30)
    matches = re.findall(r'^emcc \(.*\) (\d+\.\d+\.\d+)(?:\s|$)', version, re.M)
    if matches != [pins['EMSCRIPTEN_VERSION']]:
        raise ValueError('activated Emscripten does not match the caller SDK pin')
    return activated


if __name__ == '__main__':
    if sys.argv[1:] != ['setup']:
        raise SystemExit('usage: godot_sdk.py setup')
    from configuration import load
    try:
        root = Path.cwd().resolve()
        setup(root, load(root), dict(os.environ))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from None
