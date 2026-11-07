from source_fixture import SDK_ROOT
"""The selected display carries real native assertions without changing them."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import godot_ci
import godot_game
import native_tests
import virtual_display


@unittest.skipUnless(sys.platform.startswith('linux'), 'Godot browser workers use reviewed Linux runners')
class VirtualDisplayControls(unittest.TestCase):
    def gate(self):
        return {'name': 'headed-capability', 'kind': 'capability', 'runner': 'blacksmith-4vcpu-ubuntu-2204',
            'timeout_minutes': 10, 'setup': 'browser', 'virtual_display': True,
            'entry': {'runner': 'python', 'path': 'tests/headed.py', 'timeout': 120, 'args': ['original-argument']}}

    def config(self):
        return {'recipe': 'godot-game', 'tests': {'unit': [{'runner': 'python', 'path': 'tests/unit.py'}]},
            'ci': {'godot': {'gates': [self.gate()]}}}

    def test_display_is_explicit_separate_from_production_policy_and_bound_to_native_matrix(self):
        config = self.config(); gate = godot_ci.plan(None, config)['gates']['include'][0]
        self.assertTrue(gate['virtual_display']); self.assertEqual(gate['timeout_minutes'], 10)
        self.assertEqual(gate['entry'], self.gate()['entry'])
        del config['ci']['godot']['gates'][0]['virtual_display']
        self.assertNotIn('virtual_display', godot_ci.plan(None, config)['gates']['include'][0])
        for changes in [{'virtual_display': 'true'}, {'setup': 'native'}, {'kind': 'production'}, {'xvfb': ['chromium']}]:
            config = self.config(); config['ci']['godot']['gates'][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError): godot_ci.plan(None, config)

    def test_real_x_server_is_available_only_through_selected_carrier_and_assertion_failure_propagates(self):
        environment = dict(os.environ); environment.pop('DISPLAY', None)
        virtual_display.setup(Path.cwd(), environment,
            lambda command, root, env: subprocess.run(command, cwd=root, env=env, check=True, timeout=20))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'tests').mkdir(); path = root / 'tests/headed.py'
            body = ('import ctypes,os,sys\nassert os.environ.get("DISPLAY")\n'
                'assert sys.argv[1:] == ["original-argument"]\n'
                'x=ctypes.CDLL("libX11.so.6");x.XOpenDisplay.restype=ctypes.c_void_p\n'
                'display=x.XOpenDisplay(None);assert display\n'
                'x.XCloseDisplay.argtypes=[ctypes.c_void_p];x.XCloseDisplay(display)\n')
            path.write_text(body)
            config = {'tests': {'security': [self.gate()['entry']]}}
            with self.assertRaises(subprocess.CalledProcessError): native_tests.run_entries(root, config, environment, 'security')
            (root / 'app').mkdir(); (root / 'app/project.godot').touch()
            selected = self.config(); selected['components'] = {'godot': {}}
            worker = dict(environment, COOKBOOK_GODOT_ENTRY='gate-headed-capability')
            with patch.object(godot_game.komizo_sdk, 'check'):
                godot_game.native_tests(root, selected, worker, 'security')
            self.assertEqual(path.read_text(), body)
            path.write_text(body + 'raise SystemExit(17)\n')
            with self.assertRaises(subprocess.CalledProcessError) as failure:
                with patch.object(godot_game.komizo_sdk, 'check'):
                    godot_game.native_tests(root, selected, worker, 'security')
            self.assertEqual(failure.exception.returncode, 17)

    def test_original_timeout_and_arguments_survive_wrapper_and_missing_dependencies_never_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'tests').mkdir(); (root / 'tests/headed.py').touch()
            environment = dict(os.environ)
            with patch.object(native_tests.subprocess, 'run') as run:
                native_tests.run_entries(root, {'tests': {'security': [self.gate()['entry']]}}, environment,
                    'security', command_wrapper=virtual_display.command)
            self.assertEqual(run.call_args.args[0], ['xvfb-run', '-a', sys.executable,
                str(root / 'tests/headed.py'), 'original-argument'])
            self.assertEqual(run.call_args.kwargs['timeout'], 120)
            with patch.object(virtual_display.shutil, 'which', return_value=None), patch.object(native_tests.subprocess, 'run') as effect:
                with self.assertRaisesRegex(ValueError, 'make setup'): virtual_display.command(['node', 'owned.js'], environment)
                effect.assert_not_called()


if __name__ == '__main__': unittest.main()
