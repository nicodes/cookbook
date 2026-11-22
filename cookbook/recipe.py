"""Known recipe adapters behind shared source, SDK and evidence contracts."""
import importlib
import os
import re
import sdk_environment

MODULES = {'godot-addon': 'godot_addon', 'godot-game': 'godot_game',
           'react-native': 'react_native', 'rust-cli': 'rust_cli'}
IMPORTED_NATIVE_GROUPS = 'COOKBOOK_IMPORTED_NATIVE_GROUPS'


def adapter(config):
    try:
        name = MODULES[config['recipe']]
    except KeyError:
        raise ValueError('this native source does not enroll in a standardized application recipe') from None
    return importlib.import_module(name)


def test_suites(root, config):
    module = adapter(config)
    names = (module.test_suites(root, config) if hasattr(module, 'test_suites') else
             ['lint', 'unit', 'security', *[name for name in ['integration', 'e2e'] if name in config.get('tests', {})]])
    if (len(names) != len(set(names)) or not {'lint', 'unit', 'security'} <= set(names)
            or set(names)-{'lint', 'unit', 'security', 'integration', 'e2e'}):
        raise ValueError('recipe must expose its real, distinct test groups')
    return [name for name in ['lint', 'unit', 'security', 'integration', 'e2e'] if name in names]


def signed_release_owns_e2e(config, environment=None):
    acceptance = config.get('publication', {}).get('acceptance') or {}
    if config.get('recipe') != 'rust-cli' or not isinstance(acceptance, dict) or acceptance.get('owns_e2e') is not True:
        return False
    environment = os.environ if environment is None else environment
    repository = environment.get('GITHUB_REPOSITORY', '')
    source = environment.get('GITHUB_SHA', '')
    return bool(environment.get('GITHUB_ACTIONS') == 'true'
        and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)
        and re.fullmatch(r'[a-f0-9]{40}', source)
        and environment.get('GITHUB_EVENT_NAME') == 'workflow_dispatch'
        and environment.get('GITHUB_REF') == 'refs/heads/main'
        and environment.get('GITHUB_WORKFLOW_SHA') == source
        and environment.get('GITHUB_WORKFLOW_REF') == repository + '/.github/workflows/release.yml@refs/heads/main')


def execution_test_suites(root, config, environment=None):
    suites = test_suites(root, config)
    if signed_release_owns_e2e(config, environment):
        suites.remove('e2e')
    return suites


def local_test_suites(root, config):
    suites = execution_test_suites(root, config)
    imported = os.environ.get(IMPORTED_NATIVE_GROUPS)
    if imported:
        groups = imported.split(',')
        allowed = {group for group, enabled in [
            ('e2e', config.get('artifacts', {}).get('native_e2e_evidence_paths')),
            ('unit', config.get('ci', {}).get('rust_reuse_native_unit')),
        ] if enabled}
        if (config['recipe'] != 'rust-cli' or os.environ.get('GITHUB_ACTIONS') != 'true'
                or os.environ.get('COOKBOOK_STAGE') != 'full'
                or len(groups) != len(set(groups)) or not set(groups) <= allowed.intersection(suites)):
            raise ValueError('only the native CI join may import declared test groups')
        suites = [suite for suite in suites if suite not in groups]
    return suites


def configure_environment(root, config):
    os.environ.update(sdk_environment.preserve_mise())
    module = adapter(config)
    protected = {key: value for key, value in os.environ.items()
                 if key.startswith('GITHUB_') or key in {'GH_TOKEN', 'TOOLS_VERIFICATION_SESSION',
                     'COOKBOOK_ROOT', 'COOKBOOK_REF', 'TOOLS_ROOT', 'TOOLS_REF', 'COOKBOOK_CALLER_ROOT'}}
    if hasattr(module, 'configure_environment'):
        values = module.configure_environment(root, config)
    elif config['recipe'] == 'godot-addon':
        values = module.environment(root)
    else:
        values = module.environment(root, config)
    if any(values.get(key) != value for key, value in protected.items()):
        os.environ.update(protected)
        raise ValueError('native build inputs must preserve the Actions execution identity')
    os.environ.update(values)
    return dict(os.environ)
