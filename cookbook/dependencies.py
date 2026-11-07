"""Resolve the Cookbook provider's single immutable generic Tools dependency."""
import importlib.util
import os
from pathlib import Path
import re
import sys
import tomllib

import sdk_source

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'nicodes/tools'
BINDING_KEYS = ('cookbook_repository', 'cookbook_commit', 'tools_repository', 'tools_commit', 'entrypoint_contract')
_LOADED_HELPERS = {}


def tools_identity():
    record = sdk_source.identity(sdk_source.read_regular(ROOT / 'mise.toml'))
    return {'repository': record['repository'], 'revision': record['revision']}


def binding(cookbook_revision, tools_revision=None):
    if not isinstance(cookbook_revision, str) or not re.fullmatch('[a-f0-9]{40}', cookbook_revision):
        raise ValueError('Cookbook evidence requires its immutable source identity')
    tools_revision = tools_identity()['revision'] if tools_revision is None else tools_revision
    if not isinstance(tools_revision, str) or not re.fullmatch('[a-f0-9]{40}', tools_revision):
        raise ValueError('Tools evidence requires its separate immutable helper source identity')
    return {'cookbook_repository': 'nicodes/cookbook', 'cookbook_commit': cookbook_revision,
            'tools_repository': REPOSITORY, 'tools_commit': tools_revision,
            'entrypoint_contract': 'mise-v1'}


def validate_binding(record, cookbook_revision, tools_revision=None):
    if not isinstance(record, dict) or any(record.get(key) != value
            for key, value in binding(cookbook_revision, tools_revision).items()):
        raise ValueError('evidence differs from the separate Cookbook and Tools source identities')
    return record


def validate_record(record):
    """Check an explicit two-source record without reading provider metadata."""
    if (not isinstance(record, dict) or not isinstance(record.get('tools_commit'), str)
            or not re.fullmatch('[a-f0-9]{40}', record['tools_commit'])):
        raise ValueError('evidence requires both explicit immutable SDK source identities')
    return validate_binding(record, record.get('cookbook_commit'), record['tools_commit'])


def tools_root(supplied=None):
    identity = tools_identity()
    location = supplied if supplied is not None else os.environ.get('TOOLS_ROOT')
    if location:
        directory = Path(location)
    else:
        directory = Path.cwd() / '.artifacts/tools' / identity['revision']
    directory = sdk_source.no_links(directory)
    if directory.resolve() == ROOT.resolve():
        raise ValueError('Cookbook and Tools source roots must retain their distinct identities')
    return sdk_source.verify(directory, identity['revision'], repository=identity['repository'])


def _helper_paths(directory):
    cookbook = str(Path(__file__).resolve().parent)
    directory = str(directory)
    callers = [value for value in sys.path if value not in {cookbook, directory}]
    return cookbook, directory, callers


def helper(name):
    if not isinstance(name, str) or not re.fullmatch('[a-z][a-z0-9_-]*', name):
        raise ValueError('select an explicit generic Tools helper')
    key = 'cookbook_tools_' + name.replace('-', '_')
    module = sys.modules.get(key)
    if module is not None:
        loaded = _LOADED_HELPERS.get(key)
        if (loaded is None or loaded[1] is not module or not isinstance(getattr(module, '__file__', None), str)
                or Path(module.__file__).resolve() != loaded[0]):
            raise ValueError('generic helper origin differs from the selected immutable Tools source')
        cookbook, directory, callers = _helper_paths(loaded[0].parent)
        sys.path[:] = [cookbook, directory, *callers]
        return module
    path = tools_root() / 'helpers' / (name + '.py')
    if path.is_symlink() or not path.is_file():
        raise ValueError('generic helper must be a regular file in the selected Tools source')
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    cookbook, directory, callers = _helper_paths(path.parent)
    try:
        # A generic helper resolves its own siblings while its body loads.
        sys.path[:] = [directory, cookbook, *callers]
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(key, None)
        raise
    finally:
        # Generic modules may themselves prepend their directory. Restore the
        # recipe namespace first, retaining checked generic lazy imports next.
        sys.path[:] = [cookbook, directory, *callers]
    _LOADED_HELPERS[key] = (path.resolve(), module)
    return module


def recipe_helper(name):
    """Keep the one declaration-aware diagnostic adapter in Cookbook."""
    if name != 'native-diagnostics':
        return helper(name)
    path = ROOT / 'helpers/native-diagnostics.py'
    spec = importlib.util.spec_from_file_location('cookbook_native_diagnostics', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
