from source_fixture import SDK_ROOT, TOOLS_ROOT, parser_root
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = SDK_ROOT
sys.path.insert(0, str(TOOLS / 'cookbook'))
import source_policy


class SecretSourcePolicy(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='secret policy ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'mise.toml').write_text('[tools]\npython="' + '.'.join(map(str, sys.version_info[:3])) + '"\n')
        (self.root / 'deploy').mkdir()
        (self.root / 'deploy/application.toml').write_text('services: {}\n')
        (self.root / 'delivery.yml').write_text('jobs: {}\n')
        self.config = {'lint': {'secret_policy': {'application': 'deploy/application.toml',
            'workflows': ['delivery.yml'], 'host_only': {'postgres-owner': ['postgres']}}}}
        self.commands = []

    def prepare(self, command, **kwargs):
        self.commands.append((command, kwargs))
        if 'venv' in command:
            _, python, _ = source_policy.inputs(self.root)
            python.parent.mkdir(parents=True)
            python.write_bytes(b'prepared interpreter')

    def test_explicit_sdk_setup_is_isolated_hash_locked_and_stale_checks_never_install(self):
        with self.assertRaises(ValueError):
            source_policy.interpreter(self.root)
        source_policy.setup(self.root, self.config, {}, self.prepare)
        self.assertIn('--copies', self.commands[0][0])
        self.assertIn('-I', self.commands[0][0])
        for argument in ['--require-hashes', '--only-binary', '--no-deps', '--isolated', '-I']:
            self.assertIn(argument, self.commands[1][0])
        checks = []
        source_policy.check(self.root, self.config, {}, lambda *args: checks.append(args))
        self.assertEqual(checks[0][0][1:3], ['-I', str(source_policy.CHECKER)])
        self.assertEqual(checks[0][0][-2:], ['--host-only', 'postgres-owner: postgres'])
        changed = self.root / 'changed.txt'
        changed.write_text('changed reviewed SDK')
        with patch.object(source_policy.dependencies.helper('source_policy'), 'REQUIREMENTS', changed):
            with self.assertRaisesRegex(ValueError, 'stale'):
                source_policy.check(self.root, self.config, {}, lambda *args: self.fail('stale SDK executed'))
        self.assertEqual(len(self.commands), 2)

    def test_failed_install_invalidates_prior_sdk_and_escaping_metadata_refuses_side_effects(self):
        source_policy.setup(self.root, self.config, {}, self.prepare)
        def fail(command, **kwargs):
            if 'pip' in command:
                raise subprocess.CalledProcessError(1, command)
        with self.assertRaises(subprocess.CalledProcessError):
            source_policy.setup(self.root, self.config, {}, fail)
        with self.assertRaises(ValueError):
            source_policy.interpreter(self.root)
        for change in [{'application': '../outside'}, {'workflows': []}, {'host_only': {'../store': ['api']}},
                       {'host_only': {'migration': ['api;other']}}, {'commands': ['true']}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                source_policy.declaration(self.root, {'lint': {'secret_policy': self.config['lint']['secret_policy'] | change}})

    def test_existing_full_acceptance_refusal_and_names_only_drift_controls(self):
        parser = source_policy.interpreter(parser_root())
        poison = self.root / 'yaml.py'
        poison.write_text('raise RuntimeError("caller YAML module must not load")\n')
        subprocess.run(['bash', str(TOOLS_ROOT / 'tests/source-secret-policy.test.sh')], cwd=self.root,
                       env=dict(os.environ, SOURCE_POLICY_PYTHON=parser, PYTHONPATH=str(self.root)),
                       check=True, timeout=180)


if __name__ == '__main__':
    unittest.main()
