import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_cookbook_addon as addon_fixture
from source_fixture import write_entrypoint

TOOLS = addon_fixture.TOOLS
generator = addon_fixture.generator

sys.path.insert(0, str(TOOLS / 'cookbook'))
spec = importlib.util.spec_from_file_location('recipe_ci', TOOLS / 'cookbook/ci.py')
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)
spec = importlib.util.spec_from_file_location('recipe_publication', TOOLS / 'cookbook/publication.py')
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)


class ValidationEvidence(unittest.TestCase):
    make = addon_fixture.AddonRecipe.make
    succeeded = addon_fixture.AddonRecipe.succeeded

    def setUp(self):
        addon_fixture.AddonRecipe.setUp(self)
        self.tools = ci.git(TOOLS, 'rev-parse', 'HEAD')
        (self.root / 'mise.toml').write_text('[tools]\npython="3.13.11"\nactionlint="1.7.12"\n')
        write_entrypoint(self.root, self.tools)
        for name, text in generator.workflow_files(self.root, self.tools).items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        subprocess.run(['git', '-C', str(self.root), 'add', 'mise.toml', '.github'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=CI Controls',
                        '-c', 'user.email=ci@example.invalid', 'commit', '-qm', 'pin fixture cookbook'], check=True)
        self.source = ci.git(self.root, 'rev-parse', 'HEAD')
        self.environment = {'GITHUB_REPOSITORY': 'owner/project', 'GITHUB_RUN_ID': '123',
                            'GITHUB_RUN_ATTEMPT': '2', 'GITHUB_JOB': 'validate',
                            'GITHUB_SHA': self.source, 'GITHUB_REF': 'refs/heads/main',
                            'GITHUB_EVENT_NAME': 'workflow_dispatch',
                            'GITHUB_WORKFLOW_REF': 'owner/project/.github/workflows/release.yml@refs/heads/main'}
        environment = patch.dict(os.environ, self.environment)
        environment.start()
        self.addCleanup(environment.stop)
        self.previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.previous)
        self.config = ci.load(self.root)
        self.config['publication'] = {'tag_prefix': 'v', 'initial': 'first'}

    def accept(self):
        self.succeeded('build')
        self.succeeded('test')
        return ci.proof(self.root, self.source, self.tools, self.config)

    def test_full_proof_requires_current_complete_native_evidence(self):
        accepted = self.accept()
        self.assertEqual(accepted['source_commit'], self.source)
        self.assertEqual(accepted['test_groups'], ['integration', 'lint', 'security', 'unit'])
        file = self.root / '.artifacts/verification/test.json'
        record = json.loads(file.read_text())
        del record['test_receipts']['integration']
        file.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, 'every implemented group'):
            ci.proof(self.root, self.source, self.tools, self.config)

    def test_prior_workflow_evidence_cannot_authorize_current_acceptance(self):
        self.accept()
        with patch.dict(os.environ, {'GITHUB_RUN_ATTEMPT': '3'}), self.assertRaisesRegex(ValueError, 'another source or execution'):
            ci.proof(self.root, self.source, self.tools, self.config)

    def test_publisher_rechecks_exact_accepted_bytes_and_workflow_identity(self):
        accepted = self.accept()
        publication.validate_proof(self.root, self.config, accepted, self.source)
        with patch.dict(os.environ, {'GITHUB_RUN_ID': '999'}), self.assertRaisesRegex(ValueError, 'another workflow'):
            publication.validate_proof(self.root, self.config, accepted, self.source)
        archive = self.root / self.config['package']['archive']
        self.assertTrue(archive.is_file(), 'the control must mutate the accepted archive')
        archive.write_bytes(b'replaced after testing')
        with self.assertRaisesRegex(ValueError, 'accepted build scope'):
            publication.validate_proof(self.root, self.config, accepted, self.source)

    def test_plugin_version_and_release_inputs_are_checked_before_publication(self):
        self.assertEqual(publication.identity(self.root, self.config, 'patch')['tag'], 'v0.0.1')
        for overrides in [{'GITHUB_REF': 'refs/heads/feature'}, {'GITHUB_EVENT_NAME': 'push'}]:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                publication.identity(self.root, self.config, 'patch', self.environment | overrides)
        with self.assertRaises(ValueError):
            publication.identity(self.root, self.config, 'unknown')
        (self.root / 'addon/plugin.cfg').write_text('[plugin]\nversion="0.0.9"\n')
        with self.assertRaisesRegex(ValueError, 'unchanged tracked source'):
            publication.identity(self.root, self.config, 'patch')
        subprocess.run(['git', 'add', 'addon/plugin.cfg'], cwd=self.root, check=True)
        subprocess.run(['git', '-c', 'user.name=CI Controls', '-c', 'user.email=ci@example.invalid',
                        'commit', '-qm', 'native version mismatch'], cwd=self.root, check=True)
        with patch.dict(os.environ, {'GITHUB_SHA': ci.git(self.root, 'rev-parse', 'HEAD')}):
            with self.assertRaisesRegex(ValueError, 'plugin version'):
                publication.identity(self.root, self.config, 'patch')


if __name__ == '__main__':
    unittest.main()
