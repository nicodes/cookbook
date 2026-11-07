"""Read bounded recipe data; product code and test assertions remain caller-owned."""
from pathlib import Path
import importlib.util
import re
import sys
import tomllib

import dependencies
import recipe_schema

if (not isinstance(getattr(recipe_schema, '__file__', None), str)
        or Path(recipe_schema.__file__).resolve() != Path(__file__).with_name('recipe_schema.py').resolve()):
    raise ValueError('recipe schema module origin differs from its selected SDK')

from recipe_schema import (RECIPES, SUITES, RUNNERS, TEST_FIELDS, TEST_COMMON_FIELDS,
                           MAX_TEST_CASES, expand_tests, validate_data)

# Readers consume effective authority through the caller-pinned SDK without
# inserting retired declaration keys into the source data.
release_definition = recipe_schema.recipe_contract.release_definition


def _source_config():
    expected = dependencies.tools_root() / 'helpers/source_config.py'
    module = sys.modules.get('source_config')
    if module is None:
        spec = importlib.util.spec_from_file_location('source_config', expected)
        module = importlib.util.module_from_spec(spec)
        sys.modules['source_config'] = module
        spec.loader.exec_module(module)
    if (not isinstance(getattr(module, '__file__', None), str)
            or Path(module.__file__).resolve() != expected):
        raise ValueError('source decoder origin differs from its selected SDK')
    return module


def read_source_data(path, *, maximum=8 << 20):
    return _source_config().read(path, maximum=maximum)


def parse_source_data(raw, *, maximum=8 << 20):
    return _source_config().loads(raw, maximum=maximum)


def runtime_configuration():
    expected = dependencies.tools_root() / 'helpers/runtime_config.py'
    module = sys.modules.get('runtime_config')
    if module is None:
        spec = importlib.util.spec_from_file_location('runtime_config', expected)
        module = importlib.util.module_from_spec(spec)
        sys.modules['runtime_config'] = module
        spec.loader.exec_module(module)
    if (not isinstance(getattr(module, '__file__', None), str)
            or Path(module.__file__).resolve() != expected):
        raise ValueError('native source compiler origin differs from its selected SDK')
    return module


def ci_settings(data):
    values = recipe_schema.validate_ci_data(data)
    if 'dependency_security' in data.get('ci', {}):
        import dependency_security
        dependency_security.declaration(data)
    return values


def relative(root, name, *, exists=False):
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name
            or any(ord(character) < 32 for character in name)):
        raise ValueError('recipe paths must be nonempty relative paths')
    path = Path(name)
    if path.is_absolute() or '..' in path.parts or '.git' in path.parts:
        raise ValueError('recipe paths must stay inside the checkout')
    target = root / path
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('recipe path escapes the checkout')
    if any(p.is_symlink() for p in [target, *target.parents] if p != root and p.is_relative_to(root)):
        raise ValueError('recipe input/output paths may not be symlinks')
    if exists and not target.exists():
        raise ValueError('recipe input is missing: ' + name)
    return target


def load(root):
    root = Path(root).resolve()
    path = root / 'cookbook.toml'
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 262144:
        raise ValueError('recipe configuration must be a bounded regular source file')
    data = validate_data(tomllib.loads(path.read_text(encoding='utf-8')))
    ci_settings(data)
    for entries in data.get('tests', {}).values():
        for entry in entries:
            if 'python_environment' in entry:
                relative(root, entry['python_environment'])
            program = relative(root, entry.get('path'), exists=True)
            discovery = entry['runner'] in {'godot-suite', 'python-unittest', 'node-test', 'bun-test'}
            if not (program.is_dir() if entry['runner'] == 'godot-suite' else
                    (program.is_file() or program.is_dir()) if discovery else program.is_file()):
                raise ValueError('test entry must name a source file')
            colocated = (entry['runner'] in {'node-test','bun-test'} and program.is_file()
                and re.fullmatch(r'.+\.(?:test|spec)\.(?:[cm]?js|jsx|ts|tsx)',program.name))
            if not colocated and not any(part in {'tests', 'test'} for part in Path(entry['path']).parts):
                raise ValueError('product test entry points belong with their tests')
            if 'cwd' in entry and not relative(root, entry['cwd'], exists=True).is_dir():
                raise ValueError('native test working directory must exist inside the checkout')
    for name in data.get('clean', {}).get('paths', []):
        relative(root, name)
    if 'operations' in data:
        import operational_workflows
        operational_workflows.declaration(root, data)
    return data


def godot_pins(root):
    config = tomllib.loads((Path(root) / 'mise.toml').read_text())
    values = config.get('env', {})
    names = ['GODOT_VERSION', 'GODOT_EXPECTED_VERSION', 'GODOT_BINARY_CHECKSUM', 'GODOT_TEMPLATES_CHECKSUM']
    pins = {name: values.get(name) for name in names}
    if not isinstance(pins['GODOT_VERSION'], str) or not re.fullmatch(r'\d+\.\d+\.\d+', pins['GODOT_VERSION']):
        raise ValueError('Godot requires an exact version in caller Mise pins')
    if not isinstance(pins['GODOT_EXPECTED_VERSION'], str) or not pins['GODOT_EXPECTED_VERSION'].startswith(pins['GODOT_VERSION'] + '.'):
        raise ValueError('Godot binary identity must match its pinned version')
    for name in ['GODOT_BINARY_CHECKSUM', 'GODOT_TEMPLATES_CHECKSUM']:
        if not isinstance(pins[name], str) or not re.fullmatch(r'(?:sha256:[a-f0-9]{64}|sha512:[a-f0-9]{128})', pins[name]):
            raise ValueError('Godot downloads require reviewed exact checksums')
    return pins
