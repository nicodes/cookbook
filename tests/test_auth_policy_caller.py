"""Keep native auth policy arguments inside the caller's relative source ABI."""
from source_fixture import SDK_ROOT, native_entry, write_source
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import auth_boundary
import react_native


class AuthPolicyCaller(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='native auth caller ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.policy = 'tests/fixtures/auth policy.toml'
        (self.root / 'tests/fixtures').mkdir(parents=True)
        self.config = {'version': 1, 'recipe': 'react-native',
                       'lint': {'auth_policy': self.policy}}
        write_source(self.root / 'cookbook.toml', self.config)
        write_source(self.root / self.policy, {'provider': 'none', 'api': False})
        self.policy_bytes = (self.root / self.policy).read_bytes()
        (self.root / 'mise.toml').write_text('[tools]\nbun="1.4.1"\n')
        self.environment = {**os.environ, 'COOKBOOK_CALLER_ROOT': str(self.root),
                            'MISE_TRUSTED_CONFIG_PATHS': str(self.root),
                            'MISE_CEILING_PATHS': str(self.root.parent),
                            'MISE_AUTO_INSTALL': 'false', 'MISE_EXEC_AUTO_INSTALL': 'false',
                            'MISE_TASK_RUN_AUTO_INSTALL': 'false', 'MISE_OFFLINE': 'true'}
        self.calls = []

    def lint(self):
        def run(argv, root, environment):
            self.calls.append((argv, root, environment))
            if '--policy' in argv:
                native = native_entry(argv)
                self.assertEqual(native, [sys.executable, str(SDK_ROOT / 'cookbook/auth_boundary.py'),
                                          '--policy', self.policy])
                self.assertEqual(root, self.root)
                self.assertIs(environment, self.environment)
                auth_boundary.check(root, native[-1], environment)
        with patch.object(react_native, 'run', run), \
             patch.object(react_native.source_policy, 'check'), \
             patch.object(react_native.node_component, 'lint'), \
             patch.object(react_native.images, 'validate_compose'):
            react_native.lint(self.root, self.config, self.environment)

    def test_valid_relative_policy_reaches_actual_composed_checker_in_a_space_containing_root(self):
        self.lint()
        selected = [call for call in self.calls if '--policy' in call[0]]
        self.assertEqual(len(selected), 1)
        self.assertFalse(Path(selected[0][0][-1]).is_absolute())
        self.assertEqual(selected[0][0][-1], self.policy)
        self.assertEqual((self.root / self.policy).read_bytes(), self.policy_bytes)

    def test_absolute_escaping_missing_and_symlinked_policies_still_refuse_before_auth_execution(self):
        paths = [str(self.root / self.policy), '../outside.toml', 'missing.toml']
        alias = self.root / 'alias.toml'
        alias.symlink_to(self.root / self.policy)
        paths.append('alias.toml')
        for name in paths:
            with self.subTest(policy=name):
                self.config['lint']['auth_policy'] = name
                self.calls.clear()
                with self.assertRaises(ValueError):
                    self.lint()
                self.assertFalse(any('--policy' in call[0] for call in self.calls))

    def test_actual_failed_auth_findings_retain_the_native_failure_status(self):
        write_source(self.root / self.policy, {'provider': 'unexpected', 'api': False})
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            self.lint()
        self.assertEqual(failure.exception.returncode, 1)
        self.assertEqual(failure.exception.cmd[:2], ['bun', '--eval'])
        self.assertEqual(failure.exception.cmd[2], auth_boundary.SCRIPT)
        self.assertEqual(sum('--policy' in call[0] for call in self.calls), 1)


if __name__ == '__main__':
    unittest.main()
