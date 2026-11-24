from source_fixture import SDK_ROOT
"""Rust CLI recipes in one-job mode: one literal job per platform, local phases, no combined check."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import test_native_ci

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import ci
import native_ci
import rust_cli  # noqa: F401  imported before subprocess is patched
import single_job_workflows

spec = importlib.util.spec_from_file_location('cookbook_generate_rust_cli', SDK_ROOT / 'cookbook/generate.py')
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)

PIN = 'a' * 40
SOURCE, TOOLS = 'c' * 40, 'd' * 40
CHECK = '.github/workflows/build-and-test.yml'

GOLDEN = """# Generated cookbook caller. Change recipe data and regenerate.
name: Build and test
run-name: "Build and test · ${{ github.event.pull_request.number && format('PR #{0}', github.event.pull_request.number) || github.ref_name }}"
'on':
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
  merge_group:
    types: [checks_requested]
  push:
    branches: [main]
    paths:
      - '.github/workflows/build-and-test.yml'
      - 'cookbook.toml'
      - 'mise.toml'
      - '**/Cargo.toml'
      - '**/Cargo.lock'
      - '**/.cargo/config.toml'
permissions: {}
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true
jobs:
  build-test:
    name: Build and test (${{ matrix.platform }}-${{ matrix.arch }})
    strategy:
      fail-fast: false
      matrix:
        include:
          - id: 1-Linux-x86_64
            runner: ubuntu-24.04
            platform: Linux
            arch: x86_64
            target: x86_64-unknown-linux-musl
            timeout_minutes: 60
          - id: 1-Darwin-arm64
            runner: macos-26
            platform: Darwin
            arch: arm64
            target: aarch64-apple-darwin
            timeout_minutes: 30
          - id: 1-Windows-x86_64
            runner: windows-2025
            platform: Windows
            arch: x86_64
            target: x86_64-pc-windows-msvc
            timeout_minutes: 30
    runs-on: ${{ matrix.runner }}
    timeout-minutes: ${{ matrix.timeout_minutes }}
    permissions:
      contents: read
      actions: read
      pull-requests: read
    env:
      COOKBOOK_VALIDATION_TIMEOUT: '30'
      COOKBOOK_CANDIDATE_COMMIT: ${{ github.event.pull_request.head.sha || github.sha }}
    steps:
      - name: Prepare
        timeout-minutes: 20
        uses: nicodes/cookbook/prepare@PIN
        with:
          cookbook-ref: PIN
          source: ${{ github.sha }}
          target: ${{ matrix.target }}
      - name: Build
        timeout-minutes: ${{ matrix.timeout_minutes }}
        uses: nicodes/cookbook/build@PIN
      - name: Test
        timeout-minutes: ${{ matrix.timeout_minutes }}
        uses: nicodes/cookbook/test@PIN
      - name: Finish
        if: always()
        timeout-minutes: 5
        uses: nicodes/cookbook/finish@PIN
        with:
          status: ${{ job.status }}
          entry: ${{ matrix.id }}
""".replace('PIN', PIN)

CHECKS = ['Build and test (Linux-x86_64)', 'Build and test (Darwin-arm64)', 'Build and test (Windows-x86_64)']


class Source(unittest.TestCase):
    """A three-platform Rust CLI source that has opted into one-job CI."""

    def setUp(self):
        fixture = test_native_ci.NativeMatrix()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root, self.config, self.targets = fixture.root, fixture.config, fixture.targets
        self.declaration = fixture.declaration
        self.config['version'] = 1
        self.config['package']['native_targets'] = [self.targets[0], self.targets[3], self.targets[4]]
        self.config['ci'] = {'single_job': True}

    def files(self):
        return generator.workflow_files(self.root, PIN, self.config)

    def policy(self, checks, dispatch='build-and-test.yml'):
        path = self.root / '.github/dependency-policy.toml'
        path.parent.mkdir(exist_ok=True)
        path.write_text('version=2\nci_workflow="build-and-test.yml"\nrequired_checks=' + json.dumps(checks)
                        + '\ndispatch_workflow="' + dispatch + '"\nmode="review"\n')

    def second_wave(self):
        self.config['package']['native_targets'] = self.targets[:4]
        self.config['components']['native'] = [
            {'name': 'host', 'role': 'host', 'component': 'cli', 'binary': 'host', 'output': '.artifacts/native-host/host_{platform}_{arch}'},
            {'name': 'cli', 'role': 'cli', 'component': 'cli', 'binary': 'example', 'output': '.artifacts/native-cli/example_{platform}_{arch}',
             'needs': [{'component': 'host', 'platform': 'Linux/x86_64', 'path_environment': 'HOST_AMD64'},
                       {'component': 'host', 'platform': 'Linux/arm64', 'path_environment': 'HOST_ARM64'}]}]


class Generation(Source):
    def test_three_platform_golden_has_a_literal_matrix_and_no_plan_job(self):
        files = self.files()
        self.assertEqual(files[CHECK], GOLDEN)
        self.assertEqual(set(files), {CHECK, '.github/workflows/security.yml'})
        jobs = GOLDEN.split('\njobs:\n')[1]
        self.assertEqual(re.findall(r'^  ([a-z-]+):$', jobs, flags=re.MULTILINE), ['build-test'])
        self.assertNotIn('needs:', jobs)
        self.assertNotIn('fromJSON', GOLDEN)
        self.assertIn('      fail-fast: false\n', jobs)

    def test_each_platform_gets_its_own_deadline_and_the_group_host_adds_the_ci_deadline(self):
        self.config['package']['native_targets'][0]['timeout_minutes'] = 45
        self.config['package']['native_targets'][1]['timeout_minutes'] = 12
        self.config['ci'] = {'single_job': True, 'timeout_minutes': 50}
        rows = {row['id']: row['timeout_minutes'] for row in single_job_workflows.platforms(self.config)}
        self.assertEqual(rows, {'1-Linux-x86_64': 95, '1-Darwin-arm64': 12, '1-Windows-x86_64': 30})
        self.config['ci']['timeout_minutes'] = 120
        self.config['package']['native_targets'][0]['timeout_minutes'] = 100
        self.assertEqual(single_job_workflows.platforms(self.config)[0]['timeout_minutes'], 180)

    def test_manual_entry_is_kept_only_for_dispatching_dependency_policy(self):
        self.policy(CHECKS)
        text = self.files()[CHECK]
        self.assertIn("  workflow_dispatch:\n    inputs:\n      source:", text)
        self.assertIn("source: ${{ inputs.source || github.sha }}", text)
        self.assertIn("group: ci-${{ github.ref }}-${{ inputs.source || 'current' }}", text)
        self.assertIn('  push:\n    branches: [main]\n    paths:\n', text)
        (self.root / '.github/dependency-policy.toml').unlink()
        self.assertNotIn('workflow_dispatch', self.files()[CHECK])

    def test_the_push_paths_are_the_inputs_of_the_compiler_cache_identity(self):
        paths = ''.join("      - '" + name + "'\n" for name in single_job_workflows.CACHE_PATHS)
        self.assertIn('  push:\n    branches: [main]\n    paths:\n' + paths + 'permissions:', GOLDEN)
        for name in ['cookbook.toml', 'mise.toml', '**/Cargo.lock', '**/Cargo.toml', CHECK]:
            self.assertIn(name, single_job_workflows.CACHE_PATHS)

    def test_platform_checks_are_the_literal_job_names_of_the_matrix(self):
        self.assertEqual(single_job_workflows.required_checks(self.config), CHECKS)
        rows = re.findall(r'^          - id: .*\n            runner: .*\n            platform: (.*)\n            arch: (.*)\n', GOLDEN, flags=re.MULTILINE)
        self.assertEqual(['Build and test (' + platform + '-' + arch + ')' for platform, arch in rows], CHECKS)
        self.assertIn('    name: Build and test (${{ matrix.platform }}-${{ matrix.arch }})\n', GOLDEN)

    def test_dependency_policy_must_list_every_platform_check_and_no_aggregate(self):
        listed = json.dumps(CHECKS)
        for checks in [['Build and test'], CHECKS[:2], [*CHECKS, 'Build and test'], [*CHECKS, 'Build and test / Test'],
                       ['Build and test (Linux-x86_64)', 'Build and test (Darwin-arm64)', 'Build and test (Windows-arm64)'], []]:
            self.policy(checks)
            with self.subTest(checks=checks), self.assertRaisesRegex(ValueError, 'must include exactly: ' + re.escape(listed)):
                self.files()
        self.policy([*CHECKS, 'Security'])
        self.assertIn('Build and test (${{ matrix.platform }}-${{ matrix.arch }})', self.files()[CHECK])

    def test_a_graph_whose_dependencies_stay_on_one_platform_is_one_wave(self):
        self.config['components']['native'] = [
            {'name': 'api', 'role': 'api', 'component': 'cli', 'binary': 'api', 'output': '.artifacts/native-api/api_{platform}_{arch}'},
            {'name': 'cli', 'role': 'cli', 'component': 'cli', 'binary': 'example', 'output': '.artifacts/native-cli/example_{platform}_{arch}',
             'needs': [{'component': 'api', 'path_environment': 'API'}]}]
        self.assertEqual(native_ci.second_wave(self.config), {})
        self.assertIn('Build and test (${{ matrix.platform }}-${{ matrix.arch }})', self.files()[CHECK])

    def test_a_recipe_without_a_linux_x86_64_target_cannot_host_the_groups(self):
        self.config['package']['native_targets'] = [self.targets[1], self.targets[3]]
        with self.assertRaisesRegex(ValueError, 'needs a Linux x86_64 target'):
            self.files()

    def test_without_the_flag_the_multi_job_caller_is_unchanged(self):
        self.config['ci'] = {}
        legacy = self.files()[CHECK]
        self.config['ci'] = {'single_job': False}
        self.assertEqual(self.files()[CHECK], legacy)
        self.assertIn('uses: nicodes/cookbook/.github/workflows/build-and-test-native.yml@' + PIN, legacy)
        self.assertIn('  push:\n    branches: [main]\n  workflow_dispatch:', legacy)
        self.assertIn('  build-test:\n    name: Build and test\n    if: always()\n    needs: [validate]', legacy)
        self.assertNotIn('matrix', legacy)
        self.assertNotIn('nicodes/cookbook/prepare', legacy)
        self.policy(['Build and test'])
        self.assertEqual(self.files()[CHECK], legacy)

    def test_release_keeps_its_multi_job_path_in_single_job_mode(self):
        self.config['publication'] = {'tag_prefix': 'cli-v', 'initial_version': '0.0.10', 'initial': 'bump'}
        import cli_publication_workflows as release
        with patch.object(release, 'load', return_value=self.config):
            single = self.files()
            expected = release.workflow_files(self.root, PIN)
            self.config['ci'] = {}
            legacy = self.files()
        self.assertEqual(single['.github/workflows/release.yml'], expected['.github/workflows/release.yml'])
        self.assertEqual(single['.github/workflows/release.yml'], legacy['.github/workflows/release.yml'])
        self.assertIn('nicodes/cookbook/.github/workflows/prepare-cli-release.yml@' + PIN, single['.github/workflows/release.yml'])


class Local(Source):
    """The platform phases that do not host the groups."""

    def setUp(self):
        super().setUp()
        self.order = []
        self.config['package']['native_targets'][1]['checks'] = ['tests/boundary.py']
        self.entry = next(row for row in self.declaration()['wave1']['include'] if row['platform'] == 'Darwin')
        environment = patch.dict(os.environ, {'GITHUB_OUTPUT': str(self.root / 'output')})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(native_ci.rust_tests.IMPORTED_UNITS, None)
        mocks = {
            'native_ci.subprocess.run': {'side_effect': self.fake_run},
            'native_ci.recipe.configure_environment': {'side_effect': lambda *args: self.order.append('configure')},
            'native_ci.activate_sdk': {'side_effect': lambda *args: self.order.append('activate')},
            'native_ci.collect': {'side_effect': self.collect},
            'native_ci.export': {'side_effect': AssertionError('nothing leaves a single-job platform')},
            'native_ci.download': {'side_effect': AssertionError('nothing arrives at a single-job platform')},
            'native_ci.validate_jobs': {'side_effect': AssertionError('no Actions API job proof in one job')},
            'native_ci.helper': {'side_effect': self.helper},
            'native_ci.build_payloads': {'return_value': ({'dist/example_Darwin_arm64.tar.gz'}, [])}}
        for target, options in mocks.items():
            patcher = patch(target, **options)
            patcher.start()
            self.addCleanup(patcher.stop)

    def helper(self, name):
        return MagicMock(host=lambda: ('Darwin', 'arm64'))

    def fake_run(self, command, **kwargs):
        selection = {key: kwargs['env'][key] for key in ['COOKBOOK_TEST_PATHS', 'COOKBOOK_NATIVE_TESTS'] if key in kwargs['env']}
        self.order.append([command[-1], selection] if selection else command[-1])
        self.assertTrue(kwargs['check'])
        self.assertEqual(kwargs['env']['RUST_TARGET'], 'aarch64-apple-darwin')
        self.assertEqual(kwargs['env']['COOKBOOK_STAGE'], 'native')
        self.assertNotIn(native_ci.rust_tests.IMPORTED_UNITS, kwargs['env'])

    def collect(self, root, config, source, tools, entry):
        self.order.append('collect')
        return {'version': 1, 'coverage': 'native-partial', 'complete_host': False, 'status': 'passed', 'entry': entry,
                'tests': {name: {} for name in native_ci.expected_receipts(entry)}}

    def phase(self, phase, **options):
        native_ci.run_local(self.root, self.config, self.entry, phase, SOURCE, TOOLS, **options)

    def test_setup_runs_only_the_setup_command_for_this_platform(self):
        self.phase('setup')
        self.assertEqual(self.order, ['configure', 'setup'])
        self.assertEqual(os.environ['RUST_TARGET'], 'aarch64-apple-darwin')
        self.assertEqual(os.environ['COOKBOOK_STAGE'], 'native')

    def test_build_activates_the_prepared_sdk_before_building(self):
        self.phase('build')
        self.assertEqual(self.order, ['configure', 'activate', 'build'])

    def test_test_runs_the_platform_selections_then_keeps_its_own_receipt_without_export_or_import(self):
        stale = self.root / '.artifacts/cookbook-validation.json'
        stale.parent.mkdir()
        stale.write_text('{"stale": true}')
        self.phase('test', head='e' * 40, base='f' * 40)
        self.assertEqual(self.order[:2], ['configure', 'activate'])
        self.assertEqual(self.order[2:5], ['test:unit', ['test:integration', {'COOKBOOK_TEST_PATHS': '["tests/boundary.py"]'}], 'collect'])
        receipt = json.loads(stale.read_text())
        self.assertEqual(receipt['coverage'], 'single-job-native-platform')
        self.assertIs(receipt['complete_host'], False)
        self.assertEqual((receipt['head'], receipt['base']), ('e' * 40, 'f' * 40))
        self.assertNotIn('stale', receipt)
        self.assertEqual((self.root / 'output').read_text(),
                         'artifact-paths<<COOKBOOK_ARTIFACT_PATHS\ndist/example_Darwin_arm64.tar.gz\n'
                         '.artifacts/verification/\n.artifacts/cookbook-validation.json\nCOOKBOOK_ARTIFACT_PATHS\n')

    def test_cargo_selections_run_as_their_own_named_command(self):
        self.entry = {**self.entry, 'cargo': ['one', 'two'], 'groups': [], 'checks': []}
        self.phase('test')
        self.assertEqual(self.order[2], ['test:unit', {'COOKBOOK_NATIVE_TESTS': '["one", "two"]'}])

    def test_the_hosting_platform_and_unknown_phases_are_refused_before_any_work(self):
        host = next(row for row in self.declaration()['wave1']['include'] if row['platform'] == 'Linux')
        with self.assertRaisesRegex(ValueError, 'do not host the groups'):
            native_ci.run_local(self.root, self.config, host, 'test', SOURCE, TOOLS)
        with self.assertRaisesRegex(ValueError, 'setup, build or test'):
            self.phase('verify')
        self.assertEqual(self.order, [])

    def test_a_runner_of_another_platform_is_refused(self):
        with patch.object(native_ci, 'helper', return_value=MagicMock(host=lambda: ('Linux', 'x86_64'))), \
                self.assertRaisesRegex(ValueError, 'differs from the declared platform'):
            self.phase('build')

    def test_cargo_unit_reuse_is_refused_in_a_single_job_job(self):
        environment = {'GITHUB_ACTIONS': 'true', 'GITHUB_JOB': 'build-test', 'COOKBOOK_STAGE': 'full',
                       native_ci.rust_tests.IMPORTED_UNITS: '["owner"]'}
        self.config['components']['rust_tests'] = [{'name': 'owner', 'component': 'cli', 'selectors': {'native_host': True}}]
        with self.assertRaisesRegex(ValueError, 'exact host join'):
            native_ci.rust_tests.remaining_unit_names(self.config, environment, ['owner'])


class Selection(Source):
    def test_the_prepared_target_selects_one_declared_platform(self):
        for entry in self.declaration()['wave1']['include']:
            selected = native_ci.local_entry(self.root, self.config, {native_ci.LOCAL_TARGET: entry['target']})
            self.assertEqual(selected['id'], entry['id'])
        for target in ['', 'aarch64-unknown-linux-musl']:
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'compilation target'):
                native_ci.local_entry(self.root, self.config, {native_ci.LOCAL_TARGET: target})

    def test_only_linux_x86_64_hosts_the_groups(self):
        hosting = [entry['id'] for entry in self.declaration()['wave1']['include'] if native_ci.hosts_groups(entry)]
        self.assertEqual(hosting, ['1-Linux-x86_64'])

    def test_the_hosting_platform_validates_its_own_complete_build_for_its_target(self):
        entry = self.declaration()['wave1']['include'][0]
        environment = {'COOKBOOK_NATIVE_COMPONENTS': '["a"]', 'NATIVE_CLI_ARCHIVES': '/imports',
                       native_ci.rust_tests.IMPORTED_UNITS: '["x"]'}
        with patch.dict(os.environ, environment), \
                patch.object(native_ci, 'helper', return_value=MagicMock(host=lambda: ('Linux', 'x86_64'))):
            native_ci.primary_environment(entry)
            self.assertEqual(os.environ['RUST_TARGET'], 'x86_64-unknown-linux-musl')
            for name in environment:
                self.assertNotIn(name, os.environ)
            self.assertNotEqual(os.environ.get('COOKBOOK_STAGE'), 'native')
        with patch.object(native_ci, 'helper', return_value=MagicMock(host=lambda: ('Darwin', 'arm64'))), \
                self.assertRaisesRegex(ValueError, 'differs from the declared platform'):
            native_ci.primary_environment(entry)


class Dispatch(Source):
    """ci.py --single-job reaches the platform path and never the matrix."""

    def setUp(self):
        super().setUp()
        self.calls = []
        self.model = {'route': 'native', 'recipe': 'rust-cli', 'test_groups': []}
        mocks = {'ci.load': {'return_value': self.config}, 'ci.orchestration.model': {'return_value': self.model},
                 'ci.identity': {'return_value': 'tree'}, 'ci.recipe.configure_environment': {},
                 'ci.subprocess': {'new': SimpleNamespace(run=lambda command, **kwargs: self.calls.append(command[-1]))}}
        for target, options in mocks.items():
            patcher = patch(target, **options)
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ['COOKBOOK_GODOT_ENTRY', 'COOKBOOK_GODOT_HOST']:
            os.environ.pop(name, None)

    def test_a_platform_that_does_not_host_the_groups_runs_locally(self):
        os.environ[native_ci.LOCAL_TARGET] = 'aarch64-apple-darwin'
        with patch.object(native_ci, 'run_local') as local:
            ci.run_single_job(self.root, 'full', 'build', SOURCE, TOOLS)
        self.assertEqual(local.call_args.args[2]['id'], '1-Darwin-arm64')
        self.assertEqual(local.call_args.args[3:6], ('build', SOURCE, TOOLS))
        self.assertEqual(self.calls, [])

    def test_the_platform_hosting_the_groups_continues_as_a_complete_single_job_validation(self):
        os.environ[native_ci.LOCAL_TARGET] = 'x86_64-unknown-linux-musl'
        module = MagicMock(spec=['artifact_paths', 'post_test_evidence'])
        module.artifact_paths.return_value = []
        with patch.object(native_ci, 'run_local') as local, \
                patch.object(native_ci, 'primary_environment', side_effect=lambda entry: os.environ.update(RUST_TARGET=entry['target'])), \
                patch('ci.recipe.adapter', return_value=module), patch('ci.proof', return_value={'status': 'passed'}), \
                patch('ci.relative', side_effect=lambda root, name, **kwargs: root / name):
            ci.run_single_job(self.root, 'full', 'build', SOURCE, TOOLS)
        local.assert_not_called()
        self.assertEqual(self.calls, ['build'])
        self.assertEqual(os.environ['RUST_TARGET'], 'x86_64-unknown-linux-musl')
        self.assertEqual(os.environ['COOKBOOK_STAGE'], 'full')

    def test_an_unselected_platform_is_refused_before_any_work(self):
        os.environ[native_ci.LOCAL_TARGET] = 'x86_64-pc-windows-gnu'
        with self.assertRaisesRegex(ValueError, 'compilation target'):
            ci.run_single_job(self.root, 'full', 'build', SOURCE, TOOLS)
        self.assertEqual(self.calls, [])
