"""Named native Cargo test selections with explicit partial-coverage identities."""
from pathlib import Path
import re

import native_components
import rust_component
from configuration import relative


def records(root, config):
    declarations = config.get('components', {}).get('rust_tests', [])
    if not isinstance(declarations, list) or len(declarations) > 128:
        raise ValueError('native Cargo test declarations must be bounded')
    names = set()
    for value in declarations:
        if not isinstance(value, dict) or set(value) - {'name', 'component', 'manifest', 'selectors', 'roles', 'platforms', 'suite', 'dependency_context', 'reuse_native_build'}:
            raise ValueError('unsupported native Cargo test declaration')
        name = value.get('name')
        if not isinstance(name, str) or not re.fullmatch('[a-z][a-z0-9-]{0,63}', name) or name in names:
            raise ValueError('native Cargo test selections require unique source names')
        names.add(name)
        if value.get('suite', 'unit') not in {'unit', 'build'}:
            raise ValueError('Cargo selector checks must declare unit or build validation coverage')
        if value.get('dependency_context', 'role') not in {'none', 'role'}:
            raise ValueError('native Cargo checks must select their explicit dependency variant')
        if type(value.get('reuse_native_build', False)) is not bool or (value.get('reuse_native_build') and value.get('suite') != 'build'):
            raise ValueError('only compiler checks may reuse joined native build acceptance')
        if not isinstance(value.get('selectors', {}), dict):
            raise ValueError('Cargo test selectors must be native data')
        for field in ['roles', 'platforms']:
            if not isinstance(value.get(field, []), list) or any(not isinstance(item, str) for item in value.get(field, [])):
                raise ValueError('native Cargo test applicability must be literal source lists')
        selected = component(root, config, value)
        rust_component.validate(selected, root)
    return declarations


def component(root, config, value):
    candidates = config.get('components', {}).get('rust', [])
    if value.get('component'):
        candidates = [item for item in candidates if item.get('name') == value['component']]
    else:
        manifest = value.get('manifest', 'Cargo.toml')
        if root is not None:
            relative(root, manifest, exists=True)
        candidates = [item for item in candidates if item.get('manifest', 'Cargo.toml') == manifest]
    if len(candidates) != 1:
        raise ValueError('native Cargo test must select exactly one native component')
    return candidates[0] | {'test': {**candidates[0].get('test', {}), **value.get('selectors', {})}}


def eligible(root, config, environment, platform, suite='unit'):
    graph = config.get('components', {}).get('native')
    roles = {value['name'] for value in native_components.selection(config, environment)} if graph else set()
    declarations = records(root, config)
    covered = []
    if environment.get('COOKBOOK_NATIVE_BUILD_CHECKS'):
        import json
        value = environment['COOKBOOK_NATIVE_BUILD_CHECKS']
        if len(value) > 65536:
            raise ValueError('joined compiler check selection exceeds its bound')
        covered = json.loads(value)
        known = {row['name'] for row in declarations if row.get('suite') == 'build'}
        if not isinstance(covered, list) or any(not isinstance(name, str) for name in covered) or len(covered) != len(set(covered)) or set(covered)-known:
            raise ValueError('joined compiler checks differ from the native source declaration')
    return [value for value in declarations if value.get('suite', 'unit') == suite
            and not (value.get('reuse_native_build') and value['name'] in covered)
            and (not value.get('roles') or bool(set(value['roles']) & roles))
            and (not value.get('platforms') or platform in value['platforms'])]


def run_selected(root, config, environment, target_record, names, run, suite='unit'):
    platform = native_components.platform(target_record)
    allowed = {value['name']: value for value in eligible(root, config, environment, platform, suite)}
    if not names or len(names) != len(set(names)) or set(names) - set(allowed):
        raise ValueError('select only applicable source-declared native Cargo checks')
    for name in names:
        value = allowed[name]
        values = dict(environment)
        if config.get('components', {}).get('native') and value.get('roles') and value.get('dependency_context', 'role') == 'role':
            graph = [item for item in native_components.selection(config, environment) if item['name'] in value['roles']]
            if not graph:
                raise ValueError('native Cargo check does not belong to the selected role wave')
            _hashes, values = native_components.dependencies(root, config, graph[-1], platform, environment)
        rust_component.test(root, [component(root, config, value)], values, run, target=environment.get('RUST_TARGET'))
