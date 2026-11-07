"""The real CLI parser must forward its Cookbook identity before worker dispatch."""
from source_fixture import SDK_ROOT, SDK_REF
from contextlib import chdir, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import ci
import configuration
import dependencies
import godot_ci


class GodotCLIIdentityControls(unittest.TestCase):
    source = 'a' * 40
    cookbook = 'c' * 40

    def arguments(self, operation, *extra):
        return ['godot_ci.py', operation, '--source', self.source,
                '--cookbook-ref', self.cookbook, *extra]

    def config(self):
        return {'recipe': 'godot-game',
                'tests': {'unit': [{'name': 'test-native_case', 'runner': 'godot-check',
                                    'path': 'app/tests/native_case.gd'}]},
                'ci': {'runner': 'ubuntu-24.04', 'godot': {'unit_shards': 1, 'unit_workers': 4}}}

    def test_plan_uses_actual_parser_and_identity_before_the_native_topology(self):
        with tempfile.TemporaryDirectory() as temporary, chdir(temporary):
            root = Path.cwd().resolve()
            config = self.config()
            expected = godot_ci.plan(root, config, {})
            calls = []
            with patch.object(sys, 'argv', self.arguments('plan')), \
                    patch.object(ci, 'identity', side_effect=lambda *args: calls.append(('identity', args))), \
                    patch.object(configuration, 'load', side_effect=lambda selected: calls.append(('load', selected)) or config), \
                    patch.dict('os.environ', {}, clear=True), redirect_stdout(io.StringIO()) as output:
                godot_ci.main()
            self.assertEqual(calls, [('identity', (root, self.source, self.cookbook)), ('load', root)])
            self.assertEqual(json.loads(output.getvalue()), expected)
            self.assertEqual(expected['requirements'][1]['workers'], 4)

    def test_execute_forwards_only_the_parsed_Cookbook_ref_and_declared_entry(self):
        with tempfile.TemporaryDirectory() as temporary, chdir(temporary), \
                patch.object(sys, 'argv', self.arguments('execute', '--entry', 'unit-1-1')), \
                patch.object(ci, 'identity') as identity, \
                patch.object(configuration, 'load', return_value=self.config()), \
                patch.object(godot_ci, 'execute_entry') as execute, patch.object(godot_ci, 'run_entry') as run:
            root = Path.cwd().resolve()
            godot_ci.main()
            identity.assert_called_once_with(root, self.source, self.cookbook)
            execute.assert_called_once_with(root, self.source, self.cookbook, 'unit-1-1')
            run.assert_not_called()

    def test_run_forwards_only_the_parsed_Cookbook_ref_and_declared_entry(self):
        with tempfile.TemporaryDirectory() as temporary, chdir(temporary), \
                patch.object(sys, 'argv', self.arguments('run', '--entry', 'unit-1-1')), \
                patch.object(ci, 'identity') as identity, \
                patch.object(configuration, 'load', return_value=self.config()), \
                patch.object(godot_ci, 'run_entry') as run, patch.object(godot_ci, 'execute_entry') as execute:
            root = Path.cwd().resolve()
            godot_ci.main()
            identity.assert_called_once_with(root, self.source, self.cookbook)
            run.assert_called_once_with(root, self.source, self.cookbook, 'unit-1-1')
            execute.assert_not_called()

    def test_retired_tools_option_refuses_before_source_or_worker_access(self):
        with patch.object(sys, 'argv', self.arguments('plan', '--tools', 'd' * 40)), \
                patch.object(ci, 'identity') as identity, redirect_stderr(io.StringIO()) as error:
            with self.assertRaises(SystemExit) as refused:
                godot_ci.main()
            self.assertEqual(refused.exception.code, 2)
            self.assertIn('unrecognized arguments: --tools', error.getvalue())
            identity.assert_not_called()

    def test_invalid_source_still_refuses_through_the_real_identity_guard(self):
        argv = self.arguments('plan')
        argv[argv.index('--source') + 1] = 'not-a-source-commit'
        with patch.object(sys, 'argv', argv), patch.object(configuration, 'load') as load, \
                patch.object(godot_ci, 'plan') as plan, redirect_stderr(io.StringIO()) as error:
            with self.assertRaises(SystemExit) as refused:
                godot_ci.main()
            self.assertEqual(refused.exception.code, 1)
            self.assertIn('CI requires exact source and cookbook commits', error.getvalue())
            load.assert_not_called()
            plan.assert_not_called()

    def test_real_Tools_ref_cannot_stand_for_the_Cookbook_identity(self):
        helper = dependencies.tools_identity()['revision']
        self.assertNotEqual(helper, SDK_REF)
        argv = ['godot_ci.py', 'plan', '--source', SDK_REF, '--cookbook-ref', helper]
        with chdir(SDK_ROOT), patch.object(sys, 'argv', argv), patch.object(configuration, 'load') as load, \
                patch.object(godot_ci, 'plan') as plan, redirect_stderr(io.StringIO()) as error:
            with self.assertRaises(SystemExit) as refused:
                godot_ci.main()
            self.assertEqual(refused.exception.code, 1)
            self.assertIn('checked-out source or cookbook differs from the requested revision', error.getvalue())
            load.assert_not_called()
            plan.assert_not_called()

    def test_worker_entry_remains_required_after_valid_identity(self):
        for operation in ['execute', 'run']:
            with self.subTest(operation=operation), patch.object(sys, 'argv', self.arguments(operation)), \
                    patch.object(ci, 'identity'), patch.object(configuration, 'load', return_value=self.config()), \
                    patch.object(godot_ci, 'execute_entry') as execute, patch.object(godot_ci, 'run_entry') as run, \
                    redirect_stderr(io.StringIO()) as error:
                with self.assertRaises(SystemExit) as refused:
                    godot_ci.main()
                self.assertEqual(refused.exception.code, 1)
                self.assertIn('Godot workers require one exact declared entry', error.getvalue())
                execute.assert_not_called()
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
