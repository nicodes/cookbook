"""Keep authenticated native matrix metadata adoption ahead of CLI defaults."""
from contextlib import ExitStack
import copy
import json
import os
import subprocess
import unittest
from unittest.mock import patch

import test_native_ci as native_fixture
import ci
import recipe
import sdk_environment

native = native_fixture.native


class NativeHostMetadataOrder(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixture.NativeMatrix('test_all_six_platforms_remain_real_native_requirements')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config = copy.deepcopy(self.fixture.config)
        self.config['version'] = 1
        # This real compiler alias must also be bound after matrix adoption.
        self.config['components']['native'] = [{
            'name': 'api', 'role': 'api', 'component': 'cli', 'binary': 'example',
            'output': '.artifacts/native-api/example_{platform}_{arch}',
            'environment': {'FIXTURE_BUILD_VERSION': '{version}'}}]
        self.root = self.fixture.root
        self.source, self.sdk = self.fixture.source, self.fixture.tools
        self.declaration = native.plan(self.root, self.config)
        self.proofs = [{'version_identity': '0.5.0', 'date_identity': '2026-11-09'}
                       for _entry in self.declaration['requirements']]
        self.commands = []

    def environment(self, **explicit):
        values = dict(os.environ)
        values.update(self.fixture.environment)
        # CI's outer provider checkout is not this disposable source caller.
        # Bind the real fixture before exercising the unchanged identity guard.
        values['COOKBOOK_CALLER_ROOT'] = str(self.root.resolve())
        values['GITHUB_ENV'] = str(self.root / 'fixture-github-env')
        for key in ['VERSION', 'DATE', 'COMMIT', 'FIXTURE_BUILD_VERSION', 'GITHUB_OUTPUT',
                    'COOKBOOK_CANDIDATE_COMMIT', 'COOKBOOK_NATIVE_COMPONENTS',
                    'COOKBOOK_NATIVE_TESTS', 'COOKBOOK_TEST_PATHS']:
            values.pop(key, None)
        return values | explicit

    def contexts(self, environment):
        stack = ExitStack()
        stack.enter_context(patch.dict(os.environ, environment, clear=True))
        # Admission/build receipts belong to other controls. This fixture has
        # no real product build and the provider worktree can be under edit.
        stack.enter_context(patch.object(ci, 'load', return_value=self.config))
        stack.enter_context(patch.object(ci, 'identity', return_value=self.fixture.tree))
        proof = {'status': 'passed', 'source_commit': self.source,
                 **native.dependencies.binding(self.sdk), 'build_verified': True,
                 'tests_verified': True, 'test_groups': self.declaration['host_groups']}
        stack.enter_context(patch.object(ci, 'proof', return_value=proof))
        # Only transport is substituted; native prepare_host, job matching,
        # metadata validation/adoption and complete-host joining remain real.
        stack.enter_context(patch.object(native, 'download', return_value=self.root / 'downloads'))
        stack.enter_context(patch.object(native, 'import_packets', return_value=self.proofs))
        stack.enter_context(patch.object(native, 'github', return_value=self.fixture.jobs(self.declaration['requirements'])))
        self.configure = stack.enter_context(patch.object(recipe, 'configure_environment', wraps=recipe.configure_environment))
        original_run = subprocess.run
        dispatch = {tuple(sdk_environment.python_entry(ci.COOKBOOK / 'cookbook/dispatch.py', command)): command
                    for command in ['setup', 'build', 'test']}

        def run(arguments, *args, **options):
            selected = dispatch.get(tuple(arguments))
            if selected is None:
                # In particular actual Git/source queries and native helper
                # imports must execute normally, not look like successful setup.
                return original_run(arguments, *args, **options)
            self.assertFalse(args)
            self.assertEqual(set(options), {'cwd', 'env', 'check'})
            self.assertEqual(options['cwd'], self.root)
            self.assertIs(options['check'], True)
            values = options['env']
            self.assertEqual((values['VERSION'], values['DATE']), ('0.5.0', '2026-11-09'))
            self.assertEqual(values['FIXTURE_BUILD_VERSION'], '0.5.0')
            self.assertEqual(values['COMMIT'], self.source)
            self.assertEqual(values['COOKBOOK_CALLER_ROOT'], str(self.root.resolve()))
            self.assertEqual(values['NATIVE_CLI_ARCHIVES'], str(self.root / '.artifacts/native-archives'))
            for key, expected in self.fixture.environment.items():
                self.assertEqual(values[key], expected, 'native join changed the Actions execution identity')
            self.commands.append(selected)
            return subprocess.CompletedProcess(arguments, 0)

        stack.enter_context(patch.object(subprocess, 'run', side_effect=run))
        return stack

    def test_implicit_unanimous_matrix_inputs_precede_actual_cli_environment_defaults(self):
        with self.contexts(self.environment()):
            ci.run(self.root, 'full', self.source, self.sdk)
            self.assertEqual((os.environ['VERSION'], os.environ['DATE'], os.environ['FIXTURE_BUILD_VERSION']),
                             ('0.5.0', '2026-11-09', '0.5.0'))
        self.configure.assert_called_once_with(self.root, self.config)
        self.assertEqual(self.commands, ['setup', 'build', 'test'])
        report = json.loads((self.root / '.artifacts/cookbook-validation.json').read_text())
        self.assertEqual(report['coverage'], 'complete-host-and-native-matrix')
        self.assertEqual(report['native_matrix']['proofs'], self.proofs)

    def test_matching_explicit_release_inputs_still_reach_every_host_command(self):
        with self.contexts(self.environment(VERSION='0.5.0', DATE='2026-11-09')):
            ci.run(self.root, 'full', self.source, self.sdk)
        self.configure.assert_called_once_with(self.root, self.config)
        self.assertEqual(self.commands, ['setup', 'build', 'test'])

    def test_explicit_version_or_date_mismatch_refuses_before_configuration_and_commands(self):
        for field, wrong in [('VERSION', 'dev'), ('DATE', 'unknown')]:
            with self.subTest(field=field):
                self.commands.clear()
                values = self.environment(VERSION='0.5.0', DATE='2026-11-09') | {field: wrong}
                with self.contexts(values), self.assertRaisesRegex(ValueError, 'build inputs differ'):
                    ci.run(self.root, 'full', self.source, self.sdk)
                self.configure.assert_not_called()
                self.assertEqual(self.commands, [])
                self.assertFalse((self.root / '.artifacts/cookbook-validation.json').exists())

    def test_disagreeing_matrix_inputs_cannot_be_hidden_by_recipe_defaults(self):
        self.proofs[-1] = self.proofs[-1] | {'version_identity': '0.5.1'}
        with self.contexts(self.environment()), self.assertRaisesRegex(ValueError, 'builders disagree'):
            ci.run(self.root, 'full', self.source, self.sdk)
        self.configure.assert_not_called()
        self.assertEqual(self.commands, [])
        self.assertFalse((self.root / '.artifacts/cookbook-validation.json').exists())


if __name__ == '__main__':
    unittest.main()
