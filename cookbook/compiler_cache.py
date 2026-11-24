#!/usr/bin/env python3
"""Cache compiler intermediates and Cargo downloads, never accepted product evidence."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import tomllib

from configuration import load, relative
import mise_entrypoint
import recipe
import rust_component

MAX_BYTES = 12 << 30
MAX_FILES = 250000
VERSION = 'compiler-v2-'
OS_RELEASE = Path('/etc/os-release')


def clean(value):
    return re.sub('[^a-z0-9.]+', '_', value.lower()).strip('_')[:48]


def image_identity(environment=None, system=None, os_release=OS_RELEASE, glibc=None):
    """The runner image a cached compiler payload was produced on.

    Build scripts and proc-macro binaries in a Cargo target directory are linked against the glibc of the image
    that built them, so a cache must never cross images. GitHub-hosted runners name their image in ImageOS
    (ubuntu22, ubuntu24, macos15, win22). Third-party runners often leave it unset, so Linux falls back to the
    distribution in os-release, and also records the glibc version, which is what actually decides compatibility.
    ImageVersion is deliberately left out: it changes weekly and would empty every cache.
    """
    environment = os.environ if environment is None else environment
    system = system or platform.system()
    named = clean(environment.get('ImageOS', ''))
    if system == 'Linux':
        if not named:
            fields = {}
            try:
                for line in Path(os_release).read_text().splitlines():
                    key, _, value = line.partition('=')
                    fields[key] = value.strip('"\'')
            except OSError:
                pass
            named = clean(fields.get('ID', 'linux') + fields.get('VERSION_ID', ''))
        if glibc is None:
            try:
                glibc = os.confstr('CS_GNU_LIBC_VERSION').split()[-1]
            except (OSError, ValueError, AttributeError, IndexError):
                glibc = 'unknown'
        return named + '-' + clean(glibc)
    if not named:
        if system == 'Darwin':
            named = 'macos' + clean(platform.mac_ver()[0])
        elif system == 'Windows':
            named = 'windows' + clean(platform.version())
        else:
            named = clean(system or 'unknown')
    return named



def plan(root, config, environment, source, runner_os, runner_arch, target='', *, image):
    components = config.get('components', {}).get('rust', [])
    if not components:
        return {'enabled': False}
    if (not re.fullmatch('[a-f0-9]{40}', source) or not re.fullmatch('[A-Za-z0-9_-]{1,32}', runner_os)
            or not re.fullmatch('[A-Za-z0-9_-]{1,32}', runner_arch) or not re.fullmatch('[a-z0-9._-]{1,64}', image) or (target and not rust_component.TARGET.fullmatch(target))):
        raise ValueError('compiler cache requires exact source and native runner identities')
    directories = set()
    mise_source = (root / 'mise.toml').read_bytes()
    sdk_settings, generated = mise_entrypoint.split(mise_source)
    if generated is not None:
        mise_entrypoint.decode(mise_source)
    inputs = {'mise_sha256': hashlib.sha256(sdk_settings.encode('utf-8')).hexdigest(),
              'components': components, 'native': config.get('components', {}).get('native', []),
              'flags': {key: value for key, value in environment.items() if key == 'RUSTFLAGS' or re.fullmatch('CARGO_[A-Z0-9_]+_RUSTFLAGS', key)},
              'files': {}}
    records = components
    if config.get('recipe') in {'react-native', 'godot-game'}:
        records = recipe.adapter(config).cargo_records(config, {**environment, 'APP_VERSION': source})
    for component in records:
        rust_component.validate(component, root)
        manifest = component.get('manifest', 'Cargo.toml')
        locked = False
        # Include enclosing manifests and locks conservatively. Workspace
        # members use an ancestor lock; a cache key must also retain workspace
        # declarations, without running Cargo before SDK preparation.
        for directory in (root / manifest).parents:
            if not directory.is_relative_to(root):
                break
            for filename in ['Cargo.toml', 'Cargo.lock']:
                path = directory / filename
                if not path.exists() and not path.is_symlink():
                    continue
                name = path.relative_to(root).as_posix()
                path = relative(root, name, exists=True)
                if not path.is_file() or path.stat().st_size > 8 << 20:
                    raise ValueError('compiler cache inputs require bounded native source files')
                inputs['files'][name] = hashlib.sha256(path.read_bytes()).hexdigest()
                locked = locked or filename == 'Cargo.lock'
            path = directory / '.cargo/config.toml'
            if path.exists():
                safe = relative(root, path.relative_to(root).as_posix(), exists=True)
                inputs['files'][safe.relative_to(root).as_posix()] = hashlib.sha256(safe.read_bytes()).hexdigest()
        if not locked:
            raise ValueError('compiler caches require a caller-owned Cargo lockfile')
        destination = component.get('target_directory') or environment.get('CARGO_TARGET_DIR') or str(Path(manifest).parent / 'target')
        path = Path(destination)
        path = path if path.is_absolute() else root / path
        if not path.is_relative_to(root):
            raise ValueError('compiler target caches must stay inside the checkout')
        path = relative(root, path.relative_to(root).as_posix())
        if any(value in str(path) for value in '*?[]{}!'):
            raise ValueError('compiler cache paths may not contain artifact glob syntax')
        if not re.fullmatch(r'[A-Za-z0-9_.-]*target[A-Za-z0-9_.-]*', path.name):
            raise ValueError('compiler caches may contain only dedicated native target directories')
        directories.add(str(path))
    if len(directories) > 32:
        raise ValueError('compiler target cache inventory exceeds its bound')
    cargo = Path(environment.get('CARGO_HOME') or (Path.home() / '.cargo'))
    if (not cargo.is_absolute() or any(ord(value) < 32 for value in str(cargo))
            or any(value in str(cargo) for value in '*?[]{}!')):
        raise ValueError('Cargo download cache must be its actual absolute SDK home')
    for suffix in ['registry/index', 'registry/cache', 'registry/src', 'git/db', 'git/checkouts']:
        path = cargo / suffix
        if any(value.is_symlink() for value in [path, *path.parents]):
            raise ValueError('Cargo cache directories may not be symlinks')
        directories.add(str(path))
    identity = hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    job = environment.get('GITHUB_JOB', 'local')
    if not re.fullmatch('[A-Za-z0-9_-]{1,100}', job):
        raise ValueError('compiler cache variant must identify the actual native job')
    base = VERSION + '-'.join([runner_os, runner_arch, image, identity]) + '-'
    target_prefix = base + (target or 'host') + '-'
    prefix = target_prefix + job + '-'
    return {'enabled': True, 'paths': sorted(directories), 'key': prefix + source,
            'restore_keys': [prefix, target_prefix, base]}


def within_budget(paths):
    total = count = 0
    for name in paths:
        root = Path(name)
        if root.is_symlink():
            return False
        if not root.exists():
            continue
        if not root.is_dir():
            return False
        for directory, children, files in os.walk(root, followlinks=False):
            for name in children + files:
                path = Path(directory) / name
                if path.is_symlink():
                    return False
                if path.is_file():
                    count += 1
                    total += path.stat().st_size
                    if count > MAX_FILES or total > MAX_BYTES:
                        return False
                elif not path.is_dir():
                    return False
    return bool(count)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', default='')
    parser.add_argument('--save', action='store_true')
    args = parser.parse_args()
    root = Path.cwd().resolve()
    try:
        config = load(root)
        pins = tomllib.loads((root / 'mise.toml').read_text())
        if not config.get('components', {}).get('rust'):
            values = {'enabled': False}
        else:
            python = pins.get('tools', {}).get('python')
            if not isinstance(python, str) or not re.fullmatch(r'\d+\.\d+\.\d+', python):
                raise ValueError('compiler cache activation requires the caller-pinned Python SDK')
            data = subprocess.check_output(['mise', 'env', '--json', 'python@' + python], cwd=root,
                env={**os.environ, 'MISE_AUTO_INSTALL': 'false', 'MISE_EXEC_AUTO_INSTALL': 'false'}, timeout=30)
            if len(data) > 1 << 20:
                raise ValueError('SDK environment exceeds its bound')
            environment = {**os.environ, **json.loads(data)}
            source = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True, timeout=30).strip()
            values = plan(root, config, environment, source, os.environ['RUNNER_OS'], os.environ['RUNNER_ARCH'], args.target,
                image=image_identity())
            if args.save and values['enabled']:
                values['enabled'] = within_budget(values['paths'])
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                output.write('enabled=' + str(values['enabled']).lower() + '\n')
                if values['enabled']:
                    output.write('key=' + values['key'] + '\n')
                    output.write('restore-keys<<COMPILER_CACHE_KEYS\n' + '\n'.join(values['restore_keys']) + '\nCOMPILER_CACHE_KEYS\n')
                    output.write('paths<<COMPILER_CACHE_PATHS\n' + '\n'.join(values['paths']) + '\nCOMPILER_CACHE_PATHS\n')
        print('Compiler cache: ' + ('selected native intermediates and Cargo downloads' if values['enabled'] else 'no eligible bounded compiler payload'))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__': main()
