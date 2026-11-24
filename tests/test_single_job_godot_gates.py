from source_fixture import SDK_ROOT, write_source
"""Godot gates in one-job validation: in-process gates, a job per different runner, and their coverage proof."""
import json
import os
import subprocess
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
        candidate='github.event.pull_request.head.sha || github.sha', role='whole'):
    """Literal expected text of one validation job.

    role is whole (build and test in one job), build (builds and packs for the others) or consume (imports that build,
    then tests; gate jobs and the main job of a split run).
    """
    pin = '@' + REVISION
    ident = 'build' if role == 'build' else 'build-test' if gate is None else 'build-test-' + gate[0]
    text = '  ' + ident + ':\n    name: ' + name + '\n'
    if role == 'consume':
        text += '    needs: build\n'
    if condition:
        text += '    if: ' + condition + '\n'
    text += '    runs-on: ' + runner + '\n    timeout-minutes: ' + str(timeout) + '\n'
    if role == 'build':
        text += '    outputs:\n      digest: ${{ steps.pack.outputs.digest }}\n'
    text += '    permissions:\n      contents: read\n      actions: read\n      pull-requests: read\n'
    text += "    env:\n      COOKBOOK_VALIDATION_TIMEOUT: '30'\n      COOKBOOK_CANDIDATE_COMMIT: ${{ " + candidate + ' }}\n'
    if gate:
        text += '      COOKBOOK_GATE_RUNNER: ' + gate[1] + '\n'
    text += '    steps:\n      - name: Prepare\n        timeout-minutes: 20\n        uses: nicodes/cookbook/prepare' + pin + '\n'
    text += '        with:\n          cookbook-ref: ' + cookbook + '\n          source: ' + source + '\n'
    if role == 'consume':
        text += ('      - name: Import build\n        timeout-minutes: ' + str(build) + '\n        uses: nicodes/cookbook/import-build' + pin
                 + '\n        with:\n          digest: ${{ needs.build.outputs.digest }}\n')
    else:
        text += '      - name: Build\n        timeout-minutes: ' + str(build) + '\n        uses: nicodes/cookbook/build' + pin + '\n'
    if role == 'build':
        text += ('      - name: Pack build outputs\n        id: pack\n        timeout-minutes: 15\n'
                 '        uses: nicodes/cookbook/export-build' + pin + '\n')
    else:
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
        # build once on the recipe's runner; the main job and the gate job import that build instead of making it
        self.assertEqual(text, HEADER + job('Build and test (build)', 'ubuntu-24.04', 60, 25, 0, role='build')
                         + job('Build and test', 'ubuntu-24.04', 82, 15, 67, role='consume')
                         + job('Build and test (recovery)', 'ubuntu-22.04', 50, 15, 15, gate=('recovery', 'ubuntu-22.04'),
                               role='consume'))
        self.assertEqual(single_job_workflows.source_checks(self.write(BASE + UNIT + RECOVERY_GATE)),
                         ['Build and test', 'Build and test (build)', 'Build and test (recovery)'])

    def test_a_recipe_without_a_different_runner_gate_never_splits_its_build(self):
        for gates in ['', BROWSER_GATE]:
            text = self.files(BASE + UNIT + gates)['.github/workflows/build-and-test.yml']
            for word in ['needs:', 'import-build', 'export-build', 'Pack build outputs', 'outputs:', 'build-test-']:
                self.assertNotIn(word, text)
            self.assertEqual(text.count('uses: nicodes/cookbook/build@'), 1)
        self.assertEqual(single_job_workflows.source_checks(self.write(BASE + UNIT)), ['Build and test'])

    def test_gates_sharing_a_foreign_runner_share_one_job_named_for_the_runner(self):
        second = RECOVERY_GATE.replace('recovery', 'upgrade').replace('recover.py', 'shot.py')
        config = self.write(BASE + UNIT + RECOVERY_GATE + second)
        self.assertEqual([row['check'] for row in single_job_workflows.gate_jobs(config)], ['Build and test (ubuntu-22.04)'])
        jobs = read_yaml(generate.workflow_files(self.root, REVISION)['.github/workflows/build-and-test.yml'])['jobs']
        self.assertEqual(set(jobs), {'build', 'build-test', 'build-test-ubuntu-22-04'})
        self.assertEqual(jobs['build-test-ubuntu-22-04']['timeout-minutes'], 65)  # 20 prepare + 15 import + 15 + 15
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
        for checks in ['["Build and test"]', '["Build and test (recovery)"]', '["Build and test", "Build and test (recovery)"]', '[]']:
            policy.write_text('version=2\nci_workflow="build-and-test.yml"\nrequired_checks=' + checks
                              + '\ndispatch_workflow="build-and-test.yml"\nmode="review"\n')
            with self.subTest(checks=checks), self.assertRaisesRegex(
                    ValueError, r'must require Build and test, Build and test \(build\), Build and test \(recovery\) and not Build and test / Test'):
                generate.workflow_files(self.root, REVISION)
        policy.write_text('version=2\nci_workflow="build-and-test.yml"\nrequired_checks=["Build and test", "Build and test (build)", "Build and test (recovery)"]\n'
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
        self.assertEqual(list(jobs), ['build', 'build-test-recovery', 'report', 'deploy'])
        build = jobs['build']
        self.assertEqual(build['name'], 'Build and test (build)')
        self.assertEqual(build['runs-on'], 'ubuntu-24.04')  # the recipe's own runner, not a small hosted one
        self.assertEqual(build['if'], jobs['deploy']['if'])
        self.assertNotIn('needs', build)
        self.assertEqual(build['outputs'], {'digest': '${{ steps.pack.outputs.digest }}'})
        self.assertEqual([step.get('uses', '').split('@')[0] for step in build['steps']],
                         ['nicodes/cookbook/prepare', 'nicodes/cookbook/build', 'nicodes/cookbook/export-build', 'nicodes/cookbook/finish'])
        gate = jobs['build-test-recovery']
        self.assertEqual(gate['needs'], 'build')
        self.assertEqual([step.get('uses', '').split('@')[0] for step in gate['steps']],
                         ['nicodes/cookbook/prepare', 'nicodes/cookbook/import-build', 'nicodes/cookbook/test', 'nicodes/cookbook/finish'])
        self.assertEqual(gate['steps'][1]['with'], {'digest': '${{ needs.build.outputs.digest }}'})
        self.assertEqual(gate['name'], 'Build and test (recovery)')
        self.assertEqual(gate['runs-on'], 'ubuntu-22.04')
        self.assertEqual(gate['env']['COOKBOOK_GATE_RUNNER'], 'ubuntu-22.04')
        self.assertEqual(gate['if'], jobs['deploy']['if'])
        self.assertEqual(jobs['deploy']['needs'], ['build-test-recovery'])
        self.assertEqual(jobs['report']['needs'], ['build', 'build-test-recovery'])
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
        self.assertEqual(list(jobs), ['build', 'build-test-recovery', 'deploy'])


class DeployValidationDeadline(Fixture):
    """The deploy workflow's validation jobs carry the delivery policy's deadline, not the recipe's CI deadline."""
    TEXT = BASE.replace('single_job=true\n', 'single_job=true\ntimeout_minutes=45\n') + UNIT + RECOVERY_GATE

    def deploy(self, validation, text=None):
        policy = dict(version=1, project='demo', komizo_ref='c' * 40, publish_runner='ubuntu-24.04', publish_timeout=15,
                      deploy_runner='ubuntu-24.04', deploy_timeout=15, combined=True,
                      health_urls=['https://example.test/readyz'], notification_url='https://example.test/')
        if validation:
            policy['validation_timeout_minutes'] = validation
        write_source(self.root / 'delivery.toml', policy)
        return self.files((text or self.TEXT) + '[delivery]\ndefinition="delivery.toml"\n')

    def test_every_validation_job_of_deploy_carries_the_delivery_deadline_and_pull_requests_keep_the_recipe_deadline(self):
        files = self.deploy(60)
        jobs = read_yaml(files['.github/workflows/deploy.yml'])['jobs']
        checked = [name for name, value in jobs.items() if 'env' in value]
        self.assertEqual(checked, ['build', 'build-test-recovery'])
        for name in checked:
            self.assertEqual(jobs[name]['env']['COOKBOOK_VALIDATION_TIMEOUT'], '60')
        pull = read_yaml(files['.github/workflows/build-and-test.yml'])['jobs']
        self.assertEqual({value['env']['COOKBOOK_VALIDATION_TIMEOUT'] for value in pull.values()}, {'45'})

    def test_only_the_deadline_value_changes_and_job_and_step_deadlines_stay_the_recipe_budgets(self):
        old = self.deploy(45)['.github/workflows/deploy.yml']
        new = self.deploy(60)['.github/workflows/deploy.yml']
        self.assertEqual(old.count("COOKBOOK_VALIDATION_TIMEOUT: '45'"), 2)
        self.assertEqual(old.replace("COOKBOOK_VALIDATION_TIMEOUT: '45'", "COOKBOOK_VALIDATION_TIMEOUT: '60'"), new)
        for name, value in read_yaml(new)['jobs'].items():
            self.assertEqual(value.get('timeout-minutes'), read_yaml(old)['jobs'][name].get('timeout-minutes'))

    def test_a_policy_without_its_own_deadline_leaves_the_recipe_deadline(self):
        jobs = read_yaml(self.deploy(None)['.github/workflows/deploy.yml'])['jobs']
        self.assertEqual(jobs['build']['env']['COOKBOOK_VALIDATION_TIMEOUT'], '45')

    def test_the_trusted_deploy_check_accepts_the_generated_deadline(self):
        files = self.deploy(60)
        jobs = read_yaml(files['.github/workflows/deploy.yml'])['jobs']

        def git(*args):
            return subprocess.check_output(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
                '-c', 'commit.gpgsign=false', '-C', str(self.root), *args], text=True, stderr=subprocess.DEVNULL).strip()
        git('init')
        git('add', '.')
        git('commit', '-qm', 'Source')
        source = git('rev-parse', 'HEAD')
        config = configuration.load(self.root)
        for name in ['build', 'build-test-recovery']:
            environment = dict(GITHUB_REPOSITORY='owner/demo', GITHUB_SHA=source, GITHUB_WORKFLOW_SHA=source,
                GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/deploy.yml@refs/heads/main', GITHUB_REF='refs/heads/main',
                GITHUB_EVENT_NAME='push', SOURCE_SHA=source,
                COOKBOOK_VALIDATION_TIMEOUT=jobs[name]['env']['COOKBOOK_VALIDATION_TIMEOUT'])
            godot_ci.plan(self.root, config, environment)
        with self.assertRaisesRegex(ValueError, 'differs from native delivery policy'):
            godot_ci.plan(self.root, config, {**environment, 'COOKBOOK_VALIDATION_TIMEOUT': '45'})


PRODUCTION = """[[node.packages]]
directory="tests/browser"
manager="npm"
[[ci.godot.gates]]
name="public"
kind="production"
setup="browser"
runner="blacksmith-4vcpu-ubuntu-2204"
timeout_minutes=15
packages=["tests/browser"]
browsers=["chromium"]
[ci.godot.gates.entry]
runner="node"
path="tests/browser/public.mjs"
"""
# nicodes/komizo-actions interface of verify-deployed-godot-production.yml (commit 82917b131363a3284a3c010a38a4121fdeaf362c (merged)):
# inputs cookbook-ref, komizo-ref, source, runner, timeout-minutes; admitted caller .github/workflows/verify-public-release.yml.
PUBLIC_RELEASE = """# Generated cookbook caller. Change recipe data and regenerate.
name: Verify public release
run-name: "Verify public release · ${{ github.event_name == 'workflow_run' && github.event.workflow_run.head_sha || github.ref_name }}"
'on':
  workflow_run:
    workflows: [Deploy]
    types: [completed]
    branches: [main]
  workflow_dispatch:
permissions: {}
concurrency:
  group: public-release-verification
  cancel-in-progress: false
jobs:
  verify:
    name: Verify public release
    if: github.ref == 'refs/heads/main' && ((github.event_name == 'workflow_run' && github.event.workflow_run.path == '.github/workflows/deploy.yml' && github.event.workflow_run.conclusion == 'success' && github.event.workflow_run.head_branch == 'main' && contains(fromJSON('["push","workflow_dispatch"]'), github.event.workflow_run.event) && github.event.workflow_run.head_repository.full_name == github.repository && github.event.workflow_run.repository.full_name == github.repository && github.event.workflow_run.head_sha == github.sha) || (github.event_name == 'workflow_dispatch'))
    uses: nicodes/komizo-actions/.github/workflows/verify-deployed-godot-production.yml@cccccccccccccccccccccccccccccccccccccccc
    permissions:
      contents: read
      actions: read
    with:
      cookbook-ref: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
      komizo-ref: cccccccccccccccccccccccccccccccccccccccc
      source: ${{ github.sha }}
      runner: blacksmith-4vcpu-ubuntu-2204
      timeout-minutes: 15
"""


class PublicRelease(Fixture):
    def setUp(self):
        super().setUp()
        (self.root / 'tests/browser').mkdir()
        (self.root / 'tests/browser/public.mjs').write_text('x\n')

    def files_with(self, text):
        write_source(self.root / 'delivery.toml', dict(
            version=1, project='demo', komizo_ref='c' * 40, publish_runner='ubuntu-24.04', publish_timeout=15,
            deploy_runner='ubuntu-24.04', deploy_timeout=15, combined=True, health_urls=['https://example.test/readyz'],
            notification_url='https://example.test/'))
        return self.files(text + '[delivery]\ndefinition="delivery.toml"\n')

    def test_a_production_gate_becomes_its_own_workflow_after_a_successful_deploy(self):
        files = self.files_with(BASE + UNIT + PRODUCTION)
        self.assertEqual(set(files), {'.github/workflows/build-and-test.yml', '.github/workflows/deploy.yml',
                                      '.github/workflows/verify-public-release.yml'})
        self.assertEqual(files['.github/workflows/verify-public-release.yml'], PUBLIC_RELEASE)

    def test_the_deploy_and_build_workflows_carry_no_production_job_or_runner_image(self):
        files = self.files_with(BASE + UNIT + PRODUCTION)
        for name in ['.github/workflows/build-and-test.yml', '.github/workflows/deploy.yml']:
            self.assertNotIn('blacksmith', files[name])
            self.assertNotIn('verify-godot-production', files[name])
        self.assertEqual(list(read_yaml(files['.github/workflows/deploy.yml'])['jobs']), ['deploy'])

    def test_the_generated_caller_is_triggered_only_by_a_deploy_identified_by_its_path(self):
        text = self.files_with(BASE + UNIT + PRODUCTION)['.github/workflows/verify-public-release.yml']
        workflow = read_yaml(text)
        self.assertEqual(workflow['on']['workflow_run'], {'workflows': ['Deploy'], 'types': ['completed'], 'branches': ['main']})
        self.assertIn("github.event.workflow_run.path == '.github/workflows/deploy.yml'", text)
        self.assertNotIn('workflow_run.name', text)
        self.assertEqual(workflow['jobs']['verify']['permissions'], {'contents': 'read', 'actions': 'read'})
        self.assertNotIn('schedule', workflow['on'])  # a production gate declares no schedule

    def test_the_gate_is_combined_with_an_in_process_gate_and_a_gate_job(self):
        files = self.files_with(BASE + UNIT + BROWSER_GATE + RECOVERY_GATE + PRODUCTION)
        self.assertEqual(list(read_yaml(files['.github/workflows/deploy.yml'])['jobs']), ['build', 'build-test-recovery', 'report', 'deploy'])
        self.assertIn('verify-public-release.yml', ''.join(files))

    def test_without_a_production_gate_there_is_no_verification_workflow(self):
        self.assertNotIn('.github/workflows/verify-public-release.yml', self.files_with(BASE + UNIT + BROWSER_GATE))


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


class ImportedReceipt(Fixture):
    """ci.proof accepts an imported build receipt only from the build job, differing from this job in nothing else."""

    CONTEXT = {'GITHUB_REPOSITORY': 'o/r', 'GITHUB_RUN_ID': '7', 'GITHUB_JOB': 'build-test-recovery', 'GITHUB_SHA': SOURCE}

    def prove(self, produced_by, **kwargs):
        provenance = {'source_commit': SOURCE, 'source_tree': 'tree', 'tracked_source_clean': True, 'input_identity': 'i',
                      'execution_context': {**self.CONTEXT, 'GITHUB_JOB': produced_by}}
        directory = self.root / '.artifacts/verification'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'build.json').write_text(json.dumps({'identity': 'i', 'artifacts': {}, 'provenance': provenance}))
        (directory / 'test.json').write_text(json.dumps({
            'source_commit': SOURCE, 'source_tree': 'tree', 'tracked_source_clean': True, 'tested_commit': SOURCE,
            'input_identity': 'i', 'execution_context': self.CONTEXT, 'test_receipts': {'unit': {}}}))
        verify = MagicMock()
        verify.execution_context.return_value = self.CONTEXT
        verify.identity.return_value = 'i'
        module = MagicMock(spec=['verification_scope'])
        with patch('ci.identity', return_value='tree'), patch('ci.verification', return_value=verify), \
                patch('ci.artifacts', return_value={}), patch('ci.recipe.adapter', return_value=module), \
                patch('ci.recipe.execution_test_suites', return_value=['unit']), \
                patch('ci.subprocess.check_output', return_value=b'x'), patch('ci.dependencies.binding', return_value={}):
            return ci.proof(self.root, SOURCE, REVISION, {'recipe': 'godot-game'}, **kwargs)

    def test_a_receipt_made_by_another_job_is_refused_unless_it_is_the_declared_build_job(self):
        with self.assertRaisesRegex(ValueError, 'another source or execution'):
            self.prove('build')
        with self.assertRaisesRegex(ValueError, 'another source or execution'):
            self.prove('build-test', build_job='build')
        self.assertEqual(self.prove('build', build_job='build')['status'], 'passed')

    def test_the_test_receipt_must_always_be_this_jobs_own(self):
        self.prove('build-test-recovery')
        path = self.root / '.artifacts/verification/test.json'
        record = json.loads(path.read_text())
        record['execution_context'] = {**self.CONTEXT, 'GITHUB_JOB': 'build'}
        path.write_text(json.dumps(record))
        verify = MagicMock()
        verify.execution_context.return_value = self.CONTEXT
        with patch('ci.identity', return_value='tree'), patch('ci.verification', return_value=verify), \
                self.assertRaisesRegex(ValueError, 'another source or execution'):
            ci.proof(self.root, SOURCE, REVISION, {}, build_job='build')


class BuildPacket(Fixture):
    """A job that imports the build verifies the downloaded packet against the digest its producer reported."""

    def setUp(self):
        super().setUp()
        self.config = self.write(BASE + UNIT + RECOVERY_GATE)
        self.packet = self.root / godot_ci.SINGLE_PACKET
        (self.packet / 'payload').mkdir(parents=True)
        (self.packet / 'godot-proof.json').write_text('{"build": {"artifacts": {"dist/game.pck": "' + 'e' * 64 + '"}}}\n')
        (self.packet / 'payload/dist').mkdir()
        (self.packet / 'payload/dist/game.pck').write_bytes(b'x' * 10)
        self.expected = godot_ci.digest(self.packet / 'godot-proof.json')
        self.proof = json.loads((self.packet / 'godot-proof.json').read_text())

    def run_import(self, expected, free=1 << 40, settings=None, scope=None):
        self.order = []
        with patch('godot_ci.declaration_entry', return_value={'id': 'build'}), \
                patch('godot_ci.packet', return_value=self.proof) as verified, \
                patch('godot_ci.import_build', side_effect=lambda *a: self.order.append('outputs')) as imported, \
                patch('godot_game.project', return_value=(self.root, settings or {})), \
                patch('godot_game.import_native', side_effect=lambda *a, **k: self.order.append('engine')) as engine, \
                patch('godot_ci.shutil.disk_usage', return_value=MagicMock(free=free)):
            try:
                godot_ci.single_import(self.root, self.config, SOURCE, REVISION, expected,
                                       {godot_ci.SINGLE: scope} if scope else {})
            finally:
                self.verified, self.imported, self.engine = verified, imported, engine

    def test_a_project_that_imports_at_build_time_is_imported_after_the_outputs_arrive(self):
        # What a job that built keeps in .godot (imported assets, script classes) is not in the packet.
        self.run_import(self.expected, settings={'extensions': ['x']}, scope='main')
        self.assertEqual(self.order, ['outputs', 'engine'])
        self.assertTrue(self.engine.call_args.kwargs['imported'])

    def test_a_project_the_setup_phase_already_imported_is_not_imported_again(self):
        self.run_import(self.expected, settings={}, scope='main')
        self.assertEqual(self.order, ['outputs'])

    def test_a_gate_job_imports_only_when_one_of_its_gates_runs_the_engine(self):
        build = {'extensions': ['x']}
        self.run_import(self.expected, settings=build, scope='gates:ubuntu-22.04')  # a capability program only
        self.assertEqual(self.order, ['outputs'])
        engine = RECOVERY_GATE.replace('kind="capability"', 'kind="capability"\nsetup="godot"')
        self.config = self.write(BASE + UNIT + engine)
        self.run_import(self.expected, settings=build, scope='gates:ubuntu-22.04')
        self.assertEqual(self.order, ['outputs', 'engine'])

    def test_a_matching_digest_verifies_the_packet_then_imports_and_marks_it(self):
        self.run_import(self.expected)
        self.verified.assert_called_once()
        self.imported.assert_called_once_with(self.root, self.config, self.packet, self.proof)
        self.assertEqual(json.loads((self.root / godot_ci.SINGLE_IMPORTED).read_text()), self.proof['build'])

    def test_a_packet_that_differs_from_the_reported_digest_is_refused_before_anything_is_read_or_copied(self):
        (self.packet / 'godot-proof.json').write_text('{"build": {"artifacts": {"dist/other": "' + 'f' * 64 + '"}}}\n')
        with self.assertRaisesRegex(ValueError, 'differs from the digest'):
            self.run_import(self.expected)
        self.verified.assert_not_called()
        self.imported.assert_not_called()
        self.assertFalse((self.root / godot_ci.SINGLE_IMPORTED).exists())

    def test_a_missing_or_malformed_digest_is_refused(self):
        for value in ['', None, 'abc', 'G' * 64, self.expected.upper()]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'requires the digest'):
                self.run_import(value)

    def test_too_little_free_disk_fails_early_and_says_how_much_is_needed(self):
        with self.assertRaisesRegex(ValueError, r'not enough free disk to import the build: need 1024 MiB .* have 5 MiB'):
            self.run_import(self.expected, free=5 << 20)
        self.imported.assert_not_called()

    def test_the_build_job_reports_the_proof_digest_through_its_output(self):
        stage = self.root / godot_ci.SINGLE_STAGE
        stage.mkdir(parents=True)
        (stage / 'godot-proof.json').write_text('{"p": 1}\n')
        output = self.root / 'github-output'
        with patch.dict(os.environ, {'GITHUB_JOB': 'build'}), \
                patch('godot_ci.declaration_entry', return_value={'id': 'build'}), \
                patch('godot_ci.read_json', return_value={}), \
                patch('godot_ci.collect_partial', return_value={}), \
                patch('godot_ci.export_packet', return_value=stage):
            value = godot_ci.single_pack(self.root, self.config, SOURCE, REVISION, str(output))
        self.assertEqual(output.read_text(), 'digest=' + godot_ci.digest(stage / 'godot-proof.json') + '\n')
        self.assertEqual(value, godot_ci.digest(stage / 'godot-proof.json'))
        with patch.dict(os.environ, {'GITHUB_JOB': 'build-test'}), self.assertRaisesRegex(ValueError, 'job named build'):
            godot_ci.single_pack(self.root, self.config, SOURCE, REVISION)


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

    def test_an_imported_build_receipt_is_accepted_as_the_build_jobs_and_a_local_one_as_this_jobs(self):
        ci.run_single_job(self.root, 'full', 'test', SOURCE, REVISION)
        self.assertIsNone([call for call in self.calls if call[0] == 'proof'][0][1]['build_job'])
        self.calls.clear()
        marker = self.root / godot_ci.SINGLE_IMPORTED
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('{}')
        os.environ['COOKBOOK_GATE_RUNNER'] = 'ubuntu-22.04'
        ci.run_single_job(self.root, 'full', 'test', SOURCE, REVISION)
        self.assertEqual([call for call in self.calls if call[0] == 'proof'][0][1]['build_job'], 'build')

    def test_the_pack_and_import_phases_only_stage_or_import_and_run_nothing_else(self):
        os.environ['GITHUB_OUTPUT'] = str(self.root / 'output')
        os.environ['COOKBOOK_BUILD_DIGEST'] = 'd' * 64
        with patch('godot_ci.single_pack') as pack, patch('godot_ci.single_import') as unpack:
            ci.run_single_job(self.root, 'full', 'pack', SOURCE, REVISION)
            ci.run_single_job(self.root, 'full', 'import', SOURCE, REVISION)
        pack.assert_called_once_with(self.root, self.config, SOURCE, REVISION, str(self.root / 'output'))
        unpack.assert_called_once_with(self.root, self.config, SOURCE, REVISION, 'd' * 64)
        self.assertEqual([call for call in self.calls if call[0] in {'setup', 'build', 'test'}], [])

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
