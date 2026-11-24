from source_fixture import SDK_ROOT
"""The four one-job composite actions keep to the composite policy and the existing sibling-action pattern."""
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unittest

import workflow_bootstrap
from workflow_fixtures import read_yaml

ALLOWED = {'run', 'shell', 'if', 'name', 'id', 'env', 'working-directory', 'uses', 'with', 'continue-on-error'}
PINNED = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[a-f0-9]{40}\Z')
SIBLING = './.artifacts/recipe-sdks/_cookbook/'
NAMES = ['prepare', 'build', 'test', 'security', 'finish']


def action(name):
    return read_yaml((SDK_ROOT / name / 'action.yml').read_text())


def steps(name):
    return action(name)['runs']['steps']


def every_step():
    for name in NAMES:
        for step in steps(name):
            yield name, step


class Policy(unittest.TestCase):
    def test_each_phase_is_one_composite_action_with_the_declared_contract(self):
        for name in NAMES:
            self.assertEqual(action(name)['runs']['using'], 'composite', name)
        inputs = action('prepare')['inputs']
        self.assertEqual(set(inputs), {'cookbook-ref', 'source', 'checkout', 'advisories',
                                       'preview-clerk-key', 'version', 'date', 'target'})
        self.assertTrue(inputs['cookbook-ref']['required'] and inputs['source']['required'])
        self.assertEqual({key: value['default'] for key, value in inputs.items() if 'default' in value},
                         {'checkout': 'true', 'advisories': 'skip', 'preview-clerk-key': '', 'version': '',
                          'date': '', 'target': ''})
        self.assertNotIn('inputs', action('build'))
        self.assertNotIn('inputs', action('test'))
        self.assertNotIn('inputs', action('security'))
        self.assertNotIn('outputs', action('security'))
        self.assertEqual(set(action('test')['outputs']), {'artifact-paths'})
        self.assertNotIn('outputs', action('build'))
        finish = action('finish')['inputs']
        self.assertEqual(set(finish), {'status', 'entry'})
        self.assertTrue(finish['status']['required'])
        self.assertEqual(finish['entry']['default'], 'host')

    def test_steps_use_only_composite_fields_and_no_step_timeout_or_caller_only_context(self):
        for name, step in every_step():
            with self.subTest(action=name, step=step.get('name')):
                self.assertLessEqual(set(step), ALLOWED)
                self.assertTrue(step.get('name'))
                self.assertEqual('run' in step, 'shell' in step)
                self.assertNotEqual('run' in step, 'uses' in step)
        for name in NAMES:
            text = (SDK_ROOT / name / 'action.yml').read_text()
            self.assertNotRegex(text, r'\$\{\{\s*(secrets|vars)\.', name)
            self.assertNotIn('timeout-minutes', text.replace('`timeout-minutes`', ''), name)

    def test_nested_actions_are_pinned_remote_pins_or_the_same_sibling_paths_workflows_use(self):
        workflows = ''.join(path.read_text() for path in (SDK_ROOT / '.github/workflows').glob('build-and-test-*.yml'))
        for name, step in every_step():
            reference = step.get('uses')
            if reference is None:
                continue
            with self.subTest(action=name, uses=reference):
                if reference.startswith('./'):
                    self.assertTrue(reference.startswith(SIBLING))
                    self.assertTrue((SDK_ROOT / reference.removeprefix(SIBLING) / 'action.yml').is_file())
                    self.assertIn('uses: ' + reference, workflows)
                else:
                    self.assertRegex(reference, PINNED)

    def test_source_and_cookbook_checkouts_are_shallow_credential_free_and_caller_selected(self):
        checkouts = [step for step in steps('prepare') if step.get('uses', '').startswith('actions/checkout@')]
        self.assertEqual([step['with'].get('repository') for step in checkouts], [None, 'nicodes/cookbook'])
        for step in checkouts:
            self.assertEqual(step['if'], "inputs.checkout == 'true'")
            self.assertEqual(step['with']['persist-credentials'], False)
            self.assertNotIn('fetch-depth', step['with'] if step['with'].get('repository') else {})
        self.assertEqual(checkouts[0]['with']['fetch-depth'], 1)
        self.assertEqual(checkouts[0]['with']['ref'], '${{ inputs.source }}')
        self.assertEqual(checkouts[1]['with']['ref'], '${{ inputs.cookbook-ref }}')
        self.assertEqual(checkouts[1]['with']['path'], '.artifacts/recipe-sdks/_cookbook')

    def test_windows_bootstrap_is_the_exact_generated_composite_block_before_any_cookbook_code(self):
        text = (SDK_ROOT / 'prepare/action.yml').read_text()
        block = workflow_bootstrap.private_steps(indent=4, composite=True)
        self.assertEqual(text.count(block), 1)
        self.assertLess(text.index('actions/checkout@'), text.index(block))
        self.assertLess(text.index(block), text.index('repository: nicodes/cookbook'))

    def test_prepare_orders_admission_before_checkout_and_ends_with_the_setup_phase(self):
        names = [step['name'] for step in steps('prepare')]
        self.assertTrue(names[0].startswith('Validate immutable source and accepted Cookbook'))
        order = ['Check out exact source', 'Check out pinned cookbook', 'Prepare separate immutable Cookbook and Tools sources',
                 'Check documentation boundary', 'Create protected checkout output root', 'Set up pinned SDKs',
                 'Restore bounded Cargo compiler intermediates', 'Read declared native diagnostic locations', 'Run the setup phase']
        positions = [names.index(name) for name in order]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(names[-1], 'Run the setup phase')
        self.assertLess(names.index('Preserve Git source line endings'), names.index('Check out exact source'))
        self.assertLess(names.index('Record the validated identities for every later phase'),
                        names.index('Set up pinned SDKs'))
        recorded = next(step for step in steps('prepare') if step['name'].startswith('Record the validated identities'))
        for name in ['SOURCE_SHA', 'COOKBOOK_SHA', 'COOKBOOK_MODE=full', 'COOKBOOK_ADVISORIES', 'MISE_GITHUB_TOKEN']:
            self.assertIn(name, recorded['run'])

    def test_phases_run_exactly_their_single_job_phase_and_nothing_else(self):
        for name, phase in [('prepare', 'setup'), ('build', 'build'), ('test', 'test'), ('security', 'security')]:
            final = steps(name)[-1]
            self.assertTrue(final['run'].endswith('--single-job --phase ' + phase), name)
            self.assertIn('--mode "$COOKBOOK_MODE" --source "$SOURCE_SHA" --cookbook-ref "$COOKBOOK_SHA"', final['run'])
            self.assertIn('_cookbook/cookbook/ci.py', final['run'])
            self.assertEqual(final['env'], {'GH_TOKEN': '${{ github.token }}'})
        self.assertEqual(steps('test')[0]['id'], 'gate')
        self.assertEqual(action('test')['outputs']['artifact-paths']['value'], '${{ steps.gate.outputs.artifact-paths }}')

    def test_finish_runs_after_failure_saves_the_cache_only_on_success_and_uploads_no_accepted_outputs(self):
        finish = steps('finish')
        for step in finish:
            self.assertTrue(step['if'].startswith('always()'), step['name'])
        save = next(step for step in finish if step.get('uses', '').endswith('/compiler-cache'))
        self.assertEqual(save['if'], "always() && inputs.status == 'success'")
        self.assertEqual(save['with']['mode'], 'save')
        diagnostics = next(step for step in finish if step.get('uses', '').endswith('/native-diagnostics'))
        self.assertEqual(diagnostics['with']['job-status'], '${{ inputs.status }}')
        self.assertEqual(diagnostics['with']['entry'], '${{ inputs.entry }}')
        self.assertNotIn('cookbook-full-', (SDK_ROOT / 'finish/action.yml').read_text())

    def test_the_single_job_selection_is_never_read_by_runtime_code(self):
        for path in sorted((SDK_ROOT / 'cookbook').glob('*.py')):
            if path.name != 'recipe_schema.py':
                self.assertNotRegex(path.read_text(), r'[\'"]single_job[\'"]', path.name)


class CookbookAdmission(unittest.TestCase):
    """Run the real admission script with a recorded stand-in for the one compare call."""
    def execute(self, relation, repository='owner/project', cookbook='a' * 40, sha='b' * 40, **overrides):
        script = steps('prepare')[0]['run']
        with tempfile.TemporaryDirectory() as directory:
            gh = Path(directory) / 'gh'
            gh.write_text('#!/bin/sh\necho "$@" >> "$GH_LOG"\necho "$GH_RELATION"\n')
            gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
            log = Path(directory) / 'log'
            environment = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'], 'GH_LOG': str(log),
                           'GH_RELATION': relation, 'GITHUB_REPOSITORY': repository, 'GITHUB_SHA': sha,
                           'SOURCE_SHA': 'c' * 40, 'COOKBOOK_SHA': cookbook, 'ADVISORIES': 'skip',
                           'CHECKOUT': 'true', 'PREVIEW_CLERK_KEY': '', **overrides}
            result = subprocess.run(['bash', '-c', script], env=environment, capture_output=True, text=True, timeout=30)
            return result.returncode, log.read_text().splitlines() if log.exists() else []

    def test_only_a_cookbook_ahead_of_or_identical_to_main_is_accepted_with_one_compare(self):
        for relation, accepted in [('ahead', True), ('identical', True), ('behind', False), ('diverged', False), ('', False)]:
            with self.subTest(relation=relation):
                code, calls = self.execute(relation)
                self.assertEqual(code == 0, accepted)
                self.assertEqual(calls, ['api repos/nicodes/cookbook/compare/' + 'a' * 40 + '...main --jq .status'])

    def test_malformed_identities_and_selections_are_refused_before_any_network_call(self):
        for overrides in [{'SOURCE_SHA': 'c' * 39}, {'COOKBOOK_SHA': 'A' * 40}, {'ADVISORIES': 'Skip'},
                          {'CHECKOUT': 'yes'}, {'PREVIEW_CLERK_KEY': 'line\nbreak'}]:
            with self.subTest(overrides=overrides):
                code, calls = self.execute('identical', **overrides)
                self.assertNotEqual(code, 0)
                self.assertEqual(calls, [])

    def test_only_cookbooks_own_ci_may_run_the_commit_it_is_testing_before_it_reaches_main(self):
        code, calls = self.execute('behind', repository='nicodes/cookbook', cookbook='b' * 40)
        self.assertEqual((code, calls), (0, []))
        for repository, cookbook in [('nicodes/cookbook', 'a' * 40), ('owner/project', 'b' * 40),
                                     ('nicodes/cookbook-fork', 'b' * 40)]:
            with self.subTest(repository=repository, cookbook=cookbook):
                code, calls = self.execute('behind', repository=repository, cookbook=cookbook)
                self.assertNotEqual(code, 0)
                self.assertEqual(len(calls), 1)


class ProviderJob(unittest.TestCase):
    def test_provider_runs_prepare_build_test_and_finish_against_a_rendered_fixture_in_one_job(self):
        jobs = read_yaml((SDK_ROOT / '.github/workflows/build-and-test.yml').read_text())['jobs']
        job = jobs['single-job']
        self.assertIn('single-job', jobs['build-test']['needs'])
        uses = [(step['uses'].removeprefix(SIBLING), step) for step in job['steps'] if step.get('uses', '').startswith(SIBLING)]
        self.assertEqual([name for name, _ in uses], ['prepare', 'build', 'test', 'finish'])
        for name, step in uses:
            self.assertIn('timeout-minutes', step, name)
        self.assertEqual(uses[0][1]['with']['checkout'], 'false')
        self.assertEqual(uses[3][1]['if'], "always() && steps.prepare.outcome != 'skipped'")
        self.assertEqual(uses[3][1]['with']['status'], '${{ job.status }}')
        self.assertLessEqual(job['timeout-minutes'], 45)
        for step in job['steps']:
            if step.get('uses', '').startswith('actions/checkout@'):
                self.assertEqual(step['with']['persist-credentials'], False)
        self.assertIn('tests/single_job_fixture.py', ''.join(step.get('run', '') for step in job['steps']))


if __name__ == '__main__':
    unittest.main()
