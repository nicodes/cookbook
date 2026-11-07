from source_fixture import SDK_ROOT
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import publication
import publication_workflows


class PublicationAuthority(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / 'addon').mkdir()
        (self.root / 'addon/plugin.cfg').write_text('[plugin]\nversion="0.0.1"\n')
        (self.root / 'package.files').write_text('plugin.cfg\n')
        (self.root / 'cookbook.toml').write_text('version=1\nrecipe="godot-addon"\n[package]\narchive="dist/@owner_addon.zip"\n[publication]\ntag_prefix="v"\n')
        subprocess.run(['git', 'add', '.'], cwd=self.root, check=True)
        subprocess.run(['git', '-c', 'user.name=Authority Controls', '-c', 'user.email=controls@example.invalid', 'commit', '-qm', 'fixture'], cwd=self.root, check=True)
        self.source = publication.git(self.root, 'rev-parse', 'HEAD')
        self.environment = {'GITHUB_REF': 'refs/heads/main', 'GITHUB_SHA': self.source,
                            'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REPOSITORY': 'owner/addon',
                            'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1',
                            'GITHUB_WORKFLOW_REF': 'owner/addon/.github/workflows/release.yml@refs/heads/main'}
        self.config = {'recipe': 'godot-addon', 'package': {'archive': 'dist/@owner_addon.zip'}, 'publication': {'tag_prefix': 'v'}}

    def package(self):
        archive = self.root / 'dist/@owner_addon.zip'
        helper = publication.helper('addon-package')
        digest = helper.build(self.root / 'addon', self.root / 'package.files', archive)
        archive.with_suffix('.zip.sha256').write_text(digest + '  ' + archive.name + '\n')
        return archive

    def proof(self):
        archive = self.package()
        return {'source_commit': self.source, 'source_tree': publication.git(self.root, 'rev-parse', 'HEAD^{tree}'),
                'repository': 'owner/addon', 'cookbook_commit': publication.git(publication.TOOLS, 'rev-parse', 'HEAD'),
                'status': 'passed', 'tracked_source_clean': True, 'build_verified': True, 'tests_verified': True,
                'test_groups': ['lint', 'unit', 'security'], 'execution_context': self.environment,
                'artifacts': {path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in [archive, archive.with_suffix('.zip.sha256')]}}

    def test_bump_first_and_plugin_identity_policies_remain_distinct(self):
        result = publication.identity(self.root, self.config, 'minor', self.environment)
        self.assertEqual((result['version'], result['tag']), ('0.0.1', 'v0.0.1'))
        self.config['publication']['initial'] = 'bump'
        with self.assertRaisesRegex(ValueError, 'plugin version'):
            publication.identity(self.root, self.config, 'minor', self.environment)
        self.config['publication']['mode'] = 'plugin'
        self.assertEqual(publication.identity(self.root, self.config, environment=self.environment)['tag'], 'v0.0.1')

    def test_release_validation_keeps_the_source_declared_browser_host(self):
        self.config['ci'] = {'runner': 'ubuntu-22.04', 'timeout_minutes': 30}
        plan = publication.identity(self.root, self.config, environment=self.environment)
        self.assertEqual(plan['validation_runner'], 'ubuntu-22.04')
        workflow = (publication.TOOLS / '.github/workflows/cookbook-addon-release.yml').read_text()
        self.assertIn('runner: ${{ needs.plan.outputs.validation_runner }}', workflow)

    def test_same_head_resume_and_wrong_head_tag_fail_closed(self):
        subprocess.run(['git', 'tag', 'v0.0.1'], cwd=self.root, check=True)
        self.config['publication'].update(resume=True, initial='bump')
        self.assertEqual(publication.identity(self.root, self.config, environment=self.environment)['operation'], 'resume')
        self.config['publication']['resume'] = False
        with self.assertRaises(ValueError):
            publication.identity(self.root, self.config, environment=self.environment)
        self.config['publication']['resume'] = True
        (self.root / 'next').write_text('same plugin, changed head')
        subprocess.run(['git', 'add', '.'], cwd=self.root, check=True)
        subprocess.run(['git', '-c', 'user.name=Controls', '-c', 'user.email=controls@example.invalid', 'commit', '-qm', 'new source'], cwd=self.root, check=True)
        environment = self.environment | {'GITHUB_SHA': publication.git(self.root, 'rev-parse', 'HEAD')}
        with self.assertRaisesRegex(ValueError, 'release head'):
            publication.identity(self.root, self.config, environment=environment)

    def test_checksum_retry_requires_complete_pair_and_never_uses_native_build_proof(self):
        subprocess.run(['git', 'tag', 'v0.0.1'], cwd=self.root, check=True)
        self.config['publication']['retry'] = True
        for kwargs in [{'retry_tag': 'v0.0.1'}, {'retry_sha256': 'a' * 64}, {'retry_tag': 'v0.0.1', 'retry_sha256': 'bad'}]:
            with self.assertRaises(ValueError):
                publication.identity(self.root, self.config, environment=self.environment, **kwargs)
        plan = publication.identity(self.root, self.config, environment=self.environment, retry_tag='v0.0.1', retry_sha256='a' * 64)
        self.assertEqual(plan['operation'], 'retry')
        with patch.object(publication, 'require_remote_source'), patch.object(publication, 'registry_recovery', return_value={}) as recover, patch.object(publication, 'validate_proof') as proof, patch.object(publication, 'assets') as assets:
            publication.publish(self.root, self.config, plan, self.environment)
        recover.assert_called_once()
        proof.assert_not_called()
        assets.assert_not_called()

    def test_exact_tag_oidc_recovery_requires_run_commit_and_never_accepts_wrong_ref(self):
        subprocess.run(['git', 'tag', 'v0.0.1'], cwd=self.root, check=True)
        self.config['publication'].update(mode='tag', registry_auth='oidc')
        plan = publication.identity(self.root, self.config, environment=self.environment, tag='v0.0.1', recovery=True)
        self.assertEqual(plan['target'], self.source)
        environment = self.environment | {'GITHUB_REF': 'refs/tags/v0.0.1'}
        self.assertEqual(publication.identity(self.root, self.config, environment=environment, tag='v0.0.1', recovery=True)['operation'], 'registry-recovery')
        for overrides in [{'GITHUB_SHA': 'b' * 40}, {'GITHUB_REF': 'refs/tags/v0.0.2'}, {'GITHUB_EVENT_NAME': 'push'}]:
            with self.assertRaises(ValueError):
                publication.identity(self.root, self.config, environment=environment | overrides, tag='v0.0.1', recovery=True)

    def test_same_run_source_tools_groups_and_every_artifact_are_required(self):
        proof = self.proof()
        publication.validate_proof(self.root, self.config, proof, self.source, self.environment)
        for change in [{'source_commit': 'b' * 40}, {'cookbook_commit': 'b' * 40}, {'test_groups': ['unit']},
                       {'execution_context': self.environment | {'GITHUB_RUN_ATTEMPT': '2'}}, {'artifacts': {}},
                       {'tracked_source_clean': False}, {'build_verified': False}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                publication.validate_proof(self.root, self.config, proof | change, self.source, self.environment)
        (self.root / 'dist/@owner_addon.zip').write_bytes(b'changed downloaded archive')
        with self.assertRaisesRegex(ValueError, 'outputs differ'):
            publication.validate_proof(self.root, self.config, proof, self.source, self.environment)

    def test_publisher_runs_no_source_program_and_preserves_legacy_digest_algorithm(self):
        self.package()
        self.config['publication']['digests'] = 'gd-package-digests.txt'
        expected = publication.assets(self.root, self.config, self.root / 'staged')
        payload = (self.root / 'addon/plugin.cfg').read_bytes()
        tree = hashlib.sha256(b'plugin.cfg\0' + hashlib.sha256(payload).digest()).hexdigest()
        self.assertIn('GD_INSTALLED_TREE_SHA256=' + tree, (self.root / 'staged/gd-package-digests.txt').read_text())
        self.assertIn('gd-package-digests.txt', expected)

    def test_only_explicit_same_run_evidence_can_be_shipped(self):
        proof = self.proof()
        evidence = self.root / 'dist/web-evidence.png'
        evidence.write_bytes(b'accepted screenshot')
        proof['evidence'] = {'dist/web-evidence.png': hashlib.sha256(evidence.read_bytes()).hexdigest()}
        self.config['publication']['evidence_assets'] = ['dist/web-evidence.png']
        publication.validate_proof(self.root, self.config, proof, self.source, self.environment)
        expected = publication.assets(self.root, self.config, self.root / 'staged', proof)
        self.assertEqual(expected['web-evidence.png'], proof['evidence']['dist/web-evidence.png'])
        evidence.write_bytes(b'screenshot replaced after acceptance')
        with self.assertRaisesRegex(ValueError, 'evidence differs'):
            publication.validate_proof(self.root, self.config, proof, self.source, self.environment)
        with self.assertRaisesRegex(ValueError, 'evidence differs'):
            publication.assets(self.root, self.config, self.root / 'replaced', proof)

    def test_evidence_cannot_shadow_an_accepted_archive_basename(self):
        proof = self.proof()
        path = self.root / 'dist/nested/@owner_addon.zip'
        path.parent.mkdir()
        path.write_bytes(b'different evidence with the archive name')
        proof['evidence'] = {'dist/nested/@owner_addon.zip': hashlib.sha256(path.read_bytes()).hexdigest()}
        self.config['publication']['evidence_assets'] = ['dist/nested/@owner_addon.zip']
        with self.assertRaisesRegex(ValueError, 'distinct canonical basenames'):
            publication.assets(self.root, self.config, self.root / 'staged', proof)
        self.assertFalse((self.root / 'staged').exists())

    def test_caller_generation_separates_oidc_recovery_from_write_publication(self):
        path = self.root / 'cookbook.toml'
        path.write_text(path.read_text() + 'mode="tag"\nregistry_auth="oidc"\n')
        files = publication_workflows.workflow_files(self.root, 'a' * 40)
        release = files['.github/workflows/release.yml']
        recovery = files['.github/workflows/gdam-publish.yml']
        self.assertIn('contents: write', release)
        self.assertIn('actions: read', release)
        shared = (Path(publication.__file__).resolve().parents[1] / '.github/workflows/cookbook-addon-release.yml').read_text()
        validation = shared.split('  validate:\n', 1)[1].split('    uses:', 1)[0]
        self.assertIn('      actions: read', validation.split('    uses:', 1)[0])
        self.assertIn('contents: read', recovery)
        self.assertIn('id-token: write', recovery)
        self.assertNotIn('gdam-key', release + recovery)
        self.assertIn('cookbook-addon-recovery.yml@' + 'a' * 40, recovery)

    def test_remote_main_and_tag_changes_refuse_publication_admission(self):
        plan = publication.identity(self.root, self.config, environment=self.environment)
        fake = Mock()
        fake.run.return_value = json.dumps({'object': {'sha': 'c' * 40}})
        with patch.object(publication, 'helper', return_value=fake), self.assertRaisesRegex(ValueError, 'main changed'):
            publication.require_remote_source(self.root, plan, self.environment)
        fake.run.return_value = json.dumps({'object': {'sha': self.source}})
        fake.remote_tag_commit.return_value = 'c' * 40
        with patch.object(publication, 'helper', return_value=fake), self.assertRaisesRegex(ValueError, 'immutable tag'):
            publication.require_remote_source(self.root, plan | {'operation': 'retry'}, self.environment)

    def test_same_head_existing_release_resumes_without_upload_or_relabel(self):
        proof = self.proof()
        destination = self.root / '.artifacts'
        destination.mkdir()
        (destination / 'cookbook-validation.json').write_text(json.dumps(proof))
        self.config['publication']['resume'] = True
        subprocess.run(['git', 'tag', 'v0.0.1'], cwd=self.root, check=True)
        plan = publication.identity(self.root, self.config, environment=self.environment)
        real_helper = publication.helper
        release = Mock()
        release.optional_release.return_value = {'draft': False}
        def selected(name):
            return release if name == 'github_release' else real_helper(name)
        with patch.object(publication, 'helper', side_effect=selected), patch.object(publication, 'require_remote_source'):
            publication.publish(self.root, self.config, plan, self.environment)
        release.publish.assert_not_called()
        release.download_existing.assert_called_once()
        expected = release.download_existing.call_args.args[-1]
        self.assertEqual(expected, {'@owner_addon.zip': proof['artifacts']['dist/@owner_addon.zip']})

    def test_declared_budget_environment_and_authentication_cannot_be_command_maps(self):
        for change in [{'registry_auth': 'custom-shell'}, {'publish_timeout': '20; true'},
                       {'environment': 'release\nOTHER=value'}, {'mode': 'run-command'}, {'retry': 'true'}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                publication.policy(self.root, self.config | {'publication': self.config['publication'] | change})


if __name__ == '__main__':
    unittest.main()
