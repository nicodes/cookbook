"""Canonical recipe locations; reusable low-level tools keep their own interfaces."""
from pathlib import PurePosixPath

APP_DIRECTORY = 'app'
APPLICATION_PATH = 'deploy/application.toml'
RELEASE_DEFINITION = 'deploy/release.toml'
ADDON_DIRECTORY = 'addon'
ADDON_MANIFEST = 'package.files'
CLI_OUTPUT_DIRECTORY = '.artifacts/build'
UI_OUTPUT = '.artifacts/ui'
RUST_WEB_DIRECTORY = '.artifacts/toolchains/rust-web'

RECIPES = {'rust-cli', 'react-native', 'godot-game', 'godot-addon'}
RUST_ROOTS = {
    'rust-cli': {'crates'},
    'react-native': {'api', 'internal'},
    'godot-game': {'app', 'api', 'core', 'deploy', 'tests'},
    'godot-addon': set(),
}
NODE_ROOTS = {
    'rust-cli': {'app', 'tests'},
    'react-native': {'app', 'internal', 'tests'},
    'godot-game': {'app', 'tests'},
    'godot-addon': {'tests'},
}


def app_directory(_config):
    return APP_DIRECTORY


def application_path(_config):
    return APPLICATION_PATH


def release_definition(_config):
    return RELEASE_DEFINITION


def _path(value, label):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise ValueError(label + ' requires a canonical repository-relative path')
    path = PurePosixPath(value)
    if (path.is_absolute() or '..' in path.parts or '.git' in path.parts
            or path.as_posix() != value or any(ord(c) < 32 for c in value)):
        raise ValueError(label + ' requires a canonical repository-relative path')
    return path


def _manifest(family, value):
    path = _path(value, 'Cargo manifest')
    root_manifest = value == 'Cargo.toml' and family in {'rust-cli', 'godot-game'}
    if path.name != 'Cargo.toml' or not (root_manifest or (
            len(path.parts) >= 2 and path.parts[0] in RUST_ROOTS[family])):
        raise ValueError('Cargo manifests must use the recipe component directories')


def _staging(value, label):
    path = _path(value, label)
    if not path.is_relative_to('.artifacts') or len(path.parts) < 2:
        raise ValueError(label + ' must remain in owned .artifacts staging')


def validate_paths(data):
    """Inspect literal declarations without accessing or executing product source."""
    family = data.get('recipe')
    if family not in RECIPES:
        raise ValueError('only the four standardized recipes are supported')
    components = data.get('components', {})
    if not isinstance(components, dict):
        raise ValueError('recipe components must be a literal table')
    rust = components.get('rust', [])
    if not isinstance(rust, list):
        raise ValueError('Rust components must be literal records')
    for value in rust:
        if not isinstance(value, dict):
            raise ValueError('Rust components must be literal records')
        _manifest(family, value.get('manifest', 'Cargo.toml'))
        if 'target_directory' in value:
            _staging(value['target_directory'], 'Cargo target directory')
    if family == 'rust-cli':
        for value in components.get('native', []):
            if not isinstance(value, dict):
                raise ValueError('Native components must be literal records')
            _staging(value.get('output'), 'Native component output')
    for value in components.get('rust_tests', []):
        if isinstance(value, dict) and 'manifest' in value:
            _manifest(family, value['manifest'])
    node = data.get('node', {})
    if not isinstance(node, dict):
        raise ValueError('Node declarations must be a literal table')
    packages = node.get('packages', [])
    if not isinstance(packages, list):
        raise ValueError('Node packages must be literal records')
    for value in packages:
        if not isinstance(value, dict):
            raise ValueError('Node packages must be literal records')
        directory = value.get('directory')
        path = _path(directory, 'Node package directory')
        if not ((family == 'godot-addon' and directory == '.') or (
                path.parts and path.parts[0] in NODE_ROOTS[family])):
            raise ValueError('Node packages must use the recipe component directories')
    if family == 'godot-game':
        for value in components.get('godot', {}).get('exports', []):
            if not isinstance(value, dict):
                raise ValueError('Godot exports must be literal records')
            output = _path(value.get('output'), 'Godot export output')
            if not output.is_relative_to(PurePosixPath(APP_DIRECTORY) / 'build') or len(output.parts) < 3:
                raise ValueError('Godot game exports must remain under app/build')
