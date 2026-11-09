from source_fixture import SDK_ROOT
import dependencies
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import cli_release as release
import cli_publication_workflows as workflows
import test_native_ci


class CliPublication(unittest.TestCase):
    def setUp(self):
        fixture = test_native_ci.NativeMatrix()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.root = fixture, fixture.root
        self.config = fixture.config
        self.config['package'].update(version_arguments=['--version'], version_template='{binary} {version} ({commit})')
        self.config.update(version=1, publication={'tag_prefix': 'cli-v', 'initial_version': '0.0.10', 'initial': 'bump'})
        self.environment = fixture.environment | {'GITHUB_SHA': fixture.source, 'GITHUB_REF': 'refs/heads/main',
            'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_WORKFLOW_REF': 'owner/project/.github/workflows/release.yml@refs/heads/main'}
        self.plan = fixture.declaration()
        self.proofs = []
        with tempfile.TemporaryDirectory() as directory:
            for entry in self.plan['requirements']:
                target = Path(directory) / entry['id']; target.mkdir()
                value = fixture.packet(entry, target)
                value['execution_context'].update(self.environment)
                value['version_identity'] = '0.0.11'
                self.proofs.append(value)
        self.jobs = fixture.jobs(self.plan['requirements'])
        accepted = release.native_ci.validate_jobs(self.plan['requirements'], self.jobs)
        self.jobs['jobs'].append({'id': 90, 'name': 'Release / Full host validation / Make full',
            'status': 'completed', 'conclusion': 'success', 'labels': ['ubuntu-24.04']})
        self.jobs['total_count'] += 1
        self.matrix = {'version': 1, 'requirements': self.plan['requirements'], 'host_groups': self.plan['host_groups'],
            'source_commit': fixture.source, **dependencies.binding(fixture.tools), 'proofs': self.proofs, 'jobs': accepted}
        self.proof = {'recipe': 'rust-cli', 'source_commit': fixture.source, 'source_tree': fixture.tree,
            **dependencies.binding(fixture.tools), 'configuration_sha256': self.proofs[0]['configuration_sha256'],
            'status': 'passed', 'tracked_source_clean': True, 'build_verified': True, 'tests_verified': True,
            'test_groups': sorted(self.plan['host_groups']), 'coverage': 'complete-host-and-native-matrix',
            'native_matrix': self.matrix, 'execution_context': self.environment | {'GITHUB_JOB': 'validate'}, 'artifacts': {}}
        (self.root / 'dist').mkdir()
        executable = self.root / 'compiled'; executable.write_bytes(b'exact native executable')
        native = release.rust_cli.helper('native_cli')
        for target in self.config['package']['native_targets']:
            archive = native.write_archive(self.root, executable, self.config['package'], target, self.root / 'dist')
            self.proof['artifacts']['dist/' + archive.name] = native.digest(archive)
        names = [native.archive_name(self.config['package'], target) for target in self.config['package']['native_targets']]
        (self.root / 'dist/checksums.txt').write_text(native.checksums(self.root / 'dist', names))
        self.proof['artifacts']['dist/checksums.txt'] = native.digest(self.root / 'dist/checksums.txt')

    def accept(self):
        with patch.object(release.ci, 'identity', return_value=self.fixture.tree):
            return release.accepted_payload(self.root, self.config, self.proof, self.fixture.source,
                self.fixture.tools, 'cli-v0.0.11', self.environment, self.jobs)

    def test_complete_six_platform_release_is_accepted_only_as_closed_exact_bytes(self):
        self.assertEqual(len(self.accept()), 7)
        archive = next((self.root / 'dist').glob('*.zip'))
        archive.write_bytes(b'changed after full validation')
        with self.assertRaisesRegex(ValueError, 'bytes differ'):
            self.accept()

    def test_partial_other_attempt_missing_platform_and_failed_host_cannot_publish(self):
        for mutation in ['partial', 'attempt', 'platform', 'host', 'version', 'extra']:
            original_proof, original_jobs = copy.deepcopy(self.proof), copy.deepcopy(self.jobs)
            if mutation == 'partial': self.proof['coverage'] = 'native-partial'
            elif mutation == 'attempt': self.proof['execution_context']['GITHUB_RUN_ATTEMPT'] = '99'
            elif mutation == 'platform': self.proof['native_matrix']['proofs'].pop()
            elif mutation == 'host': self.jobs['jobs'][-1]['conclusion'] = 'skipped'
            elif mutation == 'version': self.proof['native_matrix']['proofs'][0]['version_identity'] = '0.0.12'
            elif mutation == 'extra': (self.root / 'dist/extra.txt').write_text('unexpected')
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): self.accept()
            self.proof, self.jobs = original_proof, original_jobs
            (self.root / 'dist/extra.txt').unlink(missing_ok=True)

    def test_companion_initial_fallback_tag_guards_and_cargo_version_contract(self):
        self.assertEqual(release.select_version(self.root, self.config, 'patch', [])['tag'], 'cli-v0.0.11')
        self.assertEqual(release.select_version(self.root, self.config, 'minor', ['cli-v2.3.4', 'gd-v99.0.0'])['version'], '2.4.0')
        with self.assertRaises(ValueError): release.select_version(self.root, self.config, 'unknown', [])
        self.config['publication'] = {'initial': 'bump', 'cargo_manifest': 'Cargo.toml', 'version_identity': 'tag'}
        self.assertEqual(release.select_version(self.root, self.config, 'patch', [])['version'], 'v0.0.1')
        with self.assertRaisesRegex(ValueError, 'Cargo package version'):
            release.select_version(self.root, self.config, 'minor', [])
        for name in ['cargo_manifest', 'commands']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.policy(self.config | {'publication': {name: '../outside/Cargo.toml' if name == 'cargo_manifest' else {'publish': 'anything'}}})

    def test_source_workflow_identity_and_dispatch_controls_are_preserved(self):
        release.context(self.fixture.source, self.environment)
        for changes in [{'GITHUB_REF': 'refs/heads/feature'}, {'GITHUB_EVENT_NAME': 'pull_request'},
                        {'GITHUB_WORKFLOW_REF': 'nicodes/tools/.github/workflows/release.yml@refs/heads/main'},
                        {'GITHUB_SHA': 'f' * 40}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                release.context(self.fixture.source, self.environment | changes)

    def test_existing_remote_identity_refuses_release_without_tag_or_release_mutation(self):
        calls = []
        with patch.dict(os.environ, self.environment), patch.object(release.ci, 'identity'), \
                patch.object(release.subprocess, 'run') as native, \
                patch.object(release.subprocess, 'check_output', return_value=''), \
                patch.object(release.github_release, 'absent', side_effect=ValueError('identity already exists')):
            with self.assertRaisesRegex(ValueError, 'already exists'):
                release.identity(self.root, self.config, self.fixture.source, self.fixture.tools, 'patch')
        self.assertEqual(native.call_args.args[0], ['git', 'fetch', '--tags', '--force'])

    def test_generated_caller_keeps_attestation_in_source_and_uses_shared_native_gate(self):
        from test_dependency_security import read_yaml
        with patch.object(workflows, 'load', return_value=self.config):
            text = workflows.workflow_files(self.root, 'e' * 40)['.github/workflows/release.yml']
        self.assertIn('uses: nicodes/cookbook/.github/workflows/cookbook-native.yml@' + 'e' * 40, text)
        self.assertIn('uses: ' + workflows.ATTEST, text)
        self.assertNotIn('mise run', text)
        self.assertNotIn('scripts/', text)
        self.assertIn('cookbook-full-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}', text)
        jobs = read_yaml(text)['jobs']
        self.assertNotIn('product-acceptance', jobs)
        self.assertEqual(jobs['publish']['needs'], ['identity', 'accept'])
        self.assertIn('accept --source', next(step['run'] for step in jobs['accept']['steps']
            if step['name'].startswith('Recheck complete')))
        self.assertNotIn('sdk-setup', text)

    def configure_product_assertions(self):
        (self.root / 'tests/release_check.py').write_text('import json,sys\nfrom pathlib import Path\n'
            'Path(sys.argv[3]).mkdir(mode=0o700)\n'
            'Path(sys.argv[3],"accepted.json").write_text(json.dumps({"accepted":True,"source":sys.argv[1],"tag":sys.argv[2]}))\n')
        (self.root / 'tests/release_verify.py').write_text('import json,sys\nfrom pathlib import Path\n'
            'value=json.loads(Path(sys.argv[3],"accepted.json").read_text())\n'
            'assert value=={"accepted":True,"source":sys.argv[1],"tag":sys.argv[2]}, "owned release evidence refused"\n')
        def entry(path):
            return {'runner': 'python', 'path': path, 'args': ['{source}', '{tag}', '{evidence}'], 'timeout': 10}
        self.config['publication']['acceptance'] = {'files': ['accepted.json'],
            'checks': [entry('tests/release_check.py')], 'verification': [entry('tests/release_verify.py')]}

    def test_product_acceptance_preserves_native_bytes_and_authority_only_reads_evidence(self):
        from datetime import datetime, timezone
        self.configure_product_assertions()
        before = {path.name: path.read_bytes() for path in (self.root / 'dist').iterdir()}
        environment = self.environment | {'GITHUB_JOB': 'product-acceptance'}
        now = release.time.time()
        stamp = lambda offset: datetime.fromtimestamp(now + offset, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        archive = {'id': 91, 'name': 'Attest accepted source archives', 'head_sha': self.fixture.source,
            'run_id': int(environment['GITHUB_RUN_ID']), 'labels': ['ubuntu-24.04'],
            'started_at': stamp(-5), 'completed_at': stamp(-4), 'status': 'completed', 'conclusion': 'success'}
        product = {**archive, 'id': 92, 'name': 'Verify product release', 'started_at': stamp(-3),
            'completed_at': None, 'status': 'in_progress', 'conclusion': None}
        self.jobs['jobs'].extend([archive, product]); self.jobs['total_count'] += 2
        with patch.dict(os.environ, environment), patch.object(release.ci, 'identity', return_value=self.fixture.tree), \
                patch.object(release.native_ci, 'github', return_value=self.jobs):
            accepted = release.accept_domain(self.root, self.config, self.proof, self.fixture.source, self.fixture.tools, 'cli-v0.0.11')
            self.assertEqual(set(accepted), {'accepted.json'})
            self.assertEqual(before, {path.name: path.read_bytes() for path in (self.root / 'dist').iterdir()})
            product.update(status='completed', conclusion='success', completed_at=stamp(-1))
            with patch.object(release, 'run_assertions', side_effect=AssertionError('authority executed product code')):
                result = release.accepted_payload(self.root, self.config, self.proof, self.fixture.source, self.fixture.tools,
                    'cli-v0.0.11', environment, self.jobs)
            self.assertEqual(len(result), 8)
            path = self.root / '.artifacts/cli-publication-evidence/receipt.json'
            receipt = json.loads(path.read_text()); receipt['execution_context']['GITHUB_RUN_ATTEMPT'] = '99'
            path.write_bytes(release.canonical(receipt))
            with self.assertRaisesRegex(ValueError, 'another Actions execution'):
                release.accepted_payload(self.root, self.config, self.proof, self.fixture.source, self.fixture.tools,
                    'cli-v0.0.11', environment, self.jobs)

    def test_owned_post_attestation_steps_and_diagnostics_remain_in_source_signer_workflow(self):
        from test_dependency_security import read_yaml
        self.configure_product_assertions()
        self.config['publication']['acceptance']['diagnostics'] = ['status.json']
        self.config['publication']['acceptance_timeout'] = 55
        with patch.object(workflows, 'load', return_value=self.config):
            text = workflows.workflow_files(self.root, 'e' * 40)['.github/workflows/release.yml']
        self.assertLess(text.index('Attest build provenance'), text.index('Run owned lifecycle assertions'))
        self.assertLess(text.index('Run owned lifecycle assertions'), text.index('Attest accepted product release evidence'))
        self.assertIn('dist/accepted.json', text)
        self.assertIn('.artifacts/cli-publication-evidence/receipt.json', text)
        self.assertIn("failure() && steps.domain.outcome == 'failure'", text)
        jobs = read_yaml(text)['jobs']
        product = jobs['product-acceptance']
        self.assertEqual(product['name'], 'Verify product release')
        self.assertEqual(product['permissions'], {'contents':'read','actions':'read','attestations':'read'})
        for name in ['accept','product-acceptance','attest-evidence']:
            self.assertEqual(jobs[name]['timeout-minutes'], 55)
        for name in ['accept','attest-evidence']:
            self.assertEqual(jobs[name]['permissions']['id-token'], 'write')
            self.assertFalse(any(' acceptance --source' in step.get('run','') for step in jobs[name]['steps']))
            self.assertFalse(any('sdk-setup' in step.get('uses','') for step in jobs[name]['steps']))
        domain = next(step for step in product['steps'] if step.get('id')=='domain')
        self.assertIn(' acceptance --source', domain['run'])
        artifact = next(step for step in product['steps'] if step.get('id')=='evidence')
        self.assertEqual(artifact['with']['path'].splitlines(), [
            '.artifacts/cli-publication-evidence/receipt.json', '.artifacts/cli-publication-evidence/accepted.json'])
        signer = jobs['attest-evidence']
        downloads = [step['with'] for step in signer['steps'] if step.get('uses')==workflows.DOWNLOAD]
        self.assertEqual(downloads, [
            {'artifact-ids':'${{ needs.accept.outputs.artifact }}','path':'.artifacts/cli-input/archives'},
            {'artifact-ids':'${{ needs.product-acceptance.outputs.artifact }}','path':'.artifacts/cli-input/product'}])
        for job in ['accept', 'product-acceptance', 'attest-evidence']:
            self.assertTrue(all(step['with']['path'].startswith('.artifacts/cli-input/') for step in jobs[job]['steps']
                if step.get('uses') == workflows.DOWNLOAD))
            self.assertTrue(any('cli_inputs.py --input' in step.get('run','') for step in jobs[job]['steps']))
        self.assertEqual(jobs['publish']['needs'], ['identity','attest-evidence'])
        for files in [['receipt.json'], ['../outside.json'], ['ordinary.txt']]:
            self.config['publication']['acceptance']['files'] = files
            with self.subTest(files=files), self.assertRaises(ValueError): release.policy(self.config)

    def test_existing_native_signer_verification_keeps_exact_workflow_source_and_ref(self):
        self.config['publication']['attestation_platforms'] = ['Linux/x86_64']
        original = release.subprocess.run
        calls = []
        def execute(argv, *args, **kwargs):
            if argv[0] == 'gh':
                calls.append(argv)
                return release.subprocess.CompletedProcess(argv, 0)
            return original(argv, *args, **kwargs)
        with patch.object(release.subprocess, 'run', side_effect=execute):
            self.accept()
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertEqual(argv[:3], ['gh', 'attestation', 'verify'])
        self.assertIn('https://github.com/owner/project/.github/workflows/release.yml@refs/heads/main', argv)
        self.assertEqual(argv[argv.index('--source-digest') + 1], self.fixture.source)
        self.assertEqual(argv[argv.index('--signer-digest') + 1], self.fixture.source)
        self.assertIn('--deny-self-hosted-runners', argv)
        self.config['publication']['attestation_platforms'] = ['undeclared/native']
        with self.assertRaises(ValueError): release.policy(self.config)


if __name__ == '__main__':
    unittest.main()
