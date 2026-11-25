from source_fixture import SDK_ROOT
"""Rust CLI Release in one-job mode: the job graph, its authority and the proof the release verifier reads."""
import copy
import os
import re
import sys
import unittest
from unittest.mock import patch

import test_cli_publication as publication
import test_single_job_second_wave as second

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import cli_publication_workflows as workflows
import native_ci
import single_job_release

PIN = 'e' * 40
RELEASE = '.github/workflows/release.yml'
PUBLICATION = {'tag_prefix': 'cli-v', 'initial_version': '0.0.10', 'initial': 'bump'}
FIRST = ['Build and test (Linux-x86_64)', 'Build and test (Linux-arm64)', 'Build and test (Darwin-arm64)']


def blocks(text):
    """The jobs of a generated workflow, by id, as raw text."""
    body = text.split('\njobs:\n', 1)[1]
    parts = re.split(r'^  ([a-z0-9-]+):\n', body, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2]))


def permissions(block):
    match = re.search(r'^    permissions:\n((?:      .*\n)+)', block, flags=re.MULTILINE)
    return dict(line.strip().split(': ') for line in match[1].splitlines())


class Release(second.SecondWave):
    def release(self, **publication_values):
        self.config['publication'] = {**PUBLICATION, **publication_values}
        with patch.object(workflows, 'load', return_value=self.config):
            return workflows.workflow_files(self.root, PIN)[RELEASE]


class Generation(Release):
    def test_the_graph_is_the_plan_the_platform_waves_the_integration_job_and_the_two_authority_jobs(self):
        jobs = blocks(self.release())
        self.assertEqual(list(jobs), ['plan', 'build-test', 'build-test-wave2', 'build-test-host', 'accept', 'publish'])
        self.assertNotIn('needs:', jobs['plan'])
        self.assertIn('    needs: plan\n', jobs['build-test'])
        self.assertIn('    needs: [plan, build-test]\n', jobs['build-test-wave2'])
        self.assertIn('    needs: [plan, build-test, build-test-wave2]\n', jobs['build-test-host'])
        self.assertIn('    needs: [plan, build-test-host]\n', jobs['accept'])
        self.assertIn('    needs: [plan, accept]\n', jobs['publish'])
        self.assertIn('    name: Build and test (integration)\n', jobs['build-test-host'])
        self.assertIn('    name: Attest archives\n', jobs['accept'])

    def test_no_reusable_workflow_remains_and_only_the_platform_matrix_job_names_change(self):
        text = self.release()
        self.assertNotRegex(text, r'uses: nicodes/cookbook/\.github/workflows/')
        self.assertNotIn('prepare-cli-release', text)
        self.assertNotIn('publish-cli', text)
        self.assertNotIn('select-cli-release', text)

    def test_without_a_second_wave_the_integration_job_still_assembles_the_release(self):
        self.config['components']['native'] = []
        self.config['package']['native_targets'] = [self.targets[0], self.targets[3]]
        self.config.pop('components', None) if False else None
        jobs = blocks(self.release())
        self.assertEqual(list(jobs), ['plan', 'build-test', 'build-test-host', 'accept', 'publish'])
        self.assertIn('    needs: [plan, build-test]\n', jobs['build-test-host'])

    def test_authority_is_not_widened_and_untrusted_programs_never_hold_it(self):
        jobs = blocks(self.release())
        self.assertEqual(permissions(jobs['plan']), {'contents': 'read'})
        for name in ['build-test', 'build-test-wave2', 'build-test-host']:
            self.assertEqual(permissions(jobs[name]), {'contents': 'read', 'actions': 'read', 'pull-requests': 'read'}, name)
        self.assertEqual(permissions(jobs['accept']), {'contents': 'read', 'actions': 'read', 'attestations': 'write', 'id-token': 'write'})
        self.assertEqual(permissions(jobs['publish']), {'contents': 'write', 'actions': 'read', 'attestations': 'read'})
        # No job both signs and writes, and only the publisher can write contents.
        for name, block in jobs.items():
            self.assertNotIn('secrets.', block)
            held = permissions(block)
            self.assertFalse(held.get('id-token') == 'write' and held.get('contents') == 'write', name)
        text = self.release()
        self.assertEqual(text.count('id-token: write'), 1)
        self.assertEqual(text.count('contents: write'), 1)
        self.assertEqual(text.count('attest-build-provenance@'), 1)
        self.assertNotIn('workflow_call', text)

    def test_the_jobs_that_run_the_consumers_programs_never_run_in_the_signing_or_publishing_jobs(self):
        jobs = blocks(self.release())
        for name in ['accept', 'publish']:
            self.assertNotIn('nicodes/cookbook/prepare@', jobs[name])
            self.assertNotIn('nicodes/cookbook/build@', jobs[name])
            self.assertNotIn('nicodes/cookbook/test@', jobs[name])
            self.assertNotIn('sdk-setup', jobs[name])
            self.assertNotIn('mise ', jobs[name])
        for name in ['build-test', 'build-test-wave2', 'build-test-host']:
            self.assertNotIn('attest', jobs[name])
            self.assertNotIn('gh release', jobs[name])

    def test_the_identity_is_selected_by_the_same_operation_before_any_build_and_reaches_every_build(self):
        jobs = blocks(self.release())
        plan = jobs['plan']
        self.assertIn('cli_release.py identity --source "$SOURCE_SHA" --cookbook-ref "$COOKBOOK_SHA" --bump "$RELEASE_BUMP"', plan)
        self.assertIn('"$GITHUB_REF" == refs/heads/main && "$GITHUB_EVENT_NAME" == workflow_dispatch', plan)
        self.assertIn('compare/$COOKBOOK_SHA...main', plan)
        for output in ['version', 'tag', 'date']:
            self.assertIn('      ' + output + ': ${{ steps.identity.outputs.' + output + ' }}\n', plan)
        for name in ['build-test', 'build-test-wave2', 'build-test-host']:
            self.assertIn('          version: ${{ needs.plan.outputs.version }}\n          date: ${{ needs.plan.outputs.date }}\n', jobs[name])
            self.assertIn("      COOKBOOK_NATIVE_RELEASE: 'true'\n", jobs[name])
        self.assertIn('RELEASE_TAG: ${{ needs.plan.outputs.tag }}', jobs['accept'])
        self.assertIn('RELEASE_TAG: ${{ needs.plan.outputs.tag }}', jobs['publish'])

    def test_platform_jobs_are_named_like_their_build_and_test_checks_and_export_their_packets(self):
        text = self.release()
        jobs = blocks(text)
        self.assertIn('    name: Build and test (${{ matrix.platform }}-${{ matrix.arch }})\n', jobs['build-test'])
        self.assertIn('    name: Build and test (${{ matrix.platform }}-${{ matrix.arch }} wave 2)\n', jobs['build-test-wave2'])
        self.assertEqual(re.findall(r'platform: (\w+)\n            arch: (\S+)', jobs['build-test']),
                         [('Linux', 'x86_64'), ('Linux', 'arm64'), ('Darwin', 'arm64')])
        for name in ['build-test', 'build-test-wave2']:
            self.assertIn('nicodes/cookbook/export-native@' + PIN, jobs[name])
        self.assertNotIn('export-native', jobs['build-test-host'])
        self.assertIn('nicodes/cookbook/import-native@' + PIN, jobs['build-test-host'])

    def test_the_integration_job_audits_lockfiles_then_uploads_the_accepted_outputs_the_signer_downloads(self):
        host = blocks(self.release())['build-test-host']
        steps = re.findall(r'^      - name: (.*)$', host, flags=re.MULTILINE)
        self.assertEqual(steps, ['Prepare', 'Import native packets', 'Build', 'Test', 'Prepare pinned Rust audit tools',
                                 native_ci.LOCKFILE_AUDIT_STEP, 'Upload accepted outputs and fresh evidence', 'Finish'])
        self.assertIn('          tools: python rust cargo:cargo-audit\n', host)
        self.assertIn('        env:\n          COOKBOOK_ADVISORIES: run\n', host)
        self.assertIn('native_ci.py audit --source "$SOURCE_SHA" --cookbook-ref "$COOKBOOK_SHA"', host)
        self.assertIn('        id: test\n', host)
        self.assertIn('          path: ${{ steps.test.outputs.artifact-paths }}\n', host)
        name = 'cookbook-full-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}'
        self.assertIn('          name: ' + name + '\n', host)
        self.assertIn('          name: ' + name + '\n', blocks(self.release())['accept'])

    def test_the_attestation_and_publication_steps_are_the_multi_job_steps(self):
        legacy_config = copy.deepcopy(self.config)
        text = self.release()
        jobs = blocks(text)
        self.config['ci'] = {}
        legacy = blocks(self.release())
        self.assertEqual(jobs['accept'].replace('needs: [plan, build-test-host]', 'needs: prepare').replace('needs.plan.', 'needs.prepare.'),
                         legacy['accept'])
        # The reusable publish workflow's own steps, inline, in the same order.
        for fragment in ['cli_inputs.py --input publication', 'cli_release.py publish --source "$SOURCE_SHA"']:
            self.assertIn(fragment, jobs['publish'])
        self.assertLess(jobs['publish'].index('--input publication'), jobs['publish'].index('cli_release.py publish'))
        self.assertTrue(legacy_config)

    def test_deadlines_cover_the_release_validation_deadline_and_the_handoff(self):
        text = self.release(validation_timeout=40)
        host = blocks(text)['build-test-host']
        self.assertIn("      COOKBOOK_VALIDATION_TIMEOUT: '40'\n", host)
        entry = next(row for row in self.single()['wave1']['include'] if (row['platform'], row['arch']) == ('Linux', 'x86_64'))
        self.assertIn('    timeout-minutes: ' + str(entry['timeout_minutes'] + 40 + 15) + '\n', host)
        for row in __import__('single_job_workflows').platforms(self.config, release=True):
            self.assertEqual(row['timeout_minutes'], min(180, next(e['timeout_minutes'] for e in self.single()['wave1']['include']
                                                                   if e['id'] == row['id']) + 15))

    def test_the_cargo_sdk_release_mode_skips_the_whole_cli_graph(self):
        text = self.release(sdk_manifest='Cargo.toml')
        jobs = blocks(text)
        self.assertIn("    if: inputs.mode == 'cli'\n", jobs['plan'])
        self.assertIn('prepare-sdk', jobs)
        self.assertIn("inputs.mode == 'sdk'", jobs['publish-sdk'])
        self.assertNotIn('inputs.mode', jobs['build-test'])  # skipped through needs: plan

    def test_product_acceptance_keeps_its_read_only_job_between_the_signer_and_the_final_attestation(self):
        (self.root / 'tests').mkdir(exist_ok=True)
        for name in ['release_check.py', 'release_verify.py']:
            (self.root / 'tests' / name).write_text('pass\n')
        entry = lambda path: {'runner': 'python', 'path': path, 'args': [], 'timeout': 10}
        text = self.release(acceptance={'files': ['accepted.json'], 'checks': [entry('tests/release_check.py')],
                                        'verification': [entry('tests/release_verify.py')]})
        jobs = blocks(text)
        self.assertEqual(list(jobs), ['plan', 'build-test', 'build-test-wave2', 'build-test-host', 'accept', 'product-acceptance',
                                      'attest-evidence', 'publish'])
        self.assertEqual(permissions(jobs['product-acceptance']), {'contents': 'read', 'actions': 'read', 'attestations': 'read'})
        self.assertIn('    needs: [plan, accept]\n', jobs['product-acceptance'])
        self.assertIn('    needs: [plan, accept, product-acceptance]\n', jobs['attest-evidence'])
        self.assertIn('    needs: [plan, attest-evidence]\n', jobs['publish'])
        self.assertIn('artifact-ids: ${{ needs.attest-evidence.outputs.artifact }}', jobs['publish'])
        self.assertEqual(text.count('id-token: write'), 2)

    def test_recipes_without_the_opt_in_generate_the_multi_job_release(self):
        self.config['ci'] = {}
        text = self.release()
        self.assertIn('prepare-cli-release.yml@' + PIN, text)
        self.assertIn('publish-cli.yml@' + PIN, text)
        self.assertNotIn('COOKBOOK_NATIVE_RELEASE', text)
        self.assertNotIn('build-test-host', text)


class Planning(second.SecondWave):
    def test_a_release_always_has_an_integration_job_and_names_its_checks(self):
        self.config['components']['native'] = []
        self.config['package']['native_targets'] = [self.targets[0], self.targets[3]]
        plain = native_ci.single_plan(self.root, self.config, release=False)
        release = native_ci.single_plan(self.root, self.config, release=True)
        self.assertFalse(plain['integration'])
        self.assertTrue(release['integration'])
        self.assertIn(native_ci.RELEASE_REASON, release['integration_reasons'])
        self.assertEqual([row['job'] for row in release['requirements']],
                         ['Build and test (Linux-x86_64)', 'Build and test (Darwin-arm64)'])
        self.assertFalse(native_ci.hosts_groups(release['requirements'][0], release))

    def test_the_job_environment_selects_the_release_declaration(self):
        self.config['components']['native'] = []
        self.config['package']['native_targets'] = [self.targets[0]]
        with patch.dict(os.environ, {native_ci.RELEASE: 'true'}):
            self.assertTrue(native_ci.single_plan(self.root, self.config)['integration'])
        with patch.dict(os.environ, {native_ci.RELEASE: ''}):
            self.assertFalse(native_ci.single_plan(self.root, self.config)['integration'])
        with patch.dict(os.environ, {native_ci.RELEASE: 'yes'}), self.assertRaisesRegex(ValueError, 'must be true'):
            native_ci.single_plan(self.root, self.config)


class Verification(publication.CliPublication):
    """The release verifier reads a one-job release's proof against the one-job graph."""

    def setUp(self):
        super().setUp()
        self.config['ci'] = {'single_job': True}
        self.single = native_ci.single_plan(self.root, self.config, release=True, environment=self.environment)
        self.plan = self.single
        requirements = self.single['requirements']
        import tempfile
        from pathlib import Path
        self.proofs = []
        with tempfile.TemporaryDirectory() as directory:
            for entry in requirements:
                target = Path(directory) / entry['id']; target.mkdir()
                value = self.fixture.packet(entry, target)
                value['execution_context'].update(self.environment, GITHUB_JOB=native_ci.SINGLE_JOBS[entry['wave']])
                value['version_identity'] = '0.0.11'
                self.proofs.append(value)
        self.jobs = {'total_count': 0, 'jobs': []}
        for index, entry in enumerate(requirements):
            self.jobs['jobs'].append({'id': index + 1, 'name': entry['job'], 'status': 'completed',
                                      'conclusion': 'success', 'labels': [entry['runner']]})
        self.integration = {'id': 90, 'name': native_ci.INTEGRATION, 'status': 'completed', 'conclusion': 'success',
            'labels': ['ubuntu-24.04'], 'run_id': int(self.environment['GITHUB_RUN_ID']),
            'run_attempt': int(self.environment['GITHUB_RUN_ATTEMPT']),
            'steps': [{'name': native_ci.LOCKFILE_AUDIT_STEP, 'status': 'completed', 'conclusion': 'success'}]}
        self.jobs['jobs'].append(self.integration)
        self.jobs['total_count'] = len(self.jobs['jobs'])
        accepted = native_ci.validate_jobs(requirements, self.jobs)
        self.matrix = {'version': 1, 'requirements': requirements, 'host_groups': self.single['host_groups'],
            'source_commit': self.fixture.source, **self.matrix_binding(), 'proofs': self.proofs, 'jobs': accepted,
            'lockfile_audit': None}
        self.proof.update(native_matrix=self.matrix, coverage='single-job-native-matrix',
                          test_groups=sorted(self.single['host_groups']),
                          execution_context=self.environment | {'GITHUB_JOB': native_ci.SINGLE_HOST_JOB})

    def matrix_binding(self):
        return {key: self.proof[key] for key in native_ci.dependencies.BINDING_KEYS}

    def test_the_one_job_graph_is_accepted_as_the_complete_release_proof(self):
        self.assertEqual(sorted(self.accept()), sorted(self.proof['artifacts'].keys() and
                         [name.removeprefix('dist/') for name in self.proof['artifacts']]))

    def refuse(self, pattern=None):
        with self.assertRaisesRegex(ValueError, pattern or '.'):
            self.accept()

    def test_the_multi_job_coverage_cannot_stand_in_for_the_one_job_proof(self):
        self.proof['coverage'] = 'complete-host-and-native-matrix'
        self.refuse('complete exact-source host and native proof')

    def test_the_one_job_coverage_cannot_stand_in_for_a_multi_job_proof(self):
        self.config['ci'] = {}
        self.refuse()

    def test_the_proof_must_come_from_the_integration_job_of_this_run(self):
        self.proof['execution_context']['GITHUB_JOB'] = 'validate'
        self.refuse('another matrix wave')

    def test_a_platform_proof_from_another_job_id_is_refused(self):
        self.matrix['proofs'][0]['execution_context']['GITHUB_JOB'] = 'native_one'
        self.refuse('another matrix wave')

    def test_every_platform_job_must_have_succeeded_on_its_runner(self):
        for change in [{'conclusion': 'failure'}, {'status': 'in_progress'}, {'labels': ['other']}]:
            with self.subTest(change=change):
                saved = copy.deepcopy(self.jobs['jobs'][0])
                self.jobs['jobs'][0].update(change)
                self.refuse('every declared native matrix job')
                self.jobs['jobs'][0] = saved
        self.jobs['jobs'] = self.jobs['jobs'][1:]
        self.jobs['total_count'] -= 1
        self.refuse('every declared native matrix job')

    def test_the_integration_job_must_be_the_one_successful_job_on_the_primary_runner(self):
        for change in [{'conclusion': 'failure'}, {'status': 'queued'}, {'labels': ['ubuntu-24.04-arm']}]:
            with self.subTest(change=change):
                saved = copy.deepcopy(self.integration)
                self.integration.update(change)
                self.refuse()
                self.integration.clear(); self.integration.update(saved)
        self.jobs['jobs'].append({**self.integration, 'id': 91})
        self.jobs['total_count'] += 1
        self.refuse()

    def test_the_lockfile_audit_is_the_integration_jobs_own_passing_step_of_this_attempt(self):
        saved = copy.deepcopy(self.integration)
        for change in [{'steps': []}, {'steps': [{'name': native_ci.LOCKFILE_AUDIT_STEP, 'status': 'completed', 'conclusion': 'skipped'}]},
                       {'steps': [{'name': native_ci.LOCKFILE_AUDIT_STEP, 'status': 'completed', 'conclusion': 'failure'}]},
                       {'run_attempt': 99}, {'run_id': 1}]:
            with self.subTest(change=change):
                self.integration.update(change)
                self.refuse()
                self.integration.clear(); self.integration.update(saved)
        self.accept()

    def test_the_matrix_cannot_claim_an_earlier_audit_job(self):
        self.matrix['lockfile_audit'] = {'id': 1, 'name': 'Plan'}
        self.refuse('records no earlier lockfile audit')

    def test_a_proof_of_the_multi_job_names_is_refused_against_the_one_job_declaration(self):
        legacy = native_ci.plan(self.root, self.config, self.environment)
        self.matrix['requirements'] = legacy['requirements']
        self.refuse('differs from the candidate source contract')


# The fixture's own tests belong to its module; this class only reuses its setup.
for _name in [name for name in dir(publication.CliPublication) if name.startswith('test_') and name not in Verification.__dict__]:
    setattr(Verification, _name, None)


if __name__ == '__main__':
    unittest.main()
