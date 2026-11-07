"""Typed native API/host/CLI dependency graphs for Rust cookbook builds."""
import importlib.util
import json
from pathlib import Path
import re
import shutil

from configuration import relative
import rust_component

import dependencies as sdk_dependencies

COOKBOOK = Path(__file__).resolve().parents[1]
TOOLS = sdk_dependencies.tools_root()


def payload_helper():
    return sdk_dependencies.helper('native_payload')


def records(config):
    values = config.get('components', {}).get('native', [])
    if not isinstance(values, list) or len(values) > 16:
        raise ValueError('native component graph must be a bounded list')
    names = set()
    cargo_names = {value.get('name') for value in config['components']['rust']}
    for value in values:
        if not isinstance(value, dict) or set(value) - {'name', 'role', 'component', 'binary', 'output', 'environment', 'needs', 'checks'}:
            raise ValueError('unsupported native component declaration')
        name = value.get('name')
        if not isinstance(name, str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}', name) or name in names:
            raise ValueError('native component names must be unique identifiers')
        names.add(name)
        checks = value.get('checks', [])
        if not isinstance(checks, list) or len(checks) > 128 or any(not isinstance(path, str) or 'tests' not in Path(path).parts or '..' in Path(path).parts for path in checks) or len(checks) != len(set(checks)):
            raise ValueError('native role checks must be declared test source paths')
        if value.get('role') not in {'api', 'host', 'cli'} or value.get('component') not in cargo_names:
            raise ValueError('native roles must select a declared Cargo component')
        if not isinstance(value.get('binary'), str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}', value['binary']):
            raise ValueError('native component must name its actual executable')
        if not isinstance(value.get('output'), str):
            raise ValueError('native component must declare an output path')
        environment = value.get('environment', {})
        if not isinstance(environment, dict) or any(not re.fullmatch('[A-Z][A-Z0-9_]*', key) or item not in {'{commit}', '{version}', '{date}'} for key, item in environment.items()):
            raise ValueError('native environment only binds named build identity fields')
        needs = value.get('needs', [])
        if not isinstance(needs, list) or len(needs) > 16:
            raise ValueError('native dependencies must be bounded source declarations')
        for dependency in needs:
            if not isinstance(dependency, dict) or set(dependency) - {'component', 'platform', 'path_environment', 'digest_environment', 'compression', 'output'}:
                raise ValueError('unsupported native dependency declaration')
            for key in ['path_environment', 'digest_environment']:
                item = dependency.get(key)
                if item is not None and (not isinstance(item, str) or not re.fullmatch('[A-Z][A-Z0-9_]*', item)):
                    raise ValueError('native dependencies require literal environment names')
            if not dependency.get('path_environment'):
                raise ValueError('native dependency must bind its verified payload path')
            if dependency.get('platform', 'native') not in {'native', *payload_helper().PLATFORMS}:
                raise ValueError('native dependency must select a declared platform')
            if dependency.get('compression') not in {None, 'gzip'}:
                raise ValueError('native payload compression must be deterministic gzip')
            if bool(dependency.get('compression')) != bool(dependency.get('output')):
                raise ValueError('compressed dependencies require one generated output')
    for value in values:
        if any(item.get('component') not in names for item in value.get('needs', [])):
            raise ValueError('native dependency references an unknown component')
    # Reject cycles before a build starts, including cycles in unselected roles.
    def visit(name, ancestors):
        if name in ancestors:
            raise ValueError('native component dependency graph contains a cycle')
        for item in next(value for value in values if value['name'] == name).get('needs', []):
            visit(item['component'], {*ancestors, name})
    for name in names:
        visit(name, set())
    return values


def selection(config, environment):
    values = records(config)
    raw = environment.get('COOKBOOK_NATIVE_COMPONENTS')
    if raw is None:
        return values
    if not isinstance(raw, str) or len(raw) > 65536:
        raise ValueError('native component selection must be bounded JSON')
    selected = json.loads(raw)
    if not isinstance(selected, list) or not selected or any(not isinstance(name, str) for name in selected) or len(selected) != len(set(selected)) or set(selected) - {value['name'] for value in values}:
        raise ValueError('select only source-declared native component names')
    return [value for value in values if value['name'] in selected]


def output(root, declaration, platform):
    system, arch = platform.split('/')
    template = declaration['output']
    name = template.replace('{platform}', system).replace('{arch}', arch)
    if '{' in name or '}' in name:
        raise ValueError('native paths accept only platform and arch tokens')
    return relative(root, name)


def platform(record):
    return record['platform'] + '/' + record['arch']


def dependencies(root, config, declaration, native_platform, environment, *, materialize=False, ancestors=(), check_compressed=True):
    if declaration['name'] in ancestors:
        raise ValueError('native dependency cycle')
    ancestors = (*ancestors, declaration['name'])
    values = {value['name']: value for value in records(config)}
    payload = payload_helper()
    hashes = {}
    result = dict(environment)
    tokens = {'{commit}': environment['COMMIT'], '{version}': environment['VERSION'], '{date}': environment['DATE']}
    for name, token in declaration.get('environment', {}).items():
        result[name] = tokens[token]
    for need in declaration.get('needs', []):
        source = values[need['component']]
        target_platform = need.get('platform', 'native')
        if target_platform == 'native':
            target_platform = native_platform
        nested_hashes, _ = dependencies(root, config, source, target_platform, environment, ancestors=ancestors, check_compressed=False)
        binary = output(root, source, target_platform)
        receipt = payload.verify(binary, environment['COMMIT'], environment['VERSION'], source['role'], target_platform, nested_hashes)
        logical = source['name'] + '-' + target_platform.replace('/', '_')
        if logical in hashes:
            raise ValueError('duplicate native dependency identity')
        hashes[logical] = receipt['sha256']
        path = binary
        if need.get('compression'):
            path = output(root, need, native_platform)
            if materialize:
                payload.compress(binary, path, environment['COMMIT'], environment['VERSION'], source['role'], target_platform, nested_hashes)
            elif check_compressed:
                # Test/validation consumers cannot silently regenerate build files.
                import gzip
                if payload.bytes_of(path) != gzip.compress(payload.bytes_of(binary), mtime=0):
                    raise ValueError('compressed native payload differs from its verified input; run mise run build')
        result[need['path_environment']] = str(path)
        if need.get('digest_environment'):
            result[need['digest_environment']] = receipt['sha256']
    return hashes, result


def artifacts(root, config, target_record, environment):
    result = []
    for value in selection(config, environment):
        binary = output(root, value, platform(target_record))
        result += [binary.relative_to(root).as_posix(), payload_helper().receipt_path(binary).relative_to(root).as_posix()]
    return list(dict.fromkeys(result))


def build(root, config, target_record, environment, run):
    payload = payload_helper()
    contexts = []
    for value in selection(config, environment):
        hashes, values = dependencies(root, config, value, platform(target_record), environment, materialize=True)
        matches = [component for component in config['components']['rust'] if component.get('name') == value['component']]
        if len(matches) != 1:
            raise ValueError('native role must select exactly one Cargo component')
        component = matches[0] | {'binaries': [value['binary']], 'build': {**matches[0].get('build', {}), 'binaries': [value['binary']]}}
        rust_component.build(root, [component], values, run, target=target_record['target'])
        target = Path(component.get('target_directory', environment.get('CARGO_TARGET_DIR', 'target')))
        if not target.is_absolute():
            target = root / target
        profile = component.get('build', {}).get('profile', component.get('profile', 'release'))
        binary = target / target_record['target'] / ('debug' if profile in {'debug', 'dev'} else 'release') / (value['binary'] + ('.exe' if target_record['platform'] == 'Windows' else ''))
        destination = output(root, value, platform(target_record))
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(binary, destination)
        destination.chmod(0o755)
        payload.record(destination, environment['COMMIT'], environment['VERSION'], value['role'], platform(target_record), hashes)
        contexts.append((component, values))
    return contexts


def verify(root, config, target_record, environment):
    payload = payload_helper()
    for value in selection(config, environment):
        hashes, _ = dependencies(root, config, value, platform(target_record), environment)
        payload.verify(output(root, value, platform(target_record)), environment['COMMIT'], environment['VERSION'], value['role'], platform(target_record), hashes)
