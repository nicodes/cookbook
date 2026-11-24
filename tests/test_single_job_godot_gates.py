from source_fixture import SDK_ROOT, write_source
"""Godot gates in one-job validation: in-process gates, a job per different runner, and their coverage proof."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import ci
import configuration
import generate
import godot_ci
import godot_game
import single_job_workflows
from workflow_fixtures import read_yaml

REVISION = 'a' * 40
SOURCE = 'b' * 40
BASE = """version=1
recipe="godot-game"
[ci]
single_job=true
[ci.godot]
build_timeout_minutes=25
unit_timeout_minutes=15
host_timeout_minutes=40
"""
UNIT = '[[tests.unit]]\nrunner="godot"\npath="tests/unit.gd"\nsentinel="UNIT PASS"\n'
BROWSER_GATE = """[[ci.godot.gates]]
name="browser-caps"
kind="capability"
setup="browser"
virtual_display=true
timeout_minutes=12
[ci.godot.gates.entry]
runner="python"
path="tests/caps.py"
"""
RECOVERY_GATE = """[[ci.godot.gates]]
name="recovery"
kind="capability"
runner="ubuntu-22.04"
timeout_minutes=15
[ci.godot.gates.entry]
runner="python"
path="tests/recover.py"
"""
# A written assertion owned by a gate on another image: it leaves the main run with its gate.
OLD_IMAGE_TEST = """[[tests.e2e]]
runner="python"
path="tests/old_image.py"
[[ci.godot.gates]]
name="old-browser"
runner="ubuntu-22.04"
tests=["tests/old_image.py"]
timeout_minutes=20
"""
EXTRA_TEST = '[[tests.e2e]]\nrunner="python"\npath="tests/shot.py"\n'


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'tests').mkdir()
        for name in ['unit.gd', 'caps.py', 'recover.py', 'shot.py', 'old_image.py', 'browser.py']:
            (self.root / 'tests' / name).write_text('x\n')

    def write(self, text):
        (self.root / 'cookbook.toml').write_text(text)
        return configuration.load(self.root)

    def files(self, text):
        self.write(text)
        return generate.workflow_files(self.root, REVISION)


def job(name, runner, timeout, build, test, *, gate=None, condition=None, cookbook=REVISION, source='${{ github.sha }}',
        candidate='github.event.pull_request.head.sha || github.sha'):
    """Literal expected text of one validation job."""
    pin = '@' + REVISION
    text = '  ' + ('build-test' if gate is None else 'build-test-' + gate[0]) + ':\n    name: ' + name + '\n'
    if condition:
        text += '    if: ' + condition + '\n'
    text += '    runs-on: ' + runner + '\n    timeout-minutes: ' + str(timeout) + '\n'
    text += '    permissions:\n      contents: read\n      actions: read\n      pull-requests: read\n'
    text += "    env:\n      COOKBOOK_VALIDATION_TIMEOUT: '30'\n      COOKBOOK_CANDIDATE_COMMIT: ${{ " + candidate + ' }}\n'
    if gate:
        text += '      COOKBOOK_GATE_RUNNER: ' + gate[1] + '\n'
    text += '    steps:\n      - name: Prepare\n        timeout-minutes: 20\n        uses: nicodes/cookbook/prepare' + pin + '\n'
    text += '        with:\n          cookbook-ref: ' + cookbook + '\n          source: ' + source + '\n'
    text += '      - name: Build\n        timeout-minutes: ' + str(build) + '\n        uses: nicodes/cookbook/build' + pin + '\n'
    text += '      - name: Test\n        timeout-minutes: ' + str(test) + '\n        uses: nicodes/cookbook/test' + pin + '\n'
    text += ('      - name: Finish\n        if: always()\n        timeout-minutes: 5\n        uses: nicodes/cookbook/finish' + pin
             + '\n        with:\n          status: ${{ job.status }}\n')
    return text


HEADER = """# Generated cookbook caller. Change recipe data and regenerate.
name: Build and test
run-name: "Build and test · ${{ github.event.pull_request.number && format('PR #{0}', github.event.pull_request.number) || github.ref_name }}"
'on':
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
  merge_group:
    types: [checks_requested]
permissions: {}
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true
jobs:
"""


class Generation(Fixture):
    def test_an_in_process_gate_stays_in_the_one_job_and_adds_its_own_budget(self):
        files = self.files(BASE + UNIT + BROWSER_GATE)
        self.assertEqual(files, {'.github/workflows/build-and-test.yml':
                                 HEADER + job('Build and test', 'ubuntu-24.04', 92, 25, 67)})
        self.assertEqual(single_job_workflows.source_checks(self.write(BASE + UNIT + BROWSER_GATE)), ['Build and test'])

    def test_a_gate_on_another_runner_gets_exactly_one_extra_job_named_for_it(self):
        text = self.files(BASE + UNIT + BROWSER_GATE + RECOVERY_GATE)['.github/workflows/build-and-test.yml']
        self.assertEqual(text, HEADER + job('Build and test', 'ubuntu-24.04', 92, 25, 67)
                         + job('Build and test (recovery)', 'ubuntu-22.04', 60, 25, 15, gate=('recovery', 'ubuntu-22.04')))
        self.assertEqual(single_job_workflows.source_checks(self.write(BASE + UNIT + RECOVERY_GATE)),
                         ['Build and test', 'Build and test (recovery)'])

    def test_gates_sharing_a_foreign_runner_share_one_job_named_for_the_runner(self):
        second = RECOVERY_GATE.replace('recovery', 'upgrade').replace('recover.py', 'shot.py')
        config = self.write(BASE + UNIT + RECOVERY_GATE + second)
        self.assertEqual([row['check'] for row in single_job_workflows.gate_jobs(config)], ['Build and test (ubuntu-22.04)'])
        jobs = read_yaml(generate.workflow_files(self.root, REVISION)['.github/workflows/build-and-test.yml'])['jobs']
        self.assertEqual(set(jobs), {'build-test', 'build-test-ubuntu-22-04'})
        self.assertEqual(jobs['build-test-ubuntu-22-04']['timeout-minutes'], 75)  # 20 prepare + 25 build + 15 + 15
        self.assertEqual(jobs['build-test-ubuntu-22-04']['steps'][2]['timeout-minutes'], 30)

    def test_a_written_assertion_on_another_runner_leaves_the_main_run(self):
        config = self.write(BASE + UNIT + OLD_IMAGE_TEST + EXTRA_TEST)
        union = godot_ci.single_union(config, self.root)
        self.assertEqual(list(union['gate_jobs']), ['ubuntu-22.04'])
        self.assertEqual(len(union['gate_jobs']['ubuntu-22.04']), 1)
        self.assertEqual(len(union['main']), 2)
        self.assertFalse(set(union['main']) & set(union['gate_jobs']['ubuntu-22.04']))

    def test_without_the_flag_the_legacy_matrix_workflow_is_unchanged_and_has_no_gate_jobs(self):
        legacy = BASE.replace('single_job=true\n', '') + UNIT + BROWSER_GATE + RECOVERY_GATE
        explicit = BASE.replace('single_job=true', 'single_job=false') + UNIT + BROWSER_GATE + RECOVERY_GATE
        before = self.files(legacy)
        self.assertEqual(self.files(explicit), before)
        text = before['.github/workflows/build-and-test.yml']
        self.assertNotIn('COOKBOOK_GATE_RUNNER', text)
        self.assertNotIn('build-test-recovery', text)
        self.assertIn('build-and-test-godot.yml', text)

    def test_the_dependency_policy_must_require_every_generated_check_and_prints_the_list(self):
        self.write(BASE + UNIT + RECOVERY_GATE)
        policy = self.root / '.github/dependency-policy.toml'
        policy.parent.mkdir()
        for checks in ['["Build and test"]', '["Build and test (recovery)"]', '[]']:
            policy.write_text('version=2\nci_workflow="build-and-test.yml"\nrequired_checks=' + checks
                              + '\ndispatch_workflow="build-and-test.yml"\nmode="review"\n')
            with self.subTest(checks=checks), self.assertRaisesRegex(
                    ValueError, r'must require Build and test, Build and test \(recovery\) and not Build and test / Test'):
                generate.workflow_files(self.root, REVISION)
        policy.write_text('version=2\nci_workflow="build-and-test.yml"\nrequired_checks=["Build and test", "Build and test (recovery)"]\n'
                          'dispatch_workflow="build-and-test.yml"\nmode="review"\n')
        self.assertIn('build-test-recovery:', generate.workflow_files(self.root, REVISION)['.github/workflows/build-and-test.yml'])

    def test_production_gates_do_not_change_build_and_test(self):
        production = """[[ci.godot.gates]]
name="public"
kind="production"
timeout_minutes=10
[ci.godot.gates.entry]
runner="python"
path="tests/caps.py"
"""
        text = self.files(BASE + UNIT + production)['.github/workflows/build-and-test.yml']
        self.assertEqual(text, HEADER + job('Build and test', 'ubuntu-24.04', 80, 25, 55))


class Deploy(Fixture):
    def deploy(self, text):
        write_source(self.root / 'delivery.toml', dict(
            version=1, project='demo', komizo_ref='c' * 40, publish_runner='ubuntu-24.04', publish_timeout=15,
            deploy_runner='ubuntu-24.04', deploy_timeout=15, combined=True, health_urls=['https://example.test/readyz'],
            notification_url='https://example.test/'))
        return self.files(text + '[delivery]\ndefinition="delivery.toml"\n')['.github/workflows/deploy.yml']

    def test_a_different_runner_gate_keeps_its_main_run_before_the_deliver_job_publishes(self):
        text = self.deploy(BASE + UNIT + BROWSER_GATE + RECOVERY_GATE)
        jobs = read_yaml(text)['jobs']
        self.assertEqual(list(jobs), ['build-test-recovery', 'report', 'deploy'])
        gate = jobs['build-test-recovery']
        self.assertEqual(gate['name'], 'Build and test (recovery)')
        self.assertEqual(gate['runs-on'], 'ubuntu-22.04')
        self.assertEqual(gate['env']['COOKBOOK_GATE_RUNNER'], 'ubuntu-22.04')
        self.assertEqual(gate['if'], jobs['deploy']['if'])
        self.assertEqual(jobs['deploy']['needs'], ['build-test-recovery'])
        self.assertEqual(jobs['report']['needs'], ['build-test-recovery'])
        self.assertIn("contains(needs.*.result, 'failure')", jobs['report']['if'])
        self.assertTrue(jobs['report']['uses'].startswith('nicodes/komizo-actions/.github/workflows/report-failure.yml@'))
        self.assertEqual(gate['steps'][0]['with']['source'], '${{ inputs.source || github.sha }}')
        self.assertEqual(gate['env']['COOKBOOK_CANDIDATE_COMMIT'], '${{ inputs.source || github.sha }}')

    def test_in_process_gates_alone_leave_deploy_one_caller_job(self):
        jobs = read_yaml(self.deploy(BASE + UNIT + BROWSER_GATE))['jobs']
        self.assertEqual(list(jobs), ['deploy'])
        self.assertNotIn('needs', jobs['deploy'])
        # the deliver job's budget covers build, the ordinary groups and the in-process gate
        self.assertEqual(json.loads(jobs['deploy']['with']['step-timeouts'])['test'], 67)

    def test_a_disabled_failure_report_adds_no_report_job(self):
        self.write(BASE + UNIT + RECOVERY_GATE + '[delivery]\ndefinition="delivery.toml"\n')
        write_source(self.root / 'delivery.toml', dict(
            version=1, project='demo', komizo_ref='c' * 40, publish_runner='ubuntu-24.04', publish_timeout=15,
            deploy_runner='ubuntu-24.04', deploy_timeout=15, combined=True, report=False,
            health_urls=['https://example.test/readyz'], notification_url='https://example.test/'))
        jobs = read_yaml(generate.workflow_files(self.root, REVISION)['.github/workflows/deploy.yml'])['jobs']
        self.assertEqual(list(jobs), ['build-test-recovery', 'deploy'])


class InProcess(Fixture):
    def setUp(self):
        super().setUp()
        self.config = self.write(BASE + UNIT + BROWSER_GATE + RECOVERY_GATE + EXTRA_TEST)
        self.order = []
        patchers = [
            patch.object(godot_ci, 'setup_worker', side_effect=self.setup_worker),
            patch.object(godot_game, 'native_tests', side_effect=self.native_tests),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        saved = patch.dict(os.environ)
        saved.start()
        self.addCleanup(saved.stop)

    def setup_worker(self, root, config, environment, row, shared=False):
        self.assertTrue(shared)
        self.order.append(('setup', row['name']))
        if row.get('setup') == 'browser':
            environment['BROWSER_EXECUTABLE'] = '/browser'
            os.environ['BROWSER_RUNTIME_IMAGE'] = ''
        self.assertEqual(environment['COOKBOOK_GODOT_ENTRY'], row['id'])

    def native_tests(self, root, config, environment, suite):
        self.order.append(('run', suite, environment['COOKBOOK_GODOT_ENTRY'], environment.get('BROWSER_EXECUTABLE')))
        self.assertEqual(os.environ.get('BROWSER_RUNTIME_IMAGE'), '' if environment.get('BROWSER_EXECUTABLE') else None)

    def test_each_gate_is_set_up_before_it_runs_and_only_then_in_its_own_group(self):
        record = self.root / godot_ci.SINGLE_RECORD
        godot_ci.run_single_gates(self.root, self.config, {}, 'lint', 'main')
        self.assertEqual(self.order, [])
        godot_ci.run_single_gates(self.root, self.config, {}, 'security', 'main')
        self.assertEqual(self.order, [('setup', 'browser-caps'), ('run', 'security', 'gate-browser-caps', '/browser')])
        self.assertEqual(list(json.loads(record.read_text())), ['gate-browser-caps'])

    def test_a_godot_setup_gate_reuses_the_jobs_engine_instead_of_installing_it_again(self):
        gate = BROWSER_GATE.replace('setup="browser"', 'setup="godot"').replace('virtual_display=true\n', '')
        config = self.write(BASE + UNIT + gate)
        with patch.object(godot_ci, 'setup_worker') as setup, patch.object(godot_game, 'native_tests'):
            godot_ci.run_single_gates(self.root, config, {}, 'security', 'main')
        self.assertEqual(setup.call_args.args[3]['setup'], 'native')
        self.assertIs(setup.call_args.kwargs['shared'], True)
        self.assertEqual(setup.call_args.args[3]['id'], 'gate-browser-caps')

    def test_the_process_environment_is_restored_after_every_gate(self):
        os.environ.pop('BROWSER_RUNTIME_IMAGE', None)
        godot_ci.run_single_gates(self.root, self.config, {}, 'security', 'main')
        self.assertNotIn('BROWSER_RUNTIME_IMAGE', os.environ)

    def test_a_gate_job_runs_only_its_runners_gates_and_never_the_main_ones(self):
        godot_ci.run_single_gates(self.root, self.config, {}, 'security', 'gates:ubuntu-22.04')
        self.assertEqual(self.order, [('setup', 'recovery'), ('run', 'security', 'gate-recovery', None)])

    def test_a_failing_gate_stops_the_group_and_is_not_recorded(self):
        self.native_tests = MagicMock(side_effect=ValueError('gate failed'))
        with patch.object(godot_game, 'native_tests', self.native_tests), self.assertRaisesRegex(ValueError, 'gate failed'):
            godot_ci.run_single_gates(self.root, self.config, {}, 'security', 'main')
        self.assertFalse((self.root / godot_ci.SINGLE_RECORD).exists())

    def test_coverage_requires_every_gate_of_the_job_to_have_run(self):
        with self.assertRaisesRegex(ValueError, 'did not all run'):
            godot_ci.single_coverage(self.root, self.config, 'main')
        godot_ci.run_single_gates(self.root, self.config, {}, 'security', 'main')
        coverage = godot_ci.single_coverage(self.root, self.config, 'main')
        self.assertEqual(list(coverage['gate_cases']), ['gate-browser-caps'])
        self.assertEqual(list(coverage['other_jobs']), ['ubuntu-22.04'])
        self.assertEqual(coverage['scope'], 'main')
        with self.assertRaisesRegex(ValueError, 'did not all run'):
            godot_ci.single_coverage(self.root, self.config, 'gates:ubuntu-22.04')

    def test_the_main_selection_drops_only_cases_that_another_place_runs(self):
        env = {godot_ci.SINGLE: 'main'}
        config = self.write(BASE + UNIT + OLD_IMAGE_TEST + EXTRA_TEST)
        names = [entry['path'] for entry in godot_ci.selected_entries(self.root, config, env, 'e2e')]
        self.assertEqual(names, ['tests/shot.py'])
        self.assertEqual([entry['path'] for entry in godot_ci.selected_entries(self.root, config, {}, 'e2e')],
                         ['tests/old_image.py', 'tests/shot.py'])

    def test_the_union_check_refuses_overlap_and_unknown_scopes(self):
        with self.assertRaisesRegex(ValueError, 'unknown one-job Godot gate scope'):
            godot_ci.single_scope({godot_ci.SINGLE: 'gates:windows-latest'})
        self.assertIsNone(godot_ci.single_scope({}))
        with patch.object(godot_ci, 'single_split', return_value=([{'cases': ['a']}], [{'cases': ['a']}])), \
                patch.object(godot_ci, 'plan', return_value={'written_case_inventory': ['a'], 'gates': {'include': []}}), \
                self.assertRaisesRegex(ValueError, 'exact disjoint union'):
            godot_ci.single_union(self.config)


BROWSER_TEST = """[components.browser]
runner="tests/browser.py"
reports=".artifacts/browser-reports"
cli_tool="github:owner/cli"
package="tests/pw"
browsers=["chromium"]
[[tests.e2e]]
runner="godot-browser"
path="tests/browser.py"
"""


class SharedSetup(Fixture):
    def test_a_gate_in_a_one_job_run_does_not_repeat_the_jobs_own_setup(self):
        (self.root / 'mise.toml').write_text('[tools]\npython="3.13.11"\nnode="26.7.0"\n')
        config = self.write(BASE + UNIT + BROWSER_GATE.replace('virtual_display=true\n', ''))
        row = godot_ci.single_split(config)[0][0]
        calls = []
        with patch.object(godot_game, 'run', side_effect=lambda argv, *a, **k: calls.append(argv[0] + ' ' + argv[1])), \
                patch.object(godot_game.komizo_sdk, 'setup', side_effect=lambda *a: calls.append('komizo')), \
                patch.object(godot_game.workload_fixture, 'setup', side_effect=lambda *a: calls.append('fixture')), \
                patch('node_component.install', side_effect=lambda *a: calls.append('npm')), \
                patch('playwright_sdk.install', side_effect=lambda *a: calls.append('playwright')):
            godot_ci.setup_worker(self.root, config, {}, row, shared=True)
            shared = list(calls)
            calls.clear()
            godot_ci.setup_worker(self.root, config, {}, row)
        self.assertNotIn('komizo', shared)
        self.assertNotIn('fixture', shared)
        self.assertNotIn('npm', shared)
        self.assertIn('mise install', shared)  # the gate's own tools are still installed
        self.assertIn('playwright', shared)  # and its own browser
        self.assertTrue({'komizo', 'fixture', 'npm'} <= set(calls))


class BrowserEvidence(Fixture):
    def test_only_a_job_that_runs_a_browser_producer_must_leave_browser_reports(self):
        # Every browser test is owned by a gate on another image.
        browser_gate = BROWSER_TEST.replace('[[tests.e2e]]\nrunner="godot-browser"\npath="tests/browser.py"\n',
            '[[tests.e2e]]\nrunner="godot-browser"\npath="tests/browser.py"\n[[ci.godot.gates]]\nname="old-browser"\n'
            'runner="ubuntu-22.04"\ntests=["tests/browser.py"]\ntimeout_minutes=20\n')
        config = self.write(BASE + UNIT + browser_gate)
        self.assertFalse(godot_ci.single_browser_evidence(config, 'main'))
        self.assertTrue(godot_ci.single_browser_evidence(config, 'gates:ubuntu-22.04'))
        self.assertTrue(godot_ci.single_browser_evidence(config, None))

    def test_a_main_job_that_runs_its_own_browser_tests_requires_the_reports(self):
        config = self.write(BASE + UNIT + BROWSER_TEST)
        self.assertTrue(godot_ci.single_browser_evidence(config, 'main'))

    def test_the_evidence_listing_follows_the_scope_of_the_job(self):
        browser_gate = BROWSER_TEST + ('[[ci.godot.gates]]\nname="old-browser"\nrunner="ubuntu-22.04"\n'
            'tests=["tests/browser.py"]\ntimeout_minutes=20\n')
        config = self.write(BASE + UNIT + browser_gate)
        (self.root / 'tests/browser.py').write_text('x\n')
        with patch.dict(os.environ, {godot_ci.SINGLE: 'main'}):
            self.assertEqual(godot_game.evidence_paths(self.root, config), [])  # no reports directory, none required
        with patch.dict(os.environ, {godot_ci.SINGLE: 'gates:ubuntu-22.04'}), self.assertRaises(ValueError):
            godot_game.evidence_paths(self.root, config)  # the gate job's reports must exist
        reports = self.root / '.artifacts/browser-reports'
        reports.mkdir(parents=True)
        (reports / 'run.json').write_text('{}')
        with patch.dict(os.environ, {godot_ci.SINGLE: 'gates:ubuntu-22.04'}):
            self.assertEqual(godot_game.evidence_paths(self.root, config), ['.artifacts/browser-reports/run.json'])
        with patch.dict(os.environ, {godot_ci.SINGLE: 'main'}):
            self.assertEqual(godot_game.evidence_paths(self.root, config), ['.artifacts/browser-reports/run.json'])


class Phases(Fixture):
    """ci.run_single_job wires the gate scope into the real phase flow."""

    def setUp(self):
        super().setUp()
        self.config = self.write(BASE + UNIT + BROWSER_GATE + RECOVERY_GATE)
        self.calls = []
        module = MagicMock(spec=['artifact_paths'])
        module.artifact_paths.return_value = ['dist']
        for target, options in [
                ('ci.load', {'return_value': self.config}),
                ('ci.identity', {'return_value': 'tree'}),
                ('ci.recipe.configure_environment', {}),
                ('ci.recipe.adapter', {'return_value': module}),
                ('ci.recipe.execution_test_suites', {'return_value': ['lint', 'unit', 'security']}),
                ('ci.proof', {'side_effect': self.proof}),
                ('ci.relative', {'side_effect': lambda root, name, **kwargs: root / name}),
                ('ci.subprocess.run', {'side_effect': self.dispatch})]:
            patcher = patch(target, **options)
            patcher.start()
            self.addCleanup(patcher.stop)
        saved = patch.dict(os.environ)
        saved.start()
        self.addCleanup(saved.stop)
        for name in ['COOKBOOK_GODOT_ENTRY', 'COOKBOOK_GODOT_HOST', 'GITHUB_OUTPUT', 'COOKBOOK_GATE_RUNNER', godot_ci.SINGLE]:
            os.environ.pop(name, None)

    def proof(self, *args, **kwargs):
        self.calls.append(('proof', kwargs))
        return {'status': 'passed', **kwargs}

    def dispatch(self, command, **kwargs):
        self.calls.append((command[-1], kwargs['env'].get(godot_ci.SINGLE)))
        if command[-1] == 'test':
            # What the dispatched test phase leaves behind: every in-process gate of this job ran.
            scope = kwargs['env'][godot_ci.SINGLE]
            record = self.root / godot_ci.SINGLE_RECORD
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text(json.dumps({row['id']: row['cases'] for row in godot_ci.single_gate_rows(self.config, scope)}))

    def receipt(self):
        return json.loads((self.root / '.artifacts/cookbook-validation.json').read_text())

    def test_the_main_job_proof_records_single_job_coverage_and_the_gates_it_ran(self):
        ci.run_single_job(self.root, 'full', 'test', SOURCE, REVISION)
        name, options = [call for call in self.calls if call[0] == 'proof'][0]
        self.assertEqual((options['coverage'], options['imported_groups']), ('single-job', []))
        receipt = self.receipt()
        self.assertEqual(receipt['coverage'], 'single-job')
        self.assertEqual(list(receipt['godot_single_job']['gate_cases']), ['gate-browser-caps'])
        self.assertEqual(list(receipt['godot_single_job']['other_jobs']), ['ubuntu-22.04'])

    def test_a_gate_job_proof_covers_only_its_own_groups_and_says_so(self):
        os.environ['COOKBOOK_GATE_RUNNER'] = 'ubuntu-22.04'
        ci.run_single_job(self.root, 'full', 'test', SOURCE, REVISION)
        self.assertIn(('test', 'gates:ubuntu-22.04'), self.calls)
        options = [call for call in self.calls if call[0] == 'proof'][0][1]
        self.assertEqual(options['coverage'], 'single-job-gates')
        self.assertEqual(options['imported_groups'], ['lint', 'unit'])
        receipt = self.receipt()
        self.assertEqual(receipt['godot_single_job']['scope'], 'gates:ubuntu-22.04')
        self.assertEqual(list(receipt['godot_single_job']['gate_cases']), ['gate-recovery'])

    def test_every_phase_carries_the_scope_and_a_stale_gate_record_never_counts(self):
        stale = self.root / godot_ci.SINGLE_RECORD
        stale.parent.mkdir(parents=True)
        stale.write_text('{"gate-recovery": []}')
        for phase in ['setup', 'build']:
            ci.run_single_job(self.root, 'full', phase, SOURCE, REVISION)
        self.assertEqual([call for call in self.calls if call[0] in {'setup', 'build'}], [('setup', 'main'), ('build', 'main')])
        self.assertFalse(stale.exists())

    def test_a_gate_job_needs_a_gate_on_its_runner_and_a_godot_recipe(self):
        os.environ['COOKBOOK_GATE_RUNNER'] = 'blacksmith-4vcpu-ubuntu-2204'
        with self.assertRaisesRegex(ValueError, 'no gate is declared for the runner'):
            ci.run_single_job(self.root, 'full', 'test', SOURCE, REVISION)
        os.environ['COOKBOOK_GATE_RUNNER'] = 'ubuntu-22.04'
        with patch('ci.orchestration.model', return_value={'route': 'host', 'recipe': 'react-native', 'test_groups': []}), \
                self.assertRaisesRegex(ValueError, 'belongs to a Godot recipe'):
            ci.run_single_job(self.root, 'full', 'test', SOURCE, REVISION)

    def test_capability_production_and_worker_bound_gates_are_no_longer_refused(self):
        for gates in [BROWSER_GATE, RECOVERY_GATE]:
            self.config = self.write(BASE + UNIT + gates)
            with patch('ci.load', return_value=self.config):
                ci.run_single_job(self.root, 'full', 'build', SOURCE, REVISION)
        self.assertEqual([call[0] for call in self.calls], ['build', 'build'])


if __name__ == '__main__':
    unittest.main()
