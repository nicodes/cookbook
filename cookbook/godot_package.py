"""Native authoring components and closed Godot serving-root packaging."""
import importlib.util
import hashlib
import json
import re
from pathlib import Path
import shutil
import sys

from configuration import relative
from recipe_contract import app_directory

import dependencies

COOKBOOK = Path(__file__).resolve().parents[1]
TOOLS = dependencies.tools_root()


def helper(name):
    return dependencies.recipe_helper(name)


def settings(config):
    values = config.get('components', {}).get('godot', {})
    if not isinstance(values, dict):
        raise ValueError('native Godot components require a metadata table')
    return values


def entry(root, record):
    if not isinstance(record, dict) or set(record) - {'runner', 'path', 'args', 'timeout'}:
        raise ValueError('native authoring components require bounded literal entrypoints')
    source = relative(root, record.get('path'), exists=True)
    if (not source.is_file() or source.relative_to(root).parts[0] not in {'app', 'core', 'api', 'internal'}
            or any(part in {'tests', 'test'} for part in source.relative_to(root).parts)):
        raise ValueError('product authoring entrypoints belong to owned native components')
    kind = record.get('runner')
    suffixes = {'python': {'.py'}, 'node': {'.js', '.mjs', '.cjs'}, 'bun': {'.js', '.mjs', '.ts'}, 'godot': {'.gd'}}
    if kind not in suffixes or source.suffix not in suffixes[kind]:
        raise ValueError('native authoring runner must match its owned source language')
    arguments = record.get('args', [])
    if not isinstance(arguments, list) or any(not isinstance(value, str) or '\0' in value for value in arguments):
        raise ValueError('native authoring arguments must be literal strings')
    timeout = record.get('timeout', 180)
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise ValueError('native authoring deadlines must stay within the reviewed native limit')
    return source, arguments, timeout


def validate(root, config, artifacts):
    values = settings(config)
    for name in ['prepare', 'postprocess', 'packs', 'web_releases', 'extensions']:
        if not isinstance(values.get(name, []), list) or len(values.get(name, [])) > 128:
            raise ValueError('native build declarations must be bounded component lists')
    for phase in ['prepare', 'postprocess']:
        for record in values.get(phase, []):
            entry(root, record)
    outputs = set()
    for record in values.get('packs', []):
        if not isinstance(record, dict) or set(record) - {'program', 'args', 'output', 'manifest', 'timeout'}:
            raise ValueError('native resource packs require declared source and outputs')
        entry(root, {'runner': 'godot', 'path': record.get('program'), 'args': record.get('args', []), 'timeout': record.get('timeout', 180)})
        output = relative(root, record['output'])
        if output.suffix != '.pck' or output in outputs:
            raise ValueError('native pack outputs must be distinct PCK files')
        outputs.add(output)
        if not any(output.is_relative_to(relative(root, name)) for name in artifacts):
            raise ValueError('native pack must belong to the verified build artifact scope')
        helper('clean').planned(root, [record['output']])
        if record.get('manifest'):
            manifest = relative(root, record['manifest'])
            if not any(manifest.is_relative_to(relative(root, name)) for name in artifacts):
                raise ValueError('native pack metadata must belong to the verified build artifact scope')
            helper('clean').planned(root, [record['manifest']])
    for record in values.get('extensions', []):
        if (not isinstance(record, dict) or set(record) - {'source', 'destination', 'format', 'record', 'target', 'profile'}
                or not {'source', 'destination', 'format'} <= set(record)
                or record['format'] not in {'elf', 'wasm'}):
            raise ValueError('native extension copies require declared compiled inputs and formats')
        source, destination = relative(root, record['source']), relative(root, record['destination'])
        if source == destination or destination in outputs:
            raise ValueError('native extension destinations must be distinct from compiled inputs')
        outputs.add(destination)
        paths = [record['source'], record['destination'], *([record['record']] if record.get('record') else [])]
        for name in paths:
            output = relative(root, name)
            if not any(output.is_relative_to(relative(root, path)) for path in artifacts):
                raise ValueError('native extension bytes and evidence must belong to the verified artifact scope')
        helper('clean').planned(root, [record['destination'], *([record['record']] if record.get('record') else [])])
    for record in values.get('web_releases', []):
        allowed = {'source', 'output', 'max_wasm_bytes', 'max_transfer_bytes', 'brotli', 'relative_urls',
                   'versioned_only', 'root_files', 'root_json', 'policy', 'revision_metadata'}
        if not isinstance(record, dict) or set(record) - allowed:
            raise ValueError('unknown native Web packaging declaration')
        source, output = relative(root, record['source']), relative(root, record['output'])
        native_exports = [relative(root, export['output']).parent for export in values.get('exports', [])
                          if export.get('platform', 'web') == 'web']
        if source not in native_exports:
            raise ValueError('serving input must name a declared native Web export')
        if output == source or output.is_relative_to(source) or source.is_relative_to(output) or output in outputs:
            raise ValueError('raw native exports and serving outputs must be distinct trees')
        outputs.add(output)
        if not any(output.is_relative_to(relative(root, name)) for name in artifacts):
            raise ValueError('serving output must belong to the verified build artifact scope')
        for name in ['max_wasm_bytes', 'max_transfer_bytes']:
            if type(record.get(name, 0)) is not int or record.get(name, 0) < 0:
                raise ValueError('native Web budgets must be nonnegative integers')
        for name in ['brotli', 'relative_urls', 'versioned_only']:
            if type(record.get(name, False)) is not bool:
                raise ValueError('native Web switches must be literal booleans')
        if type(record.get('root_json', False)) is not bool:
            raise ValueError('mutable root JSON selection must be an explicit native switch')
        if not isinstance(record.get('root_files', []), list):
            raise ValueError('root payload metadata must be a literal filename list')
        helper('clean').planned(root, [record['output']])
        for name in record.get('root_files', []):
            relative(output, name)
        if record.get('revision_metadata'):
            relative(output, record['revision_metadata'])
    for export in values.get('exports', []):
        policy = export.get('runtime_compatibility')
        if policy is not None and (export.get('platform', 'web') != 'web' or not isinstance(policy, dict) or set(policy) - {'release_root'}):
            raise ValueError('runtime compatibility accepts only a caller-owned release-root identifier')
        if policy is not None and 'release_root' in policy:
            value = policy['release_root']
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z_$][A-Za-z0-9_$]{0,79}', value):
                raise ValueError('runtime release-root identifier must be literal JavaScript')


def author(root, config, environment, run, phase):
    directory = relative(root, app_directory(config), exists=True)
    for record in settings(config).get(phase, []):
        source, arguments, timeout = entry(root, record)
        kind = record['runner']
        if kind == 'godot':
            command = [environment['GODOT_BIN'], '--headless', '--path', str(directory), '--script', str(source), '--', *arguments]
            status = helper('godot-game-runner').main(command, timeout=timeout, environment=environment, cwd=root)
            if status:
                raise ValueError('native authoring component failed: ' + record['path'])
        else:
            executable = sys.executable if kind == 'python' else kind
            run([executable, str(source), *arguments], root, environment, timeout=timeout)


def packs(root, config, environment):
    directory = relative(root, app_directory(config), exists=True)
    for record in settings(config).get('packs', []):
        source = relative(root, record['program'], exists=True)
        output = relative(root, record['output'])
        helper('clean').clean(root, [record['output']])
        output.parent.mkdir(parents=True, exist_ok=True)
        command = [environment['GODOT_BIN'], '--headless', '--path', str(directory), '--script', str(source), '--', *record.get('args', [])]
        if helper('godot-game-runner').main(command, timeout=record.get('timeout', 180), environment=environment, cwd=root):
            raise ValueError('native resource pack component failed: ' + record['program'])
        if not output.is_file() or output.is_symlink() or not output.stat().st_size:
            raise ValueError('native pack output is missing')
        if not helper('godot_web_release').pack_paths(output):
            raise ValueError('native pack must contain real resource entries')
        if record.get('manifest'):
            import hashlib
            manifest = relative(root, record['manifest'])
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(json.dumps({'file': output.name, 'bytes': output.stat().st_size,
                'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}) + '\n')


def extensions(root, config, environment):
    for record in settings(config).get('extensions', []):
        source = relative(root, record['source'], exists=True)
        if not source.is_file() or source.is_symlink():
            raise ValueError('native extension input must be a regular compiled file')
        data = source.read_bytes()
        if record['format'] == 'elf':
            valid = len(data) >= 20 and data[:5] == b'\x7fELF\x02' and int.from_bytes(data[18:20], 'little') == 62
        else:
            valid = data.startswith(b'\0asm\x01\0\0\0')
        if not valid:
            raise ValueError('native extension has the wrong compiled binary format')
        destination = relative(root, record['destination'])
        helper('clean').clean(root, [record['destination']])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        if destination.read_bytes() != data:
            raise ValueError('installed native extension differs from the compiled bytes')
        if record.get('record'):
            evidence = relative(root, record['record'])
            evidence.parent.mkdir(parents=True, exist_ok=True)
            evidence.write_text(json.dumps({'schema': 1, 'source_commit': environment['SOURCE_COMMIT'],
                'source': record['source'], 'destination': record['destination'],
                'format': record['format'], 'target': record.get('target'), 'profile': record.get('profile'),
                'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}, sort_keys=True) + '\n')


def runtime(root, config):
    for export in settings(config).get('exports', []):
        if 'runtime_compatibility' in export:
            helper('godot-web-runtime').prepare(relative(root, export['output']).with_suffix('.js'),
                export['runtime_compatibility'].get('release_root'))


def package(root, config, environment):
    for record in settings(config).get('web_releases', []):
        source, output = relative(root, record['source'], exists=True), relative(root, record['output'])
        if not source.is_dir() or any(path.is_symlink() for path in source.rglob('*')):
            raise ValueError('native Web export must be an unlinked source tree')
        helper('clean').clean(root, [record['output']])
        shutil.copytree(source, output)
        helper('godot_web_release').prepare(output, source_commit=environment['SOURCE_COMMIT'],
            max_wasm_bytes=record.get('max_wasm_bytes', 0), max_transfer_bytes=record.get('max_transfer_bytes', 0),
            use_brotli=record.get('brotli', False), relative_urls=record.get('relative_urls', False),
            versioned_only=record.get('versioned_only', False), root_files=[*record.get('root_files', []), *([path.name for path in output.glob('*.json') if path.name != 'release-manifest.json'] if record.get('root_json') else [])],
            policy=record.get('policy'))
        if record.get('revision_metadata'):
            destination = relative(output, record['revision_metadata'])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps({'commit': environment['SOURCE_COMMIT']}) + '\n')
        helper('godot_web_release').validate_hosting(output, record.get('policy') or {})
