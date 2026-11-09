"""Real job identity and immutable data guard privileged release operations."""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_cli_publication as publication

release = publication.release


class ReleaseAuthority(unittest.TestCase):
    def setUp(self):
        self.fixture = publication.CliPublication()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.configure_product_assertions()
        self.root, self.config = self.fixture.root, self.fixture.config
        self.source, self.tools = self.fixture.fixture.source, self.fixture.fixture.tools
        self.environment = self.fixture.environment | {'GITHUB_JOB': 'product-acceptance'}
        self.now = release.time.time()
        stamp = lambda offset: datetime.fromtimestamp(self.now + offset, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.archive = {'id': 91, 'name': 'Attest accepted source archives', 'head_sha': self.source,
            'run_id': int(self.environment['GITHUB_RUN_ID']), 'labels': ['ubuntu-24.04'],
            'started_at': stamp(-5), 'completed_at': stamp(-4), 'status': 'completed', 'conclusion': 'success'}
        self.product = {**self.archive, 'id': 92, 'name': 'Verify product release', 'started_at': stamp(-3),
            'completed_at': stamp(-1)}
        self.jobs = copy.deepcopy(self.fixture.jobs)
        self.jobs['jobs'].extend([self.archive, self.product]); self.jobs['total_count'] += 2

    def run_product(self):
        self.product.update(status='in_progress', conclusion=None, completed_at=None)
        with patch.dict(os.environ, self.environment), patch.object(release.ci, 'identity', return_value=self.fixture.fixture.tree), \
                patch.object(release.native_ci, 'github', return_value=self.jobs):
            release.accept_domain(self.root, self.config, self.fixture.proof, self.source, self.tools, 'cli-v0.0.11')
        self.product.update(status='completed', conclusion='success', completed_at=self.archive['completed_at'])
        # The product job must finish after it started, independently of the signer.
        self.product['completed_at'] = datetime.fromtimestamp(self.now - 1, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    def admit(self):
        original = {name.removeprefix('dist/'): value for name, value in self.fixture.proof['artifacts'].items()}
        return release.accepted_evidence(self.root, self.config, self.source, self.tools, 'cli-v0.0.11', self.environment,
            proof=self.fixture.proof, original=original, jobs=self.jobs)

    def test_authority_refuses_product_execution_outside_readonly_job(self):
        for job in ['accept', 'attest-evidence', 'publish']:
            with self.subTest(job=job), patch.dict(os.environ, self.environment | {'GITHUB_JOB': job}), \
                    patch.object(release, 'run_assertions', side_effect=AssertionError('executed')):
                with self.assertRaisesRegex(ValueError, 'separate read-only'):
                    release.accept_domain(self.root, self.config, self.fixture.proof, self.source, self.tools, 'cli-v0.0.11')

    def test_real_completed_job_source_run_runner_and_single_identity_are_required(self):
        release.authority_jobs(self.environment, self.source, jobs=self.jobs, completed=True)
        for change in [{'head_sha': 'f'*40}, {'run_id': 999}, {'labels': ['self-hosted', 'ubuntu-24.04']},
                       {'conclusion': 'failure'}, {'status': 'in_progress'}, {'completed_at': None}]:
            with self.subTest(change=change):
                jobs = copy.deepcopy(self.jobs); jobs['jobs'][-1].update(change)
                with self.assertRaises(ValueError):
                    release.authority_jobs(self.environment, self.source, jobs=jobs, completed=True)
        for jobs in [self.jobs | {'total_count': 101}, self.jobs | {'jobs': self.jobs['jobs'] + [self.product], 'total_count': self.jobs['total_count']+1}]:
            with self.assertRaises(ValueError): release.authority_jobs(self.environment, self.source, jobs=jobs, completed=True)

    def test_declared_deadline_starts_at_actual_archive_job_and_closes_after_attestation(self):
        expected = release.timestamp(self.archive['started_at']) + 600
        self.assertEqual(release.acceptance_deadline(self.config, self.archive, self.product, completed=True), expected)
        with patch.object(release.time, 'time', return_value=expected):
            with self.assertRaisesRegex(ValueError, 'aggregate deadline'):
                release.acceptance_deadline(self.config, self.archive, self.product, completed=True)
        late = self.product | {'completed_at': datetime.fromtimestamp(expected+1, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
        with self.assertRaisesRegex(ValueError, 'completed after'):
            release.acceptance_deadline(self.config, self.archive, late, completed=True)

    def test_source_assertions_share_remaining_budget_instead_of_resetting_it(self):
        import native_tests
        with patch.object(release.time, 'time', return_value=100), patch.object(native_tests, 'run_entries') as entries:
            release.run_assertions(self.root, self.config, 'cli-v0.0.11', self.source, 'checks', deadline=103)
        self.assertEqual(entries.call_args.args[1]['tests']['release'][0]['timeout'], 3)
        with patch.object(release.time, 'time', return_value=104), patch.object(native_tests, 'run_entries') as entries:
            with self.assertRaisesRegex(ValueError, 'aggregate deadline'):
                release.run_assertions(self.root, self.config, 'cli-v0.0.11', self.source, 'verification', deadline=103)
            entries.assert_not_called()

    def test_data_admission_never_executes_source_and_binds_original_inputs_and_job(self):
        self.run_product()
        with patch.object(release, 'run_assertions', side_effect=AssertionError('privileged source execution')):
            self.assertEqual(set(self.admit()), {'accepted.json'})
        path = self.root / '.artifacts/cli-publication-evidence/receipt.json'
        original = json.loads(path.read_bytes())
        for key, value in [('source_commit', 'f'*40), ('proof_sha256', '0'*64), ('input_artifacts', {}),
                           ('product_job', {'id': 999, 'started_at': self.product['started_at']}), ('private', 'forbidden')]:
            with self.subTest(key=key):
                path.write_bytes(release.canonical(original | {key: value}))
                with self.assertRaises(ValueError): self.admit()
        path.write_bytes(release.canonical(original))
        (path.parent/'extra.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'only declared evidence'): self.admit()

    def test_changed_evidence_even_with_successful_job_is_refused(self):
        self.run_product()
        (self.root/'.artifacts/cli-publication-evidence/accepted.json').write_text('{"changed":true}')
        with self.assertRaisesRegex(ValueError, 'same-run accepted source'): self.admit()

    def test_every_evidence_subject_is_verified_before_pure_publication(self):
        with patch.object(release.subprocess, 'run') as execute, patch.object(release, 'authority_jobs', return_value=(self.archive,self.product)):
            release.verify_evidence_attestations(self.root, self.config, self.source, self.environment)
        command = execute.call_args.args[0]
        self.assertEqual(command[:3], ['gh','attestation','verify'])
        self.assertEqual(command[3], str(self.root/'dist/accepted.json'))
        self.assertEqual(command[command.index('--signer-digest')+1], self.source)
        self.assertIn('--deny-self-hosted-runners', command)
        with patch.object(release.subprocess, 'run', side_effect=ValueError('untrusted attestation')):
            with self.assertRaisesRegex(ValueError, 'untrusted attestation'):
                release.verify_evidence_attestations(self.root, self.config, self.source, self.environment)
