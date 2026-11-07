"""Known recipe adapters behind shared source, SDK and evidence contracts."""
import importlib
import os
import sdk_environment

MODULES = {'godot-addon': 'godot_addon', 'godot-game': 'godot_game',
           'react-native': 'react_native', 'rust-cli': 'rust_cli'}


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
