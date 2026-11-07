#!/usr/bin/env python3
"""Build the reviewed 2D template from pinned inputs in a bounded offline container."""
import hashlib
import io
import os
import stat
import tempfile
import tomllib

from configuration import relative, load, read_source_data, _source_config
from recipe_contract import app_directory
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile



def sha256(body):
    return hashlib.sha256(body).hexdigest()


def download(url, expected, maximum):
    with urllib.request.urlopen(url, timeout=60) as response:
        body = response.read(maximum + 1)
    if len(body) > maximum or sha256(body) != expected:
        raise ValueError('Template build input is oversized or its digest differs')
    return body


PROFILE_FIELDS = {'version', 'godot_version', 'source_commit', 'source_sha256', 'emsdk_image',
                  'scons_url', 'scons_sha256', 'modules', 'options'}
OPTION_FIELDS = {'platform', 'target', 'production', 'optimize', 'lto', 'disable_3d',
                 'threads', 'modules_enabled_by_default'}


def profile_data(profile):
    data = read_source_data(profile, maximum=64 * 1024)
    if (set(data) != PROFILE_FIELDS or type(data.get('version')) is not int or data['version'] != 1
            or not isinstance(data.get('godot_version'), str)
            or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', data['godot_version'])
            or not isinstance(data.get('source_commit'), str) or not re.fullmatch('[0-9a-f]{40}', data['source_commit'])
            or any(not isinstance(data.get(name), str) or not re.fullmatch('[0-9a-f]{64}', data[name])
                   for name in ['source_sha256', 'scons_sha256'])
            or not isinstance(data.get('emsdk_image'), str)
            or not re.fullmatch(r'emscripten/emsdk@sha256:[0-9a-f]{64}', data['emsdk_image'])
            or not isinstance(data.get('scons_url'), str) or not data['scons_url'].startswith('https://')
            or not isinstance(data.get('options'), dict) or set(data['options']) != OPTION_FIELDS
            or any(not isinstance(value, str) or not value for value in data['options'].values())
            or not isinstance(data.get('modules'), list) or not data['modules']
            or any(not isinstance(value, str) or not re.fullmatch('[a-z0-9_]+', value) for value in data['modules'])
            or len(data['modules']) != len(set(data['modules']))):
        raise ValueError('custom template requires every reviewed build input and no unknown profile fields')
    return data


def template_receipt(archive, profile):
    checked_regular(archive, 64 * 1024 * 1024)
    checked_regular(profile, 64 * 1024)
    with zipfile.ZipFile(archive) as package:
        expected = {'godot.js', 'godot.wasm', 'godot.audio.worklet.js', 'godot.audio.position.worklet.js', 'godot.html', 'godot.service.worker.js', 'godot.offline.html'}
        if set(package.namelist()) != expected or len(package.infolist()) != len(expected):
            raise ValueError('Custom template contains unexpected or missing engine components')
        components = {}
        for entry in package.infolist():
            if entry.is_dir() or stat.S_IFMT(entry.external_attr >> 16) not in {0, stat.S_IFREG}:
                raise ValueError('custom template engine components must be regular files')
            if entry.file_size <= 0 or entry.file_size > 64 * 1024 * 1024:
                raise ValueError('Template component size outside the reviewed bound')
            body = package.read(entry)
            if entry.filename.endswith('.wasm') and not body.startswith(b'\0asm'):
                raise ValueError('Template WASM magic is missing')
            components[entry.filename] = {'sha256': sha256(body), 'bytes': len(body)}
    return {'version': 1, 'profile_sha256': sha256(profile.read_bytes()),
            'requested_build_inputs': profile_data(profile), 'archive_sha256': sha256(archive.read_bytes()),
            'components': components, 'gameplay_acceptance': 'unverified',
            'scope': 'Archive byte inventory and requested profile; this inspection alone does not establish build provenance, gameplay or performance.'}


def build(profile, output, *, archive_name="godot-web-template.zip", receipt_name="godot-web-template.json"):
    checked_regular(profile, 64 * 1024)
    plain_asset(archive_name, ".zip")
    plain_asset(receipt_name, ".json")
    spec = profile_data(profile)
    if spec['version'] != 1 or not re.fullmatch('[0-9a-f]{40}', spec['source_commit']):
        raise ValueError('Invalid pinned template source')
    if not re.fullmatch(r'emscripten/emsdk@sha256:[0-9a-f]{64}', spec['emsdk_image']):
        raise ValueError('Emsdk must use an immutable image digest')
    options = spec['options']
    if set(options) != {'platform', 'target', 'production', 'optimize', 'lto', 'disable_3d', 'threads', 'modules_enabled_by_default'}:
        raise ValueError('Unexpected compiler option')
    if options.get('platform') != 'web' or options.get('target') != 'template_release' or options.get('threads') != 'no':
        raise ValueError('Only the single-thread web release profile is supported')
    modules = spec['modules']
    if len(modules) != len(set(modules)) or not all(re.fullmatch('[a-z0-9_]+', m) for m in modules):
        raise ValueError('Invalid module profile')
    if output.exists():
        raise ValueError('Use a fresh output directory; ambient cached build inputs are not trusted')
    output.mkdir(parents=True)
    source, scons = output/'godot', output/'scons'
    if not source.exists():
        body = download('https://codeload.github.com/godotengine/godot/tar.gz/'+spec['source_commit'], spec['source_sha256'], 80*1024*1024)
        unpack = output/'unpack'
        unpack.mkdir()
        with tarfile.open(fileobj=io.BytesIO(body), mode='r:gz') as package:
            package.extractall(unpack, filter='data')
        roots = list(unpack.iterdir())
        if len(roots) != 1 or not roots[0].is_dir():
            raise ValueError('Unexpected Godot source archive layout')
        roots[0].rename(source)
        unpack.rmdir()
    if not scons.exists():
        body = download(spec['scons_url'], spec['scons_sha256'], 16*1024*1024)
        with zipfile.ZipFile(io.BytesIO(body)) as package:
            for entry in package.infolist():
                if Path(entry.filename).is_absolute() or '..' in Path(entry.filename).parts:
                    raise ValueError('Unsafe SCons wheel path')
            package.extractall(scons)
    args = ['docker', 'run', '--rm', '--network', 'none', '--memory', '10g', '--cpus', '4',
            '-e', 'PYTHONPATH=/scons', '-v', str(scons.resolve())+':/scons:ro',
            '-v', str(source.resolve())+':/src', '-w', '/src', spec['emsdk_image'],
            'python3', '-m', 'SCons', '-j4']
    args += [key+'='+value for key, value in options.items()]
    args += ['module_'+module+'_enabled=yes' for module in modules]
    subprocess.run(args, check=True, timeout=3600)
    artifact = source/'bin/godot.web.template_release.wasm32.nothreads.zip'
    receipt = template_receipt(artifact, profile)
    receipt['build_inputs'] = receipt.pop('requested_build_inputs')
    receipt['scope'] = 'Completed offline build from verified fresh source and pinned compiler inputs; gameplay and performance are independently verified.'
    destination = output/archive_name
    shutil.copyfile(artifact, destination)
    (output/receipt_name).write_text(json.dumps(receipt, indent=2)+'\n')
    print(destination)



SDK_KEYS = ['REPOSITORY', 'TAG', 'ARCHIVE', 'RECEIPT', 'ARCHIVE_SHA256', 'RECEIPT_SHA256', 'PROFILE_SHA256']


def plain_asset(value, suffix):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}' + re.escape(suffix), value):
        raise ValueError('custom template assets require plain reviewed filenames')
    return value


def checked_regular(path, maximum):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= maximum:
        raise ValueError('template input is linked, missing, empty or oversized')
    return path


def checked_bytes(path, expected, maximum):
    body = checked_regular(path, maximum).read_bytes()
    if sha256(body) != expected:
        raise ValueError('template artifact differs from its reviewed digest')
    return body


def declaration(root, config):
    values = config.get('components', {}).get('godot', {}).get('templates')
    if values is None:
        return None
    if not isinstance(values, dict) or set(values) != {'profile', 'directory'}:
        raise ValueError('custom templates require native profile and cache paths only')
    root = Path(root).resolve()
    profile = relative(root, values['profile'], exists=True)
    checked_regular(profile, 64 * 1024)
    directory = relative(root, values['directory'])
    project = relative(root, app_directory(config), exists=True)
    if directory == project or not directory.is_relative_to(project):
        raise ValueError('custom template cache must remain in the ignored native project')
    pins = tomllib.loads((root / 'mise.toml').read_text()).get('env', {})
    selected = {}
    for name in SDK_KEYS:
        value = pins.get('GODOT_TEMPLATE_' + name)
        if not isinstance(value, str) or not value:
            raise ValueError('declare all exact custom template SDK pins in caller Mise environment')
        selected[name.lower()] = value
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', selected['repository']):
        raise ValueError('custom template SDK requires its reviewed native release repository')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,159}', selected['tag']) or '..' in selected['tag']:
        raise ValueError('custom template SDK requires one exact reviewed release tag')
    plain_asset(selected['archive'], '.zip')
    plain_asset(selected['receipt'], '.json')
    for name in ['archive_sha256', 'receipt_sha256', 'profile_sha256']:
        if not re.fullmatch('[a-f0-9]{64}', selected[name]):
            raise ValueError('custom template SDK requires exact reviewed SHA256 pins')
    if sha256(profile.read_bytes()) != selected['profile_sha256']:
        raise ValueError('custom template provenance profile differs from its exact reviewed bytes')
    requested = profile_data(profile)
    if (not isinstance(requested, dict) or requested.get('version') != 1
            or requested.get('godot_version') != pins.get('GODOT_VERSION')):
        raise ValueError('custom template provenance must match the active pinned Godot SDK')
    return {'profile': profile, 'directory': directory, 'pin': selected}


def verify_directory(directory, pin, profile):
    plain_asset(pin['archive'], '.zip')
    plain_asset(pin['receipt'], '.json')
    directory = Path(directory).absolute()
    if directory.resolve() != directory:
        raise ValueError('template cache must not traverse links')
    archive = directory / pin['archive']
    checked_bytes(archive, pin['archive_sha256'], 64 * 1024 * 1024)
    receipt = json.loads(checked_bytes(directory / pin['receipt'], pin['receipt_sha256'], 64 * 1024))
    observed = template_receipt(archive, profile)
    if (not isinstance(receipt, dict) or receipt.get('version') != 1
            or receipt.get('archive_sha256') != pin['archive_sha256']
            or not isinstance(receipt.get('profile_sha256'), str)
            or not re.fullmatch('[a-f0-9]{64}', receipt['profile_sha256'])
            or observed['profile_sha256'] != pin['profile_sha256']
            or not _source_config().equal(receipt.get('build_inputs'), observed['requested_build_inputs'])
            or receipt.get('components') != observed['components']):
        raise ValueError('template bytes, completed build receipt and requested profile disagree')
    return archive


def install(directory, pin, profile, *, offline=False, download=None):
    plain_asset(pin['archive'], '.zip')
    plain_asset(pin['receipt'], '.json')
    directory = Path(directory).absolute()
    if directory.resolve() != directory:
        raise ValueError('template cache must not traverse links')
    if directory.exists():
        try:
            return verify_directory(directory, pin, profile)
        except ValueError:
            if offline:
                raise
    elif offline:
        raise ValueError('cache the exact reviewed custom template before offline export')
    directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='godot-template-', dir=directory.parent) as temporary:
        staging = Path(temporary)
        if download:
            download(staging)
        else:
            subprocess.run(['gh', 'release', 'download', pin['tag'], '--repo', pin['repository'],
                '--pattern', pin['archive'], '--pattern', pin['receipt'], '--dir', str(staging)], check=True, timeout=120)
        verify_directory(staging, pin, profile)
        directory.mkdir(exist_ok=True)
        (staging / '.gdignore').write_text('')
        (staging / '.gdignore').replace(directory / '.gdignore')
        for name in [pin['archive'], pin['receipt']]:
            (staging / name).replace(directory / name)
    return verify_directory(directory, pin, profile)


def selected_environment(pin, environment):
    for name in SDK_KEYS:
        key = 'GODOT_TEMPLATE_' + name
        if key in environment and environment[key] != pin[name.lower()]:
            raise ValueError('custom template environment differs from caller Mise SDK authority')


def setup(root, config, environment):
    values = declaration(root, config)
    if values is None:
        return None
    selected_environment(values['pin'], environment)
    return install(values['directory'], values['pin'], values['profile'])


def verify(root, config, environment):
    values = declaration(root, config)
    if values is None:
        return None
    selected_environment(values['pin'], environment)
    return install(values['directory'], values['pin'], values['profile'], offline=True)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['setup', 'verify', 'inspect', 'build'])
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--output', type=Path)
    parser.add_argument('--archive', type=Path)
    args = parser.parse_args()
    try:
        root = args.root.resolve()
        config = load(root)
        values = declaration(root, config)
        if values is None:
            raise ValueError('this native Godot application does not select a custom template')
        if args.operation in {'setup', 'verify'}:
            print(globals()[args.operation](root, config, dict(os.environ)))
        elif args.operation == 'inspect':
            if args.archive is None:
                raise ValueError('template inspection requires one exact archive')
            print(json.dumps(template_receipt(args.archive, values['profile']), indent=2))
        else:
            if args.output is None:
                raise ValueError('reviewed template compilation requires one fresh explicit output directory')
            build(values['profile'], args.output, archive_name=values['pin']['archive'], receipt_name=values['pin']['receipt'])
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__': main()
