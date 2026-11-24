"""Rust CLI recipes with a second native wave in one-job mode: the job graph, the packet handoff and the job roles."""
import json
import os
import re
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import MagicMock, patch

from source_fixture import SDK_ROOT
import test_single_job_rust_cli as base
from workflow_fixtures import read_yaml

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import ci
import native_ci
import security_workflows
import single_job_workflows

CHECK, PIN, SOURCE, TOOLS = base.CHECK, base.PIN, base.SOURCE, base.TOOLS
FIRST = ['Build and test (Linux-x86_64)', 'Build and test (Linux-arm64)', 'Build and test (Darwin-arm64)']
SECOND = ['Build and test (Linux-x86_64 wave 2)', 'Build and test (Linux-arm64 wave 2)', 'Build and test (Darwin-arm64 wave 2)']
INTEGRATION = 'Build and test (integration)'
USES = r'nicodes/cookbook/'


class SecondWave(base.Source):
    """Linux x86_64, Linux arm64 and macOS arm64 whose CLI package bundles the Linux host binaries."""

    def setUp(self):
        super().setUp()
        self.second_wave()
        self.config['package']['native_targets'] = [self.targets[0], self.targets[1], self.targets[3]]

    def jobs(self):
        return read_yaml(self.files()[CHECK])['jobs']

    def single(self):
        return native_ci.single_plan(self.root, self.config)


def uses(job):
    return [step['uses'].split('@')[0].removeprefix(USES) for step in job['steps']]


class Generation(SecondWave):
    def test_the_job_graph_is_the_first_wave_the_second_wave_and_one_integration_job(self):
        jobs = self.jobs()
        self.assertEqual(list(jobs), ['build-test', 'build-test-wave2', 'build-test-host'])
        self.assertNotIn('needs', jobs['build-test'])
        self.assertEqual(jobs['build-test-wave2']['needs'], 'build-test')
        self.assertEqual(jobs['build-test-host']['needs'], ['build-test', 'build-test-wave2'])
        self.assertEqual(jobs['build-test']['name'], 'Build and test (${{ matrix.platform }}-${{ matrix.arch }})')
        self.assertEqual(jobs['build-test-wave2']['name'], 'Build and test (${{ matrix.platform }}-${{ matrix.arch }} wave 2)')
        self.assertEqual(jobs['build-test-host']['name'], INTEGRATION)

    def test_each_wave_is_a_literal_matrix_of_the_declared_platforms(self):
        jobs = self.jobs()
        first = jobs['build-test']['strategy']['matrix']['include']
        second = jobs['build-test-wave2']['strategy']['matrix']['include']
        self.assertEqual([row['id'] for row in first], ['1-Linux-x86_64', '1-Linux-arm64', '1-Darwin-arm64'])
        self.assertEqual([row['id'] for row in second], ['2-Linux-x86_64', '2-Linux-arm64', '2-Darwin-arm64'])
        self.assertEqual([{key: row[key] for key in ['runner', 'target']} for row in first],
                         [{key: row[key] for key in ['runner', 'target']} for row in second])
        self.assertFalse(jobs['build-test-wave2']['strategy']['fail-fast'])
        self.assertNotIn('fromJSON', self.files()[CHECK])

    def test_every_job_hands_its_work_on_through_the_packet_actions_in_order(self):
        jobs = self.jobs()
        self.assertEqual(uses(jobs['build-test']), ['prepare', 'build', 'test', 'export-native', 'finish'])
        self.assertEqual(uses(jobs['build-test-wave2']), ['prepare', 'import-native', 'build', 'test', 'export-native', 'finish'])
        self.assertEqual(uses(jobs['build-test-host']), ['prepare', 'import-native', 'build', 'test', 'finish'])
        for name in ['build-test', 'build-test-wave2']:
            export = jobs[name]['steps'][-2]
            self.assertEqual(export['with'], {'entry': '${{ matrix.id }}'})
            self.assertEqual(jobs[name]['steps'][-1]['with']['entry'], '${{ matrix.id }}')

    def test_job_roles_are_literal_job_environment(self):
        jobs = self.jobs()
        self.assertNotIn('COOKBOOK_NATIVE_WAVE', jobs['build-test']['env'])
        self.assertEqual(jobs['build-test-wave2']['env']['COOKBOOK_NATIVE_WAVE'], '2')
        self.assertEqual(jobs['build-test-host']['env']['COOKBOOK_NATIVE_WAVE'], 'host')

    def test_the_integration_job_runs_on_linux_x86_64_for_its_target(self):
        job = self.jobs()['build-test-host']
        self.assertEqual(job['runs-on'], 'ubuntu-24.04')
        self.assertEqual(job['steps'][0]['with']['target'], 'x86_64-unknown-linux-musl')
        self.assertEqual(job['permissions'], {'contents': 'read', 'actions': 'read', 'pull-requests': 'read'})

    def test_every_job_has_a_deadline_that_covers_its_handoff(self):
        self.config['package']['native_targets'][0]['timeout_minutes'] = 40
        self.config['package']['native_targets'][0]['second_wave_timeout_minutes'] = 50
        self.config['ci'] = {'single_job': True, 'timeout_minutes': 20}
        self.assertEqual({row['id']: row['timeout_minutes'] for row in single_job_workflows.platforms(self.config)},
                         {'1-Linux-x86_64': 55, '1-Linux-arm64': 45, '1-Darwin-arm64': 45})
        self.assertEqual({row['id']: row['timeout_minutes'] for row in single_job_workflows.wave2_platforms(self.config)},
                         {'2-Linux-x86_64': 65, '2-Linux-arm64': 45, '2-Darwin-arm64': 45})
        self.assertEqual(single_job_workflows.integration(self.config)['timeout_minutes'], 75)
        self.assertEqual(read_yaml(self.files()[CHECK])['jobs']['build-test-host']['timeout-minutes'], 75)

    def test_the_required_checks_are_stable_and_listed_in_order(self):
        self.assertEqual(single_job_workflows.required_checks(self.config), [*FIRST, *SECOND, INTEGRATION])
        names = [native_ci.single_check(entry) for entry in native_ci.plan(None, self.config)['requirements']]
        self.assertEqual(sorted(names), sorted([*FIRST, *SECOND]))
        self.assertEqual(single_job_workflows.CHECK, native_ci.SINGLE_CHECK)
        self.assertEqual(single_job_workflows.INTEGRATION, native_ci.INTEGRATION)

    def test_the_dependency_policy_must_require_every_job_of_the_graph(self):
        everything = [*FIRST, *SECOND, INTEGRATION]
        self.policy(FIRST)
        with self.assertRaisesRegex(ValueError, 'must include exactly: ' + re.escape(json.dumps(everything))):
            self.files()
        self.policy([*FIRST, *SECOND])
        with self.assertRaisesRegex(ValueError, 'must include exactly'):
            self.files()
        self.policy(everything)
        self.assertIn('build-test-host', self.files()[CHECK])

    def test_recipes_without_a_second_wave_gain_no_handoff(self):
        self.config['components']['native'] = []
        del self.config['components']['native']
        self.config['package']['native_targets'] = [self.targets[0], self.targets[3], self.targets[4]]
        text = self.files()[CHECK]
        self.assertEqual(text, base.GOLDEN)
        for word in ['export-native', 'import-native', 'COOKBOOK_NATIVE_WAVE', 'needs:', 'wave 2', 'integration']:
            self.assertNotIn(word, text)
        self.assertEqual(single_job_workflows.required_checks(self.config), base.CHECKS)

    def test_a_dependency_graph_inside_one_platform_still_has_no_second_wave(self):
        self.config['components']['native'] = [
            {'name': 'api', 'role': 'api', 'component': 'cli', 'binary': 'api', 'output': '.artifacts/native-api/api_{platform}_{arch}'},
            {'name': 'cli', 'role': 'cli', 'component': 'cli', 'binary': 'example', 'output': '.artifacts/native-cli/example_{platform}_{arch}',
             'needs': [{'component': 'api', 'path_environment': 'API'}]}]
        self.assertFalse(single_job_workflows.second_wave(self.config))
        self.assertEqual(list(self.jobs()), ['build-test'])

    def test_a_recipe_without_a_linux_x86_64_target_still_cannot_host_the_groups(self):
        self.config['package']['native_targets'] = [self.targets[1], self.targets[3]]
        with self.assertRaisesRegex(ValueError, 'needs a Linux x86_64 target'):
            self.files()

    def test_the_release_workflow_is_untouched(self):
        self.config['publication'] = {'tag_prefix': 'cli-v', 'initial_version': '0.0.10', 'initial': 'bump'}
        import cli_publication_workflows as release
        with patch.object(release, 'load', return_value=self.config):
            single = self.files()
            expected = release.workflow_files(self.root, PIN)
        self.assertEqual(single['.github/workflows/release.yml'], expected['.github/workflows/release.yml'])
        self.assertIn('nicodes/cookbook/.github/workflows/prepare-cli-release.yml@' + PIN, single['.github/workflows/release.yml'])

    def test_declared_security_tests_cannot_run_in_the_daily_one_job_workflow(self):
        self.assertIn('.github/workflows/security.yml', self.files())
        self.config['tests']['security'] = [{'runner': 'python', 'path': 'tests/boundary.py'}]
        with self.assertRaisesRegex(ValueError, 'daily Security workflow cannot run declared security tests of a recipe with a second native wave'):
            security_workflows.workflow(self.config, PIN)

    def test_the_handoff_actions_upload_and_import_the_multi_job_packet_names(self):
        export = read_yaml((SDK_ROOT / 'export-native/action.yml').read_text())
        step = export['runs']['steps'][0]
        self.assertEqual(step['with']['name'], 'cookbook-native-${{ env.SOURCE_SHA }}-${{ inputs.entry }}-${{ github.run_id }}-${{ github.run_attempt }}')
        self.assertEqual(step['with']['path'], '.artifacts/native-export/')
        self.assertEqual(step['with']['if-no-files-found'], 'error')
        self.assertTrue(export['inputs']['entry']['required'])
        imported = read_yaml((SDK_ROOT / 'import-native/action.yml').read_text())['runs']['steps']
        self.assertTrue(imported[0]['run'].endswith('--single-job --phase import'))
        self.assertEqual(imported[0]['env'], {'GH_TOKEN': '${{ github.token }}'})
        self.assertEqual(native_ci.artifact_name(SOURCE, {'id': '2-Linux-arm64'}, {
            'GITHUB_REPOSITORY': 'o/r', 'GITHUB_RUN_ID': '7', 'GITHUB_RUN_ATTEMPT': '1'}),
            'cookbook-native-' + SOURCE + '-2-Linux-arm64-7-1')


class Selection(SecondWave):
    """local_entry and hosts_groups: which declared platform and role this job is."""

    def select(self, target, role=None):
        environment = {native_ci.LOCAL_TARGET: target, **({native_ci.WAVE: role} if role else {})}
        return native_ci.local_entry(self.root, self.config, environment)

    def test_the_default_role_is_the_first_wave(self):
        for role in [None, '1']:
            self.assertEqual(self.select('aarch64-unknown-linux-musl', role)['id'], '1-Linux-arm64')

    def test_the_second_wave_role_selects_the_same_platform_in_the_second_wave(self):
        self.assertEqual(self.select('aarch64-apple-darwin', '2')['id'], '2-Darwin-arm64')
        self.assertEqual(self.select('x86_64-unknown-linux-musl', '2')['wave'], 2)

    def test_the_integration_role_is_the_first_wave_linux_x86_64_platform(self):
        entry = self.select('x86_64-unknown-linux-musl', 'host')
        self.assertEqual(entry['id'], '1-Linux-x86_64')
        with self.assertRaisesRegex(ValueError, 'runs on the platform that hosts the groups'):
            self.select('aarch64-apple-darwin', 'host')

    def test_unknown_roles_and_roles_without_a_second_wave_are_refused(self):
        with self.assertRaisesRegex(ValueError, 'must be 1, 2 or host'):
            self.select('aarch64-apple-darwin', '3')
        del self.config['components']['native']
        for role in ['2', 'host']:
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, 'needs a declared second native wave'):
                self.select('x86_64-unknown-linux-musl', role)
        self.assertEqual(self.select('x86_64-unknown-linux-musl')['id'], '1-Linux-x86_64')

    def test_the_check_of_each_entry_is_its_job_name(self):
        self.assertEqual([entry['job'] for entry in self.single()['requirements']],
                         [FIRST[0], SECOND[0], FIRST[1], SECOND[1], FIRST[2], SECOND[2]])

    def test_no_platform_job_hosts_the_groups_when_there_is_a_second_wave(self):
        declaration = self.single()
        self.assertEqual([entry['id'] for entry in declaration['requirements'] if native_ci.hosts_groups(entry, declaration)], [])
        self.assertEqual([entry['id'] for entry in declaration['requirements'] if native_ci.hosts_groups(entry)], ['1-Linux-x86_64'])
        del self.config['components']['native']
        plain = self.single()
        self.assertEqual([entry['id'] for entry in plain['requirements'] if native_ci.hosts_groups(entry, plain)], ['1-Linux-x86_64'])


class Local(SecondWave):
    """The first-wave and second-wave platform jobs run their own selections and stage a packet only when handed on."""

    def setUp(self):
        super().setUp()
        self.order = []
        self.host = ('Linux', 'x86_64')
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        patches = {
            'native_ci.subprocess.run': {'side_effect': lambda command, **kwargs: self.order.append(command[-1])},
            'native_ci.recipe.configure_environment': {},
            'native_ci.activate_sdk': {},
            'native_ci.helper': {'side_effect': lambda name: MagicMock(host=lambda: self.host)},
            'native_ci.collect': {'side_effect': lambda root, config, source, tools, entry: self.order.append('collect') or {
                'version': 1, 'coverage': 'native-partial', 'entry': entry, 'tests': {}}},
            'native_ci.export': {'side_effect': lambda root, proof: self.order.append(['export', proof['coverage'], proof['entry']['id']])},
            'native_ci.build_payloads': {'return_value': (set(), [])}}
        for target, options in patches.items():
            patcher = patch(target, **options)
            patcher.start()
            self.addCleanup(patcher.stop)

    def entry(self, id):
        return next(entry for entry in self.single()['requirements'] if entry['id'] == id)

    def test_the_first_wave_linux_x86_64_platform_is_a_local_run_that_exports_its_accepted_proof(self):
        native_ci.run_local(self.root, self.config, self.entry('1-Linux-x86_64'), 'test', SOURCE, TOOLS, handoff=True)
        self.assertEqual(self.order[-2:], ['collect', ['export', 'native-partial', '1-Linux-x86_64']])
        receipt = json.loads((self.root / '.artifacts/cookbook-validation.json').read_text())
        self.assertEqual(receipt['coverage'], 'single-job-native-platform')

    def test_without_a_handoff_nothing_is_exported(self):
        self.host = ('Darwin', 'arm64')
        native_ci.run_local(self.root, self.config, self.entry('1-Darwin-arm64'), 'test', SOURCE, TOOLS)
        self.assertEqual([item for item in self.order if isinstance(item, list)], [])

    def test_a_second_wave_platform_selects_its_own_components_and_exports_its_proof(self):
        entry = self.entry('2-Linux-arm64')
        self.host = ('Linux', 'arm64')
        if True:
            native_ci.run_local(self.root, self.config, entry, 'test', SOURCE, TOOLS, handoff=True)
        self.assertEqual(os.environ['COOKBOOK_NATIVE_COMPONENTS'], '["cli"]')
        self.assertEqual(os.environ['RUST_TARGET'], 'aarch64-unknown-linux-musl')
        self.assertEqual(self.order[-1], ['export', 'native-partial', '2-Linux-arm64'])

    def test_no_platform_job_may_be_the_group_host_without_a_second_wave(self):
        del self.config['components']['native']
        with self.assertRaisesRegex(ValueError, 'do not host the groups'):
            native_ci.run_local(self.root, self.config, self.entry('1-Linux-x86_64'), 'test', SOURCE, TOOLS)


class Handoff(SecondWave):
    """The packets the jobs exchange are verified exactly as the multi-job wave 2 verifies them."""

    def setUp(self):
        super().setUp()
        self.environment = patch.dict(os.environ, self.fixture.environment | {'GITHUB_JOB': 'build-test-host'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.config['package']['native_targets'] = [self.targets[0], self.targets[1], self.targets[3]]
        self.fixture.config = self.config
        self.source = native_ci.ci.git(self.root, 'rev-parse', 'HEAD')
        self.fixture.source, self.fixture.tree = self.source, native_ci.ci.git(self.root, 'rev-parse', 'HEAD^{tree}')
        self.single_plan = native_ci.single_plan(self.root, self.config)
        self.cleaner = MagicMock()
        self.downloads = self.root / 'downloads'
        self.downloads.mkdir()

    def helper(self, name, real=native_ci.helper):
        """The real helpers, except that cleaning the downloads is observed instead of run."""
        if name == 'clean':
            return self.cleaner
        return real(name)

    def stage(self, entries):
        for entry in entries:
            directory = self.downloads / native_ci.artifact_name(self.source, entry, os.environ)
            directory.mkdir()
            self.fixture.packet(entry, directory)
            proof = directory / 'native-proof.json'
            proof.write_text(proof.read_text().replace('"native_one"', '"build-test"').replace('"native_two"', '"build-test-wave2"'))

    def records(self, entries):
        return {'total_count': len(entries), 'jobs': [
            {'id': index + 1, 'name': entry['job'], 'status': 'completed', 'conclusion': 'success', 'labels': [entry['runner']]}
            for index, entry in enumerate(entries)]}

    def test_a_second_wave_job_imports_every_first_wave_packet_and_nothing_else(self):
        first = self.single_plan['wave1']['include']
        self.stage(first)
        entry = self.single_plan['wave2']['include'][0]
        with patch.object(native_ci, 'download', return_value=self.downloads) as download, \
                patch.object(native_ci, 'helper', side_effect=self.helper) as helper:
            native_ci.import_wave(self.root, self.config, entry, self.source, self.fixture.tools)
        download.assert_called_once_with(self.root, self.source, os.environ, wave=1)
        self.cleaner.clean.assert_called_once_with(self.root, ['.artifacts/native-downloads'])
        for platform in ['Linux_x86_64', 'Linux_arm64', 'Darwin_arm64']:
            self.assertTrue((self.root / '.artifacts/native-host' / ('host_' + platform)).is_file())

    def test_a_second_wave_job_refuses_a_missing_or_foreign_packet(self):
        first = self.single_plan['wave1']['include']
        self.stage(first[:2])
        entry = self.single_plan['wave2']['include'][0]
        with patch.object(native_ci, 'download', return_value=self.downloads), \
                patch.object(native_ci, 'helper', side_effect=self.helper), \
                self.assertRaisesRegex(ValueError, 'incomplete, stale or contains unexpected jobs'):
            native_ci.import_wave(self.root, self.config, entry, self.source, self.fixture.tools)

    def test_a_packet_from_another_job_id_is_refused_by_its_execution_context(self):
        first = self.single_plan['wave1']['include']
        self.stage(first)
        for path in self.downloads.glob('*/native-proof.json'):
            path.write_text(path.read_text().replace('"build-test"', '"build-test-wave2"'))
        entry = self.single_plan['wave2']['include'][0]
        with patch.object(native_ci, 'download', return_value=self.downloads), \
                patch.object(native_ci, 'helper', side_effect=self.helper), \
                self.assertRaisesRegex(ValueError, 'another matrix wave'):
            native_ci.import_wave(self.root, self.config, entry, self.source, self.fixture.tools)

    def test_only_a_second_wave_platform_imports_the_first_wave(self):
        with self.assertRaisesRegex(ValueError, 'only a second-wave job imports'):
            native_ci.import_wave(self.root, self.config, self.single_plan['wave1']['include'][0], self.source, self.fixture.tools)

    def test_the_multi_job_job_ids_are_still_the_default(self):
        entry = self.single_plan['wave1']['include'][0]
        directory = self.root / 'one'
        directory.mkdir()
        self.fixture.packet(entry, directory)
        native_ci.packet(self.root, directory, self.config, self.source, self.fixture.tools, entry, os.environ)
        with self.assertRaisesRegex(ValueError, 'another matrix wave'):
            native_ci.packet(self.root, directory, self.config, self.source, self.fixture.tools, entry, os.environ, native_ci.SINGLE_JOBS)

    def test_the_integration_job_imports_both_waves_checks_every_job_and_keeps_the_matrix_for_later_phases(self):
        self.stage(self.single_plan['requirements'])
        records = self.records(self.single_plan['requirements'])
        with patch.object(native_ci, 'download', return_value=self.downloads), patch.object(native_ci, 'helper', side_effect=self.helper):
            matrix = native_ci.import_host(self.root, self.config, self.source, self.fixture.tools, jobs=records)
        self.assertEqual(sorted(job['name'] for job in matrix['jobs']), sorted([*FIRST, *SECOND]))
        self.assertIsNone(matrix['lockfile_audit'])
        self.assertEqual(os.environ['VERSION'], 'dev')
        self.assertEqual(os.environ['NATIVE_CLI_ARCHIVES'], str(self.root / '.artifacts/native-archives'))
        for entry in self.single_plan['wave2']['include']:
            self.assertTrue((self.root / '.artifacts/native-archives').is_dir())
        for name in ['VERSION', 'NATIVE_CLI_ARCHIVES', 'COOKBOOK_NATIVE_BUILD_CHECKS']:
            os.environ.pop(name)
        reloaded = native_ci.host_matrix(self.root, self.source, self.fixture.tools)
        self.assertEqual(reloaded['jobs'], matrix['jobs'])
        self.assertEqual(os.environ['VERSION'], 'dev')
        self.assertIn('COOKBOOK_NATIVE_BUILD_CHECKS', os.environ)

    def test_the_integration_job_refuses_a_job_that_did_not_succeed_on_its_runner_or_has_another_name(self):
        self.stage(self.single_plan['requirements'])
        entries = self.single_plan['requirements']
        for change in ['skipped', 'runner', 'multi-job name', 'missing']:
            records = self.records(entries)
            if change == 'skipped':
                records['jobs'][-1]['conclusion'] = 'skipped'
            if change == 'runner':
                records['jobs'][0]['labels'] = ['windows-2025']
            if change == 'multi-job name':
                records['jobs'][0]['name'] = 'Test (wave 1, Linux-x86_64)'
            if change == 'missing':
                records['jobs'].pop()
                records['total_count'] -= 1
            with self.subTest(change=change), patch.object(native_ci, 'download', return_value=self.downloads), \
                    patch.object(native_ci, 'helper', side_effect=self.helper), self.assertRaises(ValueError):
                native_ci.import_host(self.root, self.config, self.source, self.fixture.tools, jobs=records)
        self.assertFalse((self.root / native_ci.HOST_MATRIX).exists())

    def test_the_integration_job_needs_every_packet_of_both_waves(self):
        self.stage(self.single_plan['wave1']['include'])
        with patch.object(native_ci, 'download', return_value=self.downloads), patch.object(native_ci, 'helper', side_effect=self.helper), \
                self.assertRaisesRegex(ValueError, 'incomplete, stale or contains unexpected jobs'):
            native_ci.import_host(self.root, self.config, self.source, self.fixture.tools, jobs=self.records(self.single_plan['requirements']))

    def test_a_matrix_of_another_source_is_refused_in_later_phases(self):
        (self.root / '.artifacts').mkdir(exist_ok=True)
        (self.root / native_ci.HOST_MATRIX).write_text(json.dumps({'version': 1, 'source_commit': 'e' * 40, 'proofs': [], **native_ci.dependencies.binding(self.fixture.tools)}))
        with self.assertRaisesRegex(ValueError, 'another source'):
            native_ci.host_matrix(self.root, self.source, self.fixture.tools)

    def test_the_integration_only_exists_with_a_second_wave(self):
        del self.config['components']['native']
        with self.assertRaisesRegex(ValueError, 'has an integration job'):
            native_ci.import_host(self.root, self.config, self.source, self.fixture.tools)


class Dispatch(SecondWave):
    """ci.py --single-job reaches the role of the job and never the matrix."""

    def setUp(self):
        super().setUp()
        self.calls = []
        mocks = {'ci.load': {'return_value': self.config},
                 'ci.orchestration.model': {'return_value': {'route': 'native', 'recipe': 'rust-cli', 'test_groups': []}},
                 'ci.identity': {'return_value': 'tree'}, 'ci.recipe.configure_environment': {},
                 'ci.subprocess': {'new': SimpleNamespace(run=lambda command, **kwargs: self.calls.append(command[-1]))}}
        for target, options in mocks.items():
            patcher = patch(target, **options)
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ['COOKBOOK_GODOT_ENTRY', 'COOKBOOK_GODOT_HOST', native_ci.WAVE]:
            os.environ.pop(name, None)

    def run_phase(self, phase, target, role=None):
        os.environ[native_ci.LOCAL_TARGET] = target
        if role:
            os.environ[native_ci.WAVE] = role
        ci.run_single_job(self.root, 'full', phase, SOURCE, TOOLS)

    def test_a_first_wave_platform_runs_locally_and_hands_on_even_on_linux_x86_64(self):
        for target, id in [('x86_64-unknown-linux-musl', '1-Linux-x86_64'), ('aarch64-apple-darwin', '1-Darwin-arm64')]:
            with self.subTest(id=id), patch.object(native_ci, 'run_local') as local, patch.object(native_ci, 'primary_environment') as primary:
                self.run_phase('test', target)
            self.assertEqual(local.call_args.args[2]['id'], id)
            self.assertIs(local.call_args.kwargs['handoff'], True)
            primary.assert_not_called()

    def test_a_second_wave_platform_runs_locally_in_its_wave_and_hands_on(self):
        with patch.object(native_ci, 'run_local') as local:
            self.run_phase('build', 'aarch64-unknown-linux-musl', '2')
        self.assertEqual(local.call_args.args[2]['id'], '2-Linux-arm64')
        self.assertEqual(local.call_args.args[3], 'build')
        self.assertIs(local.call_args.kwargs['handoff'], True)

    def test_the_import_phase_runs_the_import_of_the_job_role(self):
        with patch.object(native_ci, 'import_wave') as wave, patch.object(native_ci, 'import_host') as host:
            self.run_phase('import', 'aarch64-apple-darwin', '2')
            wave.assert_called_once()
            self.assertEqual(wave.call_args.args[2]['id'], '2-Darwin-arm64')
            host.assert_not_called()
            self.run_phase('import', 'x86_64-unknown-linux-musl', 'host')
            host.assert_called_once_with(self.root, self.config, SOURCE, TOOLS)
        self.assertEqual(self.calls, [])

    def test_a_first_wave_job_has_nothing_to_import_and_no_pack_phase_exists(self):
        with self.assertRaisesRegex(ValueError, 'imports nothing'):
            self.run_phase('import', 'x86_64-unknown-linux-musl')
        with self.assertRaisesRegex(ValueError, 'packet upload'):
            self.run_phase('pack', 'x86_64-unknown-linux-musl')

    def test_only_the_first_wave_primary_platform_runs_the_security_phase(self):
        for target, role in [('aarch64-apple-darwin', None), ('x86_64-unknown-linux-musl', '2'), ('x86_64-unknown-linux-musl', 'host')]:
            os.environ.pop(native_ci.WAVE, None)
            with self.subTest(target=target, role=role), self.assertRaisesRegex(ValueError, 'runs on the platform that hosts the groups'):
                self.run_phase('security', target, role)
        os.environ.pop(native_ci.WAVE, None)
        with patch.object(native_ci, 'primary_environment', side_effect=lambda entry: os.environ.update(RUST_TARGET=entry['target'])):
            self.run_phase('security', 'x86_64-unknown-linux-musl')
        self.assertEqual(self.calls, ['test:security'])

    def host_run(self, phase):
        module = MagicMock(spec=['artifact_paths', 'post_test_evidence'])
        module.artifact_paths.return_value = []
        matrix = {'matrix': True}
        with patch.object(native_ci, 'run_local') as local, \
                patch.object(native_ci, 'primary_environment', side_effect=lambda entry: os.environ.update(RUST_TARGET=entry['target'])), \
                patch.object(native_ci, 'host_matrix', return_value=matrix) as loaded, \
                patch.object(native_ci, 'finalize_host', side_effect=lambda host, value: {**host, 'native_matrix': value, 'coverage': 'complete'}), \
                patch('ci.recipe.adapter', return_value=module), patch('ci.proof', return_value={'status': 'passed'}), \
                patch('ci.relative', side_effect=lambda root, name, **kwargs: root / name):
            self.run_phase(phase, 'x86_64-unknown-linux-musl', 'host')
        local.assert_not_called()
        return loaded

    def test_the_integration_job_is_a_complete_validation_that_adopts_the_imported_matrix(self):
        self.assertEqual(self.host_run('setup').call_count, 0)
        self.assertEqual(self.host_run('build').call_count, 1)
        self.assertEqual(self.calls, ['setup', 'build'])
        self.assertEqual(os.environ['RUST_TARGET'], 'x86_64-unknown-linux-musl')

    def test_the_integration_receipt_binds_the_matrix_and_names_its_coverage(self):
        self.assertEqual(self.host_run('test').call_count, 1)
        receipt = json.loads((self.root / '.artifacts/cookbook-validation.json').read_text())
        self.assertEqual(receipt['coverage'], 'single-job-native-matrix')
        self.assertEqual(receipt['native_matrix'], {'matrix': True})


if __name__ == '__main__':
    unittest.main()
