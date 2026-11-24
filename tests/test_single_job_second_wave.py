"""Rust CLI recipes with a second native wave in one-job mode: the job graph, the packet handoff and the job roles."""
import json
import os
from pathlib import Path
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
import rust_cli
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
        self.configure()

    def configure(self):
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

    def test_the_daily_security_workflow_runs_the_scans_only_when_the_product_is_built_by_the_integration_job(self):
        self.config['tests']['security'] = [{'runner': 'python', 'path': 'tests/boundary.py'}]
        text = security_workflows.workflow(self.config, PIN)
        self.assertNotIn('nicodes/cookbook/build@', text)
        self.assertIn('nicodes/cookbook/security@' + PIN, text)
        self.assertIn('.github/workflows/security.yml', self.files())
        del self.config['components']['native']
        self.assertIn('nicodes/cookbook/build@', security_workflows.workflow(self.config, PIN))

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
        for role, message in [('2', 'needs a declared second native wave'), ('host', 'consumes other platforms')]:
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, message):
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


class HandoffBase(SecondWave):
    """Packets staged the way the platform jobs upload them, and the real verification that imports them."""

    def setUp(self):
        super().setUp()
        self.environment = patch.dict(os.environ, self.fixture.environment | {'GITHUB_JOB': 'build-test-host'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
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

class Handoff(HandoffBase):
    """The packets the jobs exchange are verified exactly as the multi-job wave 2 verifies them."""

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
        reloaded = native_ci.host_matrix(self.root, self.config, self.source, self.fixture.tools)
        self.assertEqual(reloaded['jobs'], matrix['jobs'])
        self.assertEqual(os.environ['VERSION'], 'dev')
        self.assertIn('COOKBOOK_NATIVE_BUILD_CHECKS', os.environ)

    def test_the_integration_job_refuses_a_job_that_did_not_succeed_on_its_runner_or_has_another_name(self):
        self.stage(self.single_plan['requirements'])
        entries = self.single_plan['requirements']
        messages = {'skipped': 'must actually succeed on its declared runner', 'runner': 'must actually succeed on its declared runner',
                    'multi-job name': 'must actually succeed on its declared runner', 'missing': 'must actually succeed on its declared runner',
                    'truncated': 'complete same-attempt Actions job records are required'}
        for change in messages:
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
            if change == 'truncated':
                records['total_count'] += 1
            with self.subTest(change=change), patch.object(native_ci, 'download', return_value=self.downloads), \
                    patch.object(native_ci, 'helper', side_effect=self.helper), self.assertRaisesRegex(ValueError, messages[change]):
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
        os.environ.pop(native_ci.MATRIX_DIGEST, None)
        os.environ.pop('GITHUB_ACTIONS', None)  # outside Actions the digest is optional; the checks below still apply
        with self.assertRaisesRegex(ValueError, 'another source'):
            native_ci.host_matrix(self.root, self.config, self.source, self.fixture.tools)

    def imported(self):
        self.stage(self.single_plan['requirements'])
        with patch.object(native_ci, 'download', return_value=self.downloads), patch.object(native_ci, 'helper', side_effect=self.helper):
            return native_ci.import_host(self.root, self.config, self.source, self.fixture.tools, jobs=self.records(self.single_plan['requirements']))

    def reload(self):
        return native_ci.host_matrix(self.root, self.config, self.source, self.fixture.tools)

    def rewrite(self, change):
        """Change the saved matrix and keep its digest consistent, as a writer of both would."""
        path = self.root / native_ci.HOST_MATRIX
        matrix = json.loads(path.read_text())
        change(matrix)
        body = json.dumps(matrix) + '\n'
        path.write_text(body)
        os.environ[native_ci.MATRIX_DIGEST] = native_ci.hashlib.sha256(body.encode()).hexdigest()

    def test_the_saved_matrix_is_bound_by_a_digest_in_the_job_environment(self):
        self.imported()
        self.assertEqual(self.reload()['source_commit'], self.source)
        path = self.root / native_ci.HOST_MATRIX
        path.write_text(path.read_text().replace('"status": "passed"', '"status": "failed"') + ' ')
        with self.assertRaisesRegex(ValueError, 'changed after the import phase'):
            self.reload()

    def test_an_actions_job_must_carry_the_digest_into_later_phases(self):
        self.imported()
        os.environ.pop(native_ci.MATRIX_DIGEST)
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), self.assertRaisesRegex(ValueError, 'changed after the import phase'):
            self.reload()

    def test_the_digest_is_appended_to_the_job_environment_file(self):
        self.stage(self.single_plan['requirements'])
        target = self.root / 'github-env'
        with patch.dict(os.environ, {'GITHUB_ENV': str(target)}), patch.object(native_ci, 'download', return_value=self.downloads), \
                patch.object(native_ci, 'helper', side_effect=self.helper):
            native_ci.import_host(self.root, self.config, self.source, self.fixture.tools, jobs=self.records(self.single_plan['requirements']))
            digest = os.environ[native_ci.MATRIX_DIGEST]
        self.assertEqual(target.read_text(), native_ci.MATRIX_DIGEST + '=' + digest + '\n')
        self.assertEqual(digest, native_ci.hashlib.sha256((self.root / native_ci.HOST_MATRIX).read_bytes()).hexdigest())

    def test_a_matrix_rewritten_together_with_its_digest_is_still_checked_against_the_declaration_and_proofs(self):
        self.imported()
        mutations = {
            'a proof for another platform': (lambda matrix: matrix['proofs'].pop(), 'omitted or repeated platform proofs'),
            'a weakened proof': (lambda matrix: matrix['proofs'][0].update(coverage='complete-host'), 'differs from the declared platform, source or coverage'),
            'another source in a proof': (lambda matrix: matrix['proofs'][0].update(source_commit='f' * 40), 'differs from the declared platform, source or coverage'),
            'a dropped receipt': (lambda matrix: matrix['proofs'][0]['tests'].popitem(), 'differs from the declared platform, source or coverage'),
            'another job record': (lambda matrix: matrix['jobs'][0].update(name='Build and test (Windows-x86_64)'), 'records other jobs than the declared checks'),
            'another declaration': (lambda matrix: matrix['requirements'][0].update(target='x86_64-pc-windows-msvc'), 'differs from the declared platforms'),
            'another attempt': (lambda matrix: matrix['proofs'][0]['execution_context'].update(GITHUB_RUN_ATTEMPT='1'), 'Re-run all jobs'),
            'another job id': (lambda matrix: matrix['proofs'][0]['execution_context'].update(GITHUB_JOB='build-test-wave2'), 'another matrix wave')}
        original = (self.root / native_ci.HOST_MATRIX).read_text()
        for name, (change, message) in mutations.items():
            (self.root / native_ci.HOST_MATRIX).write_text(original)
            with self.subTest(name), patch.dict(os.environ):
                self.rewrite(change)
                with self.assertRaisesRegex(ValueError, message):
                    self.reload()

    def test_job_records_named_with_a_called_workflow_prefix_still_verify(self):
        self.imported()
        self.rewrite(lambda matrix: [job.update(name='Called / ' + job['name']) for job in matrix['jobs']])
        self.assertEqual(self.reload()['source_commit'], self.source)

    def test_an_imported_output_that_changes_before_a_later_phase_is_detected(self):
        matrix = self.imported()
        archive = next(path for path in (self.root / '.artifacts/native-archives').iterdir() if not path.name.endswith('.json'))
        archive.write_bytes(b'swapped after import')
        with self.assertRaisesRegex(ValueError, 'changed after it was verified: .artifacts/native-package/'):
            self.reload()
        archive.unlink()
        with self.assertRaisesRegex(ValueError, 'changed after it was verified'):
            self.reload()
        self.assertTrue(matrix)

    def test_packets_of_another_attempt_are_never_found_and_the_message_says_what_to_do(self):
        environment = {**self.fixture.environment, 'GITHUB_RUN_ATTEMPT': '2'}
        failure = native_ci.subprocess.CalledProcessError(1, ['gh'])
        with patch.dict(os.environ, environment), patch.object(native_ci.subprocess, 'run', side_effect=failure), \
                patch.object(native_ci, 'helper', side_effect=self.helper), \
                self.assertRaisesRegex(ValueError, 'no native packets of attempt 2 of run 123 .*same run attempt: use "Re-run all jobs"'):
            native_ci.download(self.root, self.source, os.environ, wave=1)
        directory = self.downloads
        for entry in self.single_plan['wave1']['include']:
            (directory / native_ci.artifact_name(self.source, entry, {**environment, 'GITHUB_RUN_ATTEMPT': '1'})).mkdir()
        with patch.dict(os.environ, environment), self.assertRaisesRegex(ValueError, 'incomplete, stale or contains unexpected jobs; .*Re-run all jobs'):
            native_ci.import_packets(self.root, self.config, self.source, self.fixture.tools, self.single_plan['wave1']['include'],
                                     directory, os.environ, native_ci.SINGLE_JOBS)

    def test_a_proof_of_another_attempt_of_this_run_says_so(self):
        entry = self.single_plan['wave1']['include'][0]
        context = {key: os.environ[key] for key in native_ci.CONTEXT}
        with self.assertRaisesRegex(ValueError, 'belongs to attempt 1 of this run, not attempt 2; .*Re-run all jobs'):
            native_ci.another_attempt({**context, 'GITHUB_RUN_ATTEMPT': '1'}, {**context, 'GITHUB_RUN_ATTEMPT': '2'})
        native_ci.another_attempt({**context, 'GITHUB_RUN_ID': '9', 'GITHUB_RUN_ATTEMPT': '1'}, context)  # another run: not its business
        with self.assertRaisesRegex(ValueError, 'another Actions execution'):
            native_ci.context_matches({**context, 'GITHUB_RUN_ATTEMPT': '1'}, context)
        self.assertTrue(entry)

    def test_the_integration_only_exists_with_a_second_wave(self):
        del self.config['components']['native']
        with self.assertRaisesRegex(ValueError, 'has an integration job'):
            native_ci.import_host(self.root, self.config, self.source, self.fixture.tools)


def companion_config(test):
    test.config['package']['native_targets'] = [test.targets[0], test.targets[1], test.targets[2], test.targets[3]]
    test.config['components']['native'] = [
        {'name': 'host', 'role': 'host', 'component': 'cli', 'binary': 'host', 'output': '.artifacts/native-host/host_{platform}_{arch}'},
        {'name': 'cli', 'role': 'cli', 'component': 'cli', 'binary': 'example', 'output': '.artifacts/native-cli/example_{platform}_{arch}'}]
    test.config['package']['extra_files'] = ['host_Linux_x86_64', 'host_Linux_arm64']
    test.config['package']['extra_file_sources'] = {
        'host_Linux_x86_64': {'component': 'host', 'platform': 'Linux/x86_64'},
        'host_Linux_arm64': {'component': 'host', 'platform': 'Linux/arm64'}}


class Companions(base.Source):
    """Every platform's package bundles the Linux host binaries for both architectures, built by the platform jobs.

    There is no second wave: the only native dependency stays on its own platform. The complete build on the primary
    platform still needs the other Linux architecture's host binary, which only that platform's job produces.
    """

    def setUp(self):
        super().setUp()
        companion_config(self)

    def jobs(self):
        return read_yaml(self.files()[CHECK])['jobs']

    def plain(self):
        """The same recipe whose package files are all built on the primary platform."""
        self.config['package']['extra_files'] = ['host_Linux_x86_64']
        self.config['package']['extra_file_sources'] = {'host_Linux_x86_64': {'component': 'host', 'platform': 'Linux/x86_64'}}


PLATFORMS = ['Build and test (Linux-x86_64)', 'Build and test (Linux-arm64)', 'Build and test (Darwin-x86_64)', 'Build and test (Darwin-arm64)']


class CompanionGeneration(Companions):
    def test_package_files_from_another_platform_need_an_integration_job_and_no_second_wave(self):
        declaration = native_ci.single_job_support(self.config)
        self.assertEqual(declaration['wave2']['include'], [])
        self.assertTrue(declaration['integration'])
        self.assertEqual(declaration['integration_reasons'], ['package file host_Linux_arm64 is host for Linux/arm64'])
        jobs = self.jobs()
        self.assertEqual(list(jobs), ['build-test', 'build-test-host'])
        self.assertEqual(jobs['build-test-host']['needs'], 'build-test')
        self.assertEqual(jobs['build-test-host']['name'], INTEGRATION)
        self.assertNotIn('wave 2', self.files()[CHECK])
        self.assertNotIn('build-test-wave2', self.files()[CHECK])

    def test_the_platform_jobs_hand_their_work_on_and_the_integration_job_imports_it(self):
        jobs = self.jobs()
        self.assertEqual(uses(jobs['build-test']), ['prepare', 'build', 'test', 'export-native', 'finish'])
        self.assertEqual(uses(jobs['build-test-host']), ['prepare', 'import-native', 'build', 'test', 'finish'])
        self.assertEqual(jobs['build-test-host']['env']['COOKBOOK_NATIVE_WAVE'], 'host')
        self.assertEqual(jobs['build-test-host']['steps'][0]['with']['target'], 'x86_64-unknown-linux-musl')
        self.assertEqual([row['id'] for row in jobs['build-test']['strategy']['matrix']['include']],
                         ['1-Linux-x86_64', '1-Linux-arm64', '1-Darwin-x86_64', '1-Darwin-arm64'])

    def test_the_required_checks_are_the_platforms_and_the_integration_job(self):
        self.assertEqual(single_job_workflows.required_checks(self.config), [*PLATFORMS, INTEGRATION])
        self.policy(PLATFORMS)
        with self.assertRaisesRegex(ValueError, 'must include exactly: ' + re.escape(json.dumps([*PLATFORMS, INTEGRATION]))):
            self.files()
        self.policy([*PLATFORMS, INTEGRATION])
        self.assertIn('build-test-host', self.files()[CHECK])

    def test_no_platform_job_hosts_the_groups_and_the_integration_job_has_a_deadline_for_them(self):
        declaration = native_ci.single_plan(self.root, self.config)
        self.assertEqual([entry['id'] for entry in declaration['requirements'] if native_ci.hosts_groups(entry, declaration)], [])
        self.config['ci'] = {'single_job': True, 'timeout_minutes': 20}
        self.assertEqual(single_job_workflows.integration(self.config)['timeout_minutes'], 30 + 20 + 15)

    def test_a_companion_built_on_the_primary_platform_alone_needs_no_handoff(self):
        self.plain()
        self.assertFalse(native_ci.single_job_support(self.config)['integration'])
        text = self.files()[CHECK]
        for word in ['export-native', 'import-native', 'COOKBOOK_NATIVE_WAVE', 'needs:', 'integration']:
            self.assertNotIn(word, text)
        self.assertEqual(single_job_workflows.required_checks(self.config), PLATFORMS)

    def test_a_recipe_with_neither_a_second_wave_nor_other_platform_files_is_unchanged(self):
        del self.config['components']['native']
        del self.config['package']['extra_files']
        del self.config['package']['extra_file_sources']
        self.config['package']['native_targets'] = [self.targets[0], self.targets[3], self.targets[4]]
        self.assertEqual(self.files()[CHECK], base.GOLDEN)

    def test_a_package_file_whose_platform_has_no_declared_job_is_refused_at_generation(self):
        self.config['package']['native_targets'] = [self.targets[0], self.targets[3]]
        with self.assertRaisesRegex(ValueError, 'needs a declared native target for Linux/arm64: .*host_Linux_arm64 is host for Linux/arm64'):
            self.files()

    def test_the_daily_security_workflow_runs_the_scans_only_for_this_shape(self):
        self.assertIn('.github/workflows/security.yml', self.files())
        self.config['tests']['security'] = [{'runner': 'python', 'path': 'tests/boundary.py'}]
        self.assertNotIn('nicodes/cookbook/build@', security_workflows.workflow(self.config, PIN))
        self.plain()
        self.assertIn('nicodes/cookbook/build@', security_workflows.workflow(self.config, PIN))

    def test_a_declared_lockfile_audit_is_the_security_workflows_cargo_audit_whatever_the_platform(self):
        self.config['ci'] = {'single_job': True, 'rust_lockfile_audit': 'Linux/arm64'}
        self.assertEqual(security_workflows.scanners(self.config), ['cargo audit'])
        text = self.files()['.github/workflows/security.yml']
        self.assertIn('advisories: run', text)
        self.assertIn('nicodes/cookbook/security@' + PIN, text)
        self.assertEqual(native_ci.plan(None, self.config)['wave1']['include'][1]['audit_lockfiles'], True)

    def test_receipts_of_recipes_without_an_integration_job_keep_their_job_names(self):
        self.plain()
        self.assertEqual([entry['job'] for entry in native_ci.single_plan(self.root, self.config)['requirements']],
                         [entry['job'] for entry in native_ci.plan(self.root, self.config)['requirements']])
        self.assertEqual(native_ci.single_plan(self.root, self.config)['requirements'][0]['job'], 'Test (wave 1, Linux-x86_64)')

    def test_receipts_of_integration_recipes_name_the_check_the_job_reports(self):
        names = [entry['job'] for entry in native_ci.single_plan(self.root, self.config)['requirements']]
        self.assertEqual(names, PLATFORMS)


class CompanionHandoff(HandoffBase):
    """The integration job of a recipe without a second wave imports exactly the platform packets."""

    def configure(self):
        companion_config(self)

    def test_the_integration_job_imports_only_first_wave_packets_and_checks_their_jobs(self):
        self.assertEqual(self.single_plan['wave2']['include'], [])
        entries = self.single_plan['requirements']
        self.stage(entries)
        with patch.object(native_ci, 'download', return_value=self.downloads), patch.object(native_ci, 'helper', side_effect=self.helper):
            matrix = native_ci.import_host(self.root, self.config, self.source, self.fixture.tools, jobs=self.records(entries))
        self.assertEqual(sorted(job['name'] for job in matrix['jobs']), sorted(PLATFORMS))
        for platform in ['Linux_x86_64', 'Linux_arm64']:
            self.assertTrue((self.root / '.artifacts/native-host' / ('host_' + platform)).is_file())
        self.assertEqual(native_ci.imported_checks(matrix), {})

    def test_a_missing_platform_packet_or_job_refuses_the_import(self):
        entries = self.single_plan['requirements']
        self.stage(entries[:3])
        with patch.object(native_ci, 'download', return_value=self.downloads), patch.object(native_ci, 'helper', side_effect=self.helper), \
                self.assertRaisesRegex(ValueError, 'incomplete, stale or contains unexpected jobs'):
            native_ci.import_host(self.root, self.config, self.source, self.fixture.tools, jobs=self.records(entries))

    def test_a_first_wave_job_of_this_shape_runs_locally_and_hands_on(self):
        self.assertFalse(native_ci.hosts_groups(self.single_plan['requirements'][0], self.single_plan))


class SecurityScans(unittest.TestCase):
    """The product-running security tests are skipped only when the daily phase of an integration recipe asks."""

    def run_security(self, environment):
        with patch.object(rust_cli, 'run'), patch.object(rust_cli, 'product_tests') as product, \
                patch.object(rust_cli.node_component, 'audit'), patch.object(rust_cli, 'TOOLS', rust_cli.TOOLS):
            rust_cli.security(Path('/nonexistent'), {'security': {}}, {'COOKBOOK_ADVISORIES': 'skip', **environment})
        return product

    def test_scans_only_leaves_the_product_tests_to_the_integration_job(self):
        self.run_security({rust_cli.SECURITY_SCANS_ONLY: 'true'}).assert_not_called()
        self.run_security({}).assert_called_once()
        self.run_security({rust_cli.SECURITY_SCANS_ONLY: 'false'}).assert_called_once()


class Dispatch(SecondWave):
    """ci.py --single-job reaches the role of the job and never the matrix."""

    def setUp(self):
        super().setUp()
        self.calls = []
        mocks = {'ci.load': {'return_value': self.config},
                 'ci.orchestration.model': {'return_value': {'route': 'native', 'recipe': 'rust-cli', 'test_groups': []}},
                 'ci.identity': {'return_value': 'tree'}, 'ci.recipe.configure_environment': {},
                 'ci.subprocess': {'new': SimpleNamespace(run=lambda command, **kwargs: (self.calls.append(command[-1]), self.environments.append(kwargs.get('env', {}))))}}
        self.environments = []
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
            host.assert_called_once()
            self.assertEqual(host.call_args.args, (self.root, self.config, SOURCE, TOOLS))
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
        self.assertEqual(self.environments[-1]['COOKBOOK_SECURITY_SCANS_ONLY'], 'true')

    def host_run(self, phase, imported=None):
        module = MagicMock(spec=['artifact_paths', 'post_test_evidence'])
        module.artifact_paths.return_value = []
        matrix = {'proofs': []}
        with patch.object(native_ci, 'run_local') as local, \
                patch.object(native_ci, 'primary_environment', side_effect=lambda entry: os.environ.update(RUST_TARGET=entry['target'])), \
                patch.object(native_ci, 'host_matrix', return_value=matrix) as loaded, \
                patch.object(native_ci, 'imported_checks', return_value=imported or {}), \
                patch.object(native_ci, 'validate_imported_outputs') as validated, \
                patch.object(native_ci, 'finalize_host', side_effect=lambda host, value: {**host, 'native_matrix': value, 'coverage': 'complete'}), \
                patch('ci.recipe.adapter', return_value=module), patch('ci.proof', return_value={'status': 'passed', 'test_groups': ['unit']}), \
                patch('ci.relative', side_effect=lambda root, name, **kwargs: root / name):
            self.run_phase(phase, 'x86_64-unknown-linux-musl', 'host')
        local.assert_not_called()
        self.validated = validated
        return loaded

    def test_the_integration_job_is_a_complete_validation_that_adopts_the_imported_matrix(self):
        self.assertEqual(self.host_run('setup').call_count, 0)
        self.assertEqual(self.host_run('build').call_count, 1)
        self.assertEqual(self.calls, ['setup', 'build'])
        self.assertEqual(os.environ['RUST_TARGET'], 'x86_64-unknown-linux-musl')

    def test_groups_a_platform_job_already_proved_are_imported_not_rerun(self):
        producer = {'entry': {'id': '1-Linux-x86_64'}}
        self.host_run('test', {'e2e': producer})
        self.validated.assert_called_once()
        self.assertIs(self.validated.call_args.args[3], producer)
        receipt = json.loads((self.root / '.artifacts/cookbook-validation.json').read_text())
        self.assertEqual(receipt['test_groups'], ['e2e', 'unit'])
        self.assertEqual(self.environments[-1]['COOKBOOK_IMPORTED_NATIVE_GROUPS'], 'e2e')

    def test_the_integration_receipt_binds_the_matrix_and_names_its_coverage(self):
        self.assertEqual(self.host_run('test').call_count, 1)
        receipt = json.loads((self.root / '.artifacts/cookbook-validation.json').read_text())
        self.assertEqual(receipt['coverage'], 'single-job-native-matrix')
        self.assertEqual(receipt['native_matrix'], {'proofs': []})


if __name__ == '__main__':
    unittest.main()
