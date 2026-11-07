"""Known recipe factories and complete-validation ownership, without task overrides."""
import importlib

import recipe

ROUTES = {'godot-addon': 'host', 'react-native': 'host', 'rust-cli': 'native', 'godot-game': 'godot'}


def model(config):
    name = config.get('recipe')
    if name not in ROUTES or config.get('version') != 1:
        raise ValueError('complete CI is not implemented for this recipe')
    components = config.get('components', {})
    rust = components.get('rust', [])
    packages = config.get('node', {}).get('packages', [])
    real_units = bool(config.get('tests', {}).get('unit')) or any(value.get('test', {}).get('enabled') is not False for value in rust)
    real_units |= any(isinstance(value, dict) and (value.get('test') or value.get('test_paths')) for value in packages)
    if not real_units:
        raise ValueError('the recipe must declare real native unit checks')
    groups = sorted(recipe.test_suites(None, config))
    return {'recipe': name, 'route': ROUTES[name], 'test_groups': groups,
            'preview': bool(name == 'react-native' and components.get('app', {}).get('preview_variables'))}


def matrix_module(config):
    route = model(config)['route']
    if route == 'host':
        return None
    name = 'native_ci' if route == 'native' else 'godot_ci'
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        if error.name != name:
            raise
        raise ValueError('complete ' + route + ' orchestration is not enrolled yet') from None


def contract(config):
    values = model(config)
    module = matrix_module(config)
    if module is not None:
        declaration = module.plan(None, config)
        if values['route'] == 'native' and not declaration['requirements']:
            raise ValueError('Rust CLI validation requires its complete declared native matrix')
        if values['route'] == 'godot' and not declaration.get('enrolled'):
            raise ValueError('Godot application validation requires its source-owned worker topology')
        values['matrix'] = declaration
    return values
