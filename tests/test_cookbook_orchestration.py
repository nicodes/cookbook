from source_fixture import SDK_ROOT
import copy
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import configuration
import orchestration


class RecipeFactories(unittest.TestCase):
    def test_known_recipes_keep_native_unit_ownership_and_real_implicit_groups(self):
        addon = {'version': 1, 'recipe': 'godot-addon', 'tests': {'unit': [{'path': 'tests/unit.gd'}]}}
        self.assertEqual(orchestration.contract(addon)['route'], 'host')
        app = {'version': 1, 'recipe': 'react-native',
               'components': {'rust': [{'manifest': 'api/Cargo.toml'}],
                              'rust_tests': [{'suite': 'integration', 'name': 'postgres'}]}}
        self.assertEqual(orchestration.contract(app)['test_groups'], ['integration', 'lint', 'security', 'unit'])
        for name in ['native', 'unimplemented']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                orchestration.contract(addon | {'recipe': name})
        with self.assertRaisesRegex(ValueError, 'real native unit'):
            orchestration.contract(addon | {'tests': {}})

    def test_missing_matrix_factory_blocks_full_validation_explicitly(self):
        game = {'version': 1, 'recipe': 'godot-game', 'tests': {'unit': [{'path': 'tests/unit.gd'}]}}
        original = orchestration.importlib.import_module
        missing = ModuleNotFoundError('missing', name='godot_ci')
        def importer(name):
            if name == 'godot_ci':
                raise missing
            return original(name)
        with patch.object(orchestration.importlib, 'import_module', side_effect=importer), self.assertRaisesRegex(ValueError, 'not enrolled'):
            orchestration.matrix_module(game)
        missing = ModuleNotFoundError('missing dependency', name='native_dependency')
        with patch.object(orchestration.importlib, 'import_module', side_effect=importer), self.assertRaises(ModuleNotFoundError):
            orchestration.matrix_module(game)

    def test_preview_is_one_literal_application_environment_without_task_overrides(self):
        native = {'recipe': 'react-native', 'ci': {'preview_environment': 'Preview', 'timeout_minutes': 45}}
        self.assertEqual(configuration.ci_settings(native)['preview_environment'], 'Preview')
        for name, recipe in [('production', 'react-native'), ('${{ secrets.TOKEN }}', 'react-native'),
                             ('Preview', 'godot-addon'), (True, 'react-native')]:
            value = copy.deepcopy(native)
            value['recipe'] = recipe
            value['ci']['preview_environment'] = name
            with self.subTest(name=name, recipe=recipe), self.assertRaises(ValueError):
                configuration.ci_settings(value)
        self.assertNotIn('preview_environment', configuration.ci_settings({'recipe': 'godot-addon'}))


if __name__ == '__main__':
    unittest.main()
