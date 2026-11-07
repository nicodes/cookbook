"""Actual Mise controls for offline help and fail-closed SDK execution."""
from source_fixture import SDK_ROOT
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import mise_entrypoint
import sdk_environment


class MiseRuntime(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='actual Mise source with spaces ')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'caller with spaces'
        self.root.mkdir()
        self.configuration = b'version=1\nrecipe="godot-addon"\n'
        (self.root / 'cookbook.toml').write_bytes(self.configuration)
        self.environment = sdk_environment.preserve_mise()
        for key in ['COOKBOOK_ROOT', 'COOKBOOK_REF', 'TOOLS_ROOT', 'TOOLS_REF',
                    'MISE_CONFIG_FILE', 'MISE_ENV', 'MISE_OVERRIDE_CONFIG_FILENAMES']:
            self.environment.pop(key, None)
        for name in ['global.toml', 'system.toml']:
            (self.base / name).write_text('')
        self.environment.update(MISE_CONFIG_DIR=str(self.base / 'config'),
            MISE_GLOBAL_CONFIG_FILE=str(self.base / 'global.toml'),
            MISE_SYSTEM_CONFIG_FILE=str(self.base / 'system.toml'),
            MISE_STATE_DIR=str(self.base / 'state'), MISE_CACHE_DIR=str(self.base / 'cache'),
            MISE_CEILING_PATHS=str(self.base), MISE_TRUSTED_CONFIG_PATHS=str(self.root),
            MISE_AUTO_INSTALL='false', MISE_EXEC_AUTO_INSTALL='false',
            MISE_TASK_RUN_AUTO_INSTALL='false', MISE_OFFLINE='true')

    def source(self, python='3.13.11'):
        text = mise_entrypoint.render('[tools]\npython="' + python + '"\n', 'a' * 40,
            'godot-addon', self.configuration, ['lint', 'unit', 'security'], [])
        (self.root / 'mise.toml').write_text(text)
        return text

    def command(self, operation):
        return subprocess.run(['mise', 'run', operation], cwd=self.root,
                              env=self.environment, capture_output=True, text=True, timeout=60)

    def test_help_runs_offline_before_any_sdk_source_or_native_setup(self):
        self.source()
        result = self.command('help')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('mise run build', result.stdout)
        self.assertIn('mise run test:unit', result.stdout)
        self.assertNotIn('mise run test:e2e', result.stdout)
        self.assertFalse((self.root / '.artifacts').exists())

    def test_test_refuses_absent_exact_sdk_without_fetch_install_or_build(self):
        self.source()
        result = self.command('test')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('exact source is absent', result.stderr)
        self.assertFalse((self.root / '.artifacts').exists())
        self.assertFalse((self.root / 'dist').exists())

    def test_missing_exact_python_never_falls_back_to_host_path(self):
        self.source('9.88.77')
        result = self.command('test:unit')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse((self.root / '.artifacts').exists())
        self.assertFalse((self.root / 'dist').exists())

    def test_future_tools_identity_is_not_a_cookbook_execution_authority(self):
        original = self.source()
        (self.root / 'mise.toml').write_text(original.replace('cookbook_contract = 1', 'tools_contract = 1', 1)
            .replace('cookbook_ref = ', 'tools_ref = ', 1))
        result = self.command('test:unit')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('differs from its caller-declared source', result.stderr)
        self.assertFalse((self.root / '.artifacts').exists())


if __name__ == '__main__':
    unittest.main()
