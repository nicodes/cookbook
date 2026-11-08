"""Declared Fast checks must have their guarded native SDK prepared first."""
from source_fixture import SDK_ROOT
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import dependencies
import godot_addon
import godot_game
import native_tests
import react_native
import rust_cli


RECIPES = (godot_addon, godot_game, react_native, rust_cli)
PINS = {'python': '3.13.11', 'node': '26.7.0', 'bun': '1.4.1',
        'rust': '1.94.0', 'actionlint': '1.7.12',
        'github:aviorstudio/gdlint': '0.0.3'}


class FastRunnerPreparation(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='native Fast SDK closure ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pins()
        (self.root / 'check.mjs').write_text('owned native source\n')

    def pins(self, values=None):
        values = PINS if values is None else values
        (self.root / 'mise.toml').write_text('[tools]\n' + ''.join(
            json.dumps(name) + '=' + json.dumps(version) + '\n'
            for name, version in values.items()))

    def config(self, runner='bun'):
        return {'tests': {'lint': [{'runner': runner, 'path': 'check.mjs',
                                   'args': ['owned-argument'], 'timeout': 17}]}}

    def test_shared_capabilities_follow_native_runner_variants(self):
        for runner, expected in [('python', 'python'), ('python-unittest', 'python'),
                                 ('node', 'node'), ('node-test', 'node'),
                                 ('bun', 'bun'), ('bun-test', 'bun')]:
            with self.subTest(runner=runner):
                self.assertEqual(native_tests.runner_tools([{'runner': runner}]), {expected})
        self.assertEqual(native_tests.runner_tools([{'runner': 'bash'}, {'runner': 'godot-check'}]), set())

    def test_unknown_runner_cannot_select_a_fallback_sdk(self):
        with self.assertRaisesRegex(ValueError, 'does not implement native test runner'):
            native_tests.runner_tools([{'runner': 'unknown-runtime'}])

    def test_all_four_fast_selectors_include_the_declared_runtime_without_execution(self):
        with patch.object(subprocess, 'run', side_effect=AssertionError('selection executed a command')):
            for recipe in RECIPES:
                for runner, expected in [('python', 'python'), ('python-unittest', 'python'),
                                         ('node', 'node'), ('node-test', 'node'),
                                         ('bun', 'bun'), ('bun-test', 'bun')]:
                    with self.subTest(recipe=recipe.__name__, runner=runner):
                        self.assertIn(expected, recipe.required_tools(self.root, self.config(runner), 'fast'))

    def test_missing_declared_runtime_pin_refuses_in_every_recipe(self):
        for runtime in ['bun', 'node']:
            self.pins({name: value for name, value in PINS.items() if name != runtime})
            for recipe in RECIPES:
                with self.subTest(recipe=recipe.__name__, runtime=runtime):
                    with self.assertRaises(ValueError):
                        recipe.required_tools(self.root, self.config(runtime), 'fast')

    def test_fast_does_not_acquire_unselected_unit_or_security_runtimes(self):
        config = {'tests': {'lint': [{'runner': 'bash'}],
                            'unit': [{'runner': 'bun'}], 'security': [{'runner': 'bun-test'}]}}
        for recipe in RECIPES:
            with self.subTest(recipe=recipe.__name__):
                self.assertNotIn('bun', recipe.required_tools(self.root, config, 'fast'))

    def test_selection_preserves_exact_source_pins_and_native_declaration(self):
        raw = (self.root / 'mise.toml').read_bytes()
        config = self.config()
        original = json.dumps(config, sort_keys=True)
        for recipe in RECIPES:
            selected = recipe.required_tools(self.root, config, 'fast')
            self.assertEqual(len(selected), len(set(selected)))
            self.assertEqual((self.root / 'mise.toml').read_bytes(), raw)
            self.assertEqual(json.dumps(config, sort_keys=True), original)

    def control(self, selected, *, native_status=0):
        # The real immutable helper still checks the declared pin, Mise metadata,
        # actual executable location and selected environment. Only finite Mise
        # metadata and the final native child are fixtures; nothing is installed.
        dependencies.helper('pinned-tools')
        sdk = self.root / 'installed-bun'
        sdk.mkdir(exist_ok=True)
        binary = sdk / ('bun.exe' if os.name == 'nt' else 'bun')
        binary.write_bytes(b'owned executable fixture, never executed\n')
        binary.chmod(0o755)
        calls = []
        def command(argv, **kwargs):
            if argv[:2] == ['mise', 'where']:
                name, version = argv[2].split('@')
                self.assertEqual((name, version), ('bun', PINS['bun']))
                return subprocess.CompletedProcess(argv, 0 if 'bun' in selected else 1,
                                                   str(sdk) if 'bun' in selected else '', '')
            if argv[:2] == ['mise', 'env']:
                self.assertEqual(argv[2:], ['--json', 'bun@' + PINS['bun']])
                return subprocess.CompletedProcess(argv, 0, json.dumps({'PATH': str(sdk)}), '')
            self.assertEqual(argv, ['bun', str(self.root / 'check.mjs'), 'owned-argument'])
            calls.append((argv, kwargs))
            if native_status:
                raise subprocess.CalledProcessError(native_status, argv)
            return subprocess.CompletedProcess(argv, 0)
        environment = {'COOKBOOK_CALLER_ROOT': str(self.root), 'PATH': ''}
        with patch.object(subprocess, 'run', side_effect=command):
            native_tests.run_entries(self.root, self.config(), environment, 'lint')
        return calls

    def test_actual_native_guard_refuses_unprepared_then_runs_after_each_fast_plan(self):
        for recipe in RECIPES:
            with self.subTest(recipe=recipe.__name__):
                selected = recipe.required_tools(self.root, self.config(), 'fast')
                with self.assertRaisesRegex(ValueError, 'required exact caller SDK is not prepared'):
                    self.control(set(selected) - {'bun'})
                calls = self.control(selected)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][1]['cwd'], self.root)
                self.assertEqual(calls[0][1]['timeout'], 17)
                self.assertTrue(calls[0][1]['check'])
                self.assertEqual(calls[0][1]['env']['MISE_AUTO_INSTALL'], 'false')
                self.assertEqual(calls[0][1]['env']['MISE_EXEC_AUTO_INSTALL'], 'false')
                self.assertEqual(calls[0][1]['env']['MISE_TASK_RUN_AUTO_INSTALL'], 'false')

    def test_native_failure_status_is_not_replaced_by_preparation_success(self):
        selected = godot_game.required_tools(self.root, self.config(), 'fast')
        with self.assertRaises(subprocess.CalledProcessError) as raised:
            self.control(selected, native_status=23)
        self.assertEqual(raised.exception.returncode, 23)
        self.assertEqual(raised.exception.cmd, ['bun', str(self.root / 'check.mjs'), 'owned-argument'])


if __name__ == '__main__':
    unittest.main()
