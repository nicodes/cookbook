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

    def test_the_plan_job_audits_lockfiles_before_any_platform_job_and_the_integration_job_uploads(self):
        jobs = blocks(self.release())
        steps = re.findall(r'^      - name: (.*)$', jobs['plan'], flags=re.MULTILINE)
        self.assertEqual(steps[-3:], ['Prepare pinned Rust audit tools', native_ci.LOCKFILE_AUDIT_STEP,
                                      'Select unused native release identity without mutation'])
        self.assertIn('          tools: python rust cargo:cargo-audit\n', jobs['plan'])
        self.assertIn('native_ci.py audit --source "$SOURCE_SHA" --cookbook-ref "$COOKBOOK_SHA"', jobs['plan'])
        self.assertEqual(permissions(jobs['plan']), {'contents': 'read'})
        self.assertNotIn('audit', jobs['build-test-host'].replace('advisories', ''))
        host = jobs['build-test-host']
        self.assertEqual(re.findall(r'^      - name: (.*)$', host, flags=re.MULTILINE),
                         ['Prepare', 'Import native packets', 'Build', 'Test', 'Upload accepted outputs and fresh evidence', 'Finish'])
        self.assertIn('        id: test\n', host)
        self.assertIn('          path: ${{ steps.test.outputs.artifact-paths }}\n', host)
        name = 'cookbook-full-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}'
        self.assertIn('          name: ' + name + '\n', host)
        self.assertIn('          name: ' + name + '\n', jobs['accept'])

    def test_only_the_integration_job_runs_with_advisory_scans_on_and_no_other_job_skips_them_silently(self):
        jobs = blocks(self.release())
        self.assertIn('          date: ${{ needs.plan.outputs.date }}\n          advisories: run\n', jobs['build-test-host'])
        for name in ['build-test', 'build-test-wave2']:
            self.assertNotIn('advisories', jobs[name])

    def test_the_attestation_and_publication_steps_are_the_multi_job_steps(self):
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

    def test_deadlines_cover_the_release_validation_deadline_and_the_handoff(self):
        text = self.release(validation_timeout=40)
        host = blocks(text)['build-test-host']
        self.assertIn("      COOKBOOK_VALIDATION_TIMEOUT: '40'\n", host)
        entry = next(row for row in self.single()['wave1']['include'] if (row['platform'], row['arch']) == ('Linux', 'x86_64'))
        self.assertIn('    timeout-minutes: ' + str(entry['timeout_minutes'] + 40 + 30) + '\n', host)  # packets in, outputs out
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
        self.configure()
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
        self.audit = {**self.integration, 'id': 89, 'name': native_ci.RELEASE_PLAN, 'labels': ['ubuntu-24.04']}
        self.integration = {key: value for key, value in self.integration.items() if key != 'steps'} | {'steps': []}
        self.jobs['jobs'].extend([self.audit, self.integration])
        self.jobs['total_count'] = len(self.jobs['jobs'])
        accepted = native_ci.validate_jobs(requirements, self.jobs)
        self.matrix = {'version': 1, 'requirements': requirements, 'host_groups': self.single['host_groups'],
            'source_commit': self.fixture.source, **self.matrix_binding(), 'proofs': self.proofs, 'jobs': accepted,
            'lockfile_audit': None}
        self.proof.update(native_matrix=self.matrix, coverage='single-job-native-matrix',
                          test_groups=sorted(self.single['host_groups']),
                          execution_context=self.environment | {'GITHUB_JOB': native_ci.SINGLE_HOST_JOB})

    def configure(self):
        pass

    def matrix_binding(self):
        return {key: self.proof[key] for key in native_ci.dependencies.BINDING_KEYS}

    def test_the_one_job_graph_is_accepted_as_the_complete_release_proof(self):
        self.assertEqual(sorted(self.accept()), sorted(self.proof['artifacts'].keys() and
                         [name.removeprefix('dist/') for name in self.proof['artifacts']]))

    def refuse(self, pattern=None):
        with self.assertRaisesRegex(ValueError, pattern or '.'):
            self.accept()

    def test_a_proof_that_skipped_advisory_scans_is_refused(self):
        self.proof['advisories'] = 'skip'
        self.refuse('must run every advisory scan')
        del self.proof['advisories']
        self.accept()

    def test_the_multi_job_coverage_cannot_stand_in_for_the_one_job_proof(self):
        self.proof['coverage'] = 'complete-host-and-native-matrix'
        self.refuse('complete exact-source host and native proof')

    def test_the_one_job_coverage_cannot_stand_in_for_a_multi_job_proof(self):
        self.config['ci'] = {}
        self.refuse('complete exact-source host and native proof')

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
                self.refuse('actual successful complete host job|every declared native matrix job')
                self.integration.clear(); self.integration.update(saved)
        self.jobs['jobs'].append({**self.integration, 'id': 91})
        self.jobs['total_count'] += 1
        self.refuse('actual successful complete host job')

    def test_the_lockfile_audit_is_the_plan_jobs_own_passing_step_of_this_attempt(self):
        saved = copy.deepcopy(self.audit)
        for change in [{'steps': []}, {'steps': [{'name': native_ci.LOCKFILE_AUDIT_STEP, 'status': 'completed', 'conclusion': 'skipped'}]},
                       {'steps': [{'name': native_ci.LOCKFILE_AUDIT_STEP, 'status': 'completed', 'conclusion': 'failure'}]},
                       {'run_attempt': 99}, {'run_id': 1}]:
            with self.subTest(change=change):
                self.audit.update(change)
                self.refuse('lockfile audit|Rust lockfile audit')
                self.audit.clear(); self.audit.update(saved)
        self.accept()

    def test_the_matrix_cannot_claim_an_earlier_audit_job(self):
        self.matrix['lockfile_audit'] = {'id': 1, 'name': native_ci.RELEASE_PLAN}
        self.refuse('records no earlier lockfile audit')

    def test_a_proof_of_the_multi_job_names_is_refused_against_the_one_job_declaration(self):
        legacy = native_ci.plan(self.root, self.config, self.environment)
        self.matrix['requirements'] = legacy['requirements']
        self.refuse('differs from the candidate source contract')


class SecondWaveVerification(Verification):
    """The same, for a recipe whose CLI consumes the host binaries of both Linux architectures."""

    def configure(self):
        self.config['components']['native'] = [
            {'name': 'host', 'role': 'host', 'component': 'cli', 'binary': 'host', 'output': '.artifacts/native-host/host_{platform}_{arch}'},
            {'name': 'cli', 'role': 'cli', 'component': 'cli', 'binary': 'example', 'output': '.artifacts/native-cli/example_{platform}_{arch}',
             'needs': [{'component': 'host', 'platform': 'Linux/x86_64', 'path_environment': 'HOST_AMD64'},
                       {'component': 'host', 'platform': 'Linux/arm64', 'path_environment': 'HOST_ARM64'}]}]

    def test_both_waves_are_checked_against_the_one_job_ids_and_names(self):
        self.assertEqual({row['wave'] for row in self.single['requirements']}, {1, 2})
        self.assertIn('Build and test (Linux-x86_64 wave 2)', [row['name'] for row in self.jobs['jobs']])
        self.accept()
        second = next(proof for proof in self.matrix['proofs'] if proof['entry']['wave'] == 2)
        second['execution_context']['GITHUB_JOB'] = native_ci.SINGLE_JOBS[1]
        self.refuse('another matrix wave')

    def test_a_missing_second_wave_job_is_refused(self):
        self.jobs['jobs'] = [row for row in self.jobs['jobs'] if not row['name'].endswith('wave 2)')]
        self.jobs['total_count'] = len(self.jobs['jobs'])
        self.refuse('every declared native matrix job')


class Legacy(publication.CliPublication):
    """Recipes without the opt-in keep the exact multi-job Release of main."""

    # sha256 of the Release workflow that origin/main generates for the fixture recipe at revision 'e' * 40.
    GOLDEN = {'plain': '01d7dfdfbf5208538e288339777b659a4969b242982b1b45b128769aa76fe86a',
              'sdk': '57398909d87e16b49b30a422c5d6105a00869b144912ae3a481a86dbaa8ed603',
              'acceptance': '151192a75c6b9b29da00fb2873a2bb0ed6c8b4f1311ed68ec378ea869de83f88'}

    def release(self, **values):
        self.config.pop('ci', None)
        self.config['publication'] = {**PUBLICATION, **values}
        with patch.object(workflows, 'load', return_value=self.config):
            return workflows.workflow_files(self.root, PIN)[RELEASE]

    def test_the_multi_job_release_is_byte_identical_to_main(self):
        import hashlib
        digest = lambda text: hashlib.sha256(text.encode()).hexdigest()
        self.assertEqual(digest(self.release()), self.GOLDEN['plain'])
        self.assertEqual(digest(self.release(sdk_manifest='Cargo.toml')), self.GOLDEN['sdk'])
        self.configure_product_assertions()
        acceptance = self.config['publication']['acceptance']
        self.assertEqual(digest(self.release(acceptance=acceptance)), self.GOLDEN['acceptance'])

    def test_recipe_filenames_are_never_taken_for_the_one_job_tokens(self):
        self.configure_product_assertions()
        acceptance = {**self.config['publication']['acceptance'], 'files': ['AUDIT.json', 'RELEASE_IDENTITY.json'],
                      'diagnostics': ['STAGE_PUBLICATION.json', 'PUBLISH_RUN.json']}
        text = self.release(acceptance=acceptance)
        for name in acceptance['files'] + acceptance['diagnostics']:
            self.assertIn('.artifacts/cli-publication-evidence/' + name + '\n', text)
        self.assertNotIn('native_ci.py audit', text)
        self.assertNotIn('cli_release.py identity', text)


# The fixture's own tests belong to its module; this class only reuses its setup.
for _class in (Verification, SecondWaveVerification, Legacy):
    for _name in [name for name in dir(publication.CliPublication) if name.startswith('test_') and name not in _class.__dict__]:
        setattr(_class, _name, None)


if __name__ == '__main__':
    unittest.main()
