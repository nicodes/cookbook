from source_fixture import SDK_ROOT
"""The one-job CI path runs each phase in-process and never touches the matrix machinery."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import ci
import godot_ci  # imported before the subprocess mocks: their Tools checkout is verified at import
import native_ci

SOURCE, TOOLS = 'a' * 40, 'b' * 40


class SinglePhase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.order = []
        self.module = MagicMock(spec=['artifact_paths', 'post_test_evidence'])
        self.module.artifact_paths.return_value = ['dist']
        self.module.post_test_evidence.side_effect = lambda *args: self.order.append('post')
        self.model = {'route': 'host', 'recipe': 'react-native', 'test_groups': ['lint', 'unit', 'security']}
        self.config = {'recipe': 'react-native', 'version': 1}
        self.mocks = {}
        for target, options in [
                ('ci.load', {'return_value': self.config}),
                ('ci.orchestration.model', {'side_effect': lambda config: self.model}),
                ('ci.orchestration.matrix_module', {'side_effect': AssertionError('single-job must not use the matrix')}),
                ('ci.identity', {'side_effect': lambda *args: self.order.append('identity') or 'tree'}),
                ('ci.recipe.configure_environment', {'side_effect': lambda *args: self.order.append('configure')}),
                ('ci.recipe.adapter', {'return_value': self.module}),
                ('ci.proof', {'side_effect': lambda *args, **kwargs: self.order.append('proof') or {'status': 'passed', **kwargs}}),
                ('ci.relative', {'side_effect': lambda root, name, **kwargs: root / name}),
                ('ci.subprocess.run', {'side_effect': self.dispatch})]:
            patcher = patch(target, **options)
            self.mocks[target] = patcher.start()
            self.addCleanup(patcher.stop)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ['COOKBOOK_GODOT_ENTRY', 'COOKBOOK_GODOT_HOST', 'GITHUB_OUTPUT', 'COOKBOOK_CANDIDATE_COMMIT']:
            os.environ.pop(name, None)

    def dispatch(self, command, **kwargs):
        self.order.append(command[-1])
        self.assertEqual(kwargs['cwd'], self.root)
        self.assertEqual(kwargs['env']['COOKBOOK_STAGE'], 'full')
        self.assertTrue(kwargs['check'])

    def run_phase(self, phase, **options):
        ci.run_single_job(self.root, 'full', phase, SOURCE, TOOLS, **options)

    def test_setup_and_build_configure_first_then_dispatch_only_that_command(self):
        for phase in ['setup', 'build']:
            self.order.clear()
            self.run_phase(phase)
            self.assertEqual([step for step in self.order if step != 'identity'], ['configure', phase])
            self.assertFalse(self.module.post_test_evidence.called)
            self.assertFalse((self.root / '.artifacts/cookbook-validation.json').exists())

    def test_every_phase_binds_the_requested_source_and_cookbook_before_running(self):
        for phase in ['setup', 'build', 'test']:
            self.order.clear()
            self.run_phase(phase)
            self.assertEqual(self.order[0], 'identity')
            self.assertEqual(self.mocks['ci.identity'].call_args.args[1:], (SOURCE, TOOLS))

    def test_test_phase_runs_full_test_then_post_test_evidence_proof_and_receipt(self):
        output = self.root / 'output'
        os.environ['GITHUB_OUTPUT'] = str(output)
        os.environ['COOKBOOK_CANDIDATE_COMMIT'] = 'c' * 40
        (self.root / '.artifacts').mkdir()
        stale = self.root / '.artifacts/cookbook-validation.json'
        stale.write_text('{"stale": true}')
        self.run_phase('test')
        self.assertEqual([step for step in self.order if step != 'identity'], ['configure', 'test', 'post', 'proof'])
        self.assertEqual(self.module.post_test_evidence.call_args.args[-1], 'c' * 40)
        receipt = json.loads(stale.read_text())
        self.assertEqual(receipt['coverage'], 'single-job')
        self.assertNotIn('stale', receipt)
        self.assertNotIn('head', receipt)
        self.assertIn('artifact-paths<<COOKBOOK_ARTIFACT_PATHS\ndist\n.artifacts/verification/\n.artifacts/cookbook-validation.json\n',
                      output.read_text())
        self.assertFalse(hasattr(ci, 'godot_ci'))

    def test_merge_validation_identities_are_recorded_together_or_refused(self):
        self.run_phase('test', head='d' * 40, base='e' * 40)
        self.assertEqual(json.loads((self.root / '.artifacts/cookbook-validation.json').read_text())['head'], 'd' * 40)
        for options in [{'head': 'd' * 40}, {'base': 'e' * 40}, {'head': 'd' * 40, 'base': 'short'}]:
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, 'head and base'):
                self.run_phase('test', **options)

    def test_partial_modes_phases_and_inherited_matrix_selectors_are_refused_before_any_work(self):
        with self.assertRaisesRegex(ValueError, 'complete validation'):
            ci.run_single_job(self.root, 'fast', 'test', SOURCE, TOOLS)
        with self.assertRaisesRegex(ValueError, 'setup, build or test'):
            self.run_phase('verify')
        for name in ['COOKBOOK_GODOT_ENTRY', 'COOKBOOK_GODOT_HOST']:
            with self.subTest(name=name), patch.dict(os.environ, {name: 'unit-1-2'}), \
                    self.assertRaisesRegex(ValueError, 'selected matrix worker'):
                self.run_phase('test')
        self.assertNotIn('configure', self.order)
        self.assertFalse(self.mocks['ci.subprocess.run'].called)

    def test_rust_cli_support_is_decided_by_its_native_matrix_and_worker_bound_godot_gates_fail_clearly(self):
        self.model = {'route': 'native', 'recipe': 'rust-cli', 'test_groups': []}
        with patch('native_ci.single_job_support', side_effect=ValueError('second native wave')), \
                self.assertRaisesRegex(ValueError, 'second native wave'):
            self.run_phase('setup')
        self.assertFalse(self.mocks['ci.subprocess.run'].called)
        self.assertNotIn('configure', self.order)
        # Godot gates of every kind are covered by the gate scope instead (see test_single_job_godot_gates).
        self.model = {'route': 'godot', 'recipe': 'godot-game', 'test_groups': []}
        self.config['ci'] = {'godot': {'gates': [{'name': 'caps', 'kind': 'capability', 'timeout_minutes': 5}]}}
        with patch('godot_ci.single_union'), patch('godot_ci.single_gate_rows', return_value=[]):
            self.run_phase('build')
        self.assertIn('build', self.order)

    def test_godot_without_gates_or_with_plain_test_gates_runs_whole_in_one_process(self):
        self.model = {'route': 'godot', 'recipe': 'godot-game', 'test_groups': ['lint', 'unit', 'security']}
        self.config.update(recipe='godot-game', tests={'e2e': [{'runner': 'python', 'path': 'a.py', 'name': 'a'}]})
        for settings in [{}, {'godot': {}}, {'godot': {'gates': [{'name': 'smoke', 'timeout_minutes': 5, 'suite': 'e2e', 'tests': ['a']}]}}]:
            self.order.clear()
            self.config['ci'] = settings
            with patch('godot_ci.single_coverage', return_value={'scope': 'main'}):
                self.run_phase('test')
            self.assertEqual([step for step in self.order if step != 'identity'], ['configure', 'test', 'post', 'proof'])


class Arguments(unittest.TestCase):
    def invoke(self, *extra):
        argv = ['ci.py', '--mode', 'full', '--source', SOURCE, '--cookbook-ref', TOOLS, *extra]
        with patch.object(sys, 'argv', argv), patch.object(ci, 'run') as multi, patch.object(ci, 'run_single_job') as single, \
                patch('sys.stderr', io.StringIO()):
            try:
                ci.main()
                code = 0
            except SystemExit as error:
                code = error.code
        return code, multi, single

    def test_without_the_flag_the_multi_job_entry_runs_unchanged(self):
        code, multi, single = self.invoke('--release-bump', 'patch')
        self.assertEqual(code, 0)
        multi.assert_called_once_with(Path.cwd().resolve(), 'full', SOURCE, TOOLS, 'patch', '', '')
        single.assert_not_called()

    def test_single_job_runs_its_phase_and_never_the_multi_job_entry(self):
        for phase in ['setup', 'build', 'test']:
            code, multi, single = self.invoke('--single-job', '--phase', phase)
            self.assertEqual(code, 0)
            single.assert_called_once_with(Path.cwd().resolve(), 'full', phase, SOURCE, TOOLS, '', '', '')
            multi.assert_not_called()

    def test_flag_and_phase_must_be_given_together_with_a_known_phase(self):
        for extra in [('--single-job',), ('--phase', 'test'), ('--single-job', '--phase', 'deploy')]:
            code, multi, single = self.invoke(*extra)
            self.assertEqual(code, 2, extra)
            multi.assert_not_called()
            single.assert_not_called()


if __name__ == '__main__':
    unittest.main()
