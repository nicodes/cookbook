"""Actual installed Node activation keeps component cwd separate from pin root."""
from source_fixture import SDK_ROOT
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import sdk_environment


class ComponentSDKActivation(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='component SDK root ')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'caller with spaces'
        self.app = self.root / 'app'
        self.app.mkdir(parents=True)
        self.version = tomllib.loads((SDK_ROOT / 'mise.toml').read_text())['tools']['node']
        (self.root / 'mise.toml').write_text('[tools]\nnode=' + json.dumps(self.version) + '\n')
        # The native cwd must not become a second SDK pin authority.
        (self.app / 'mise.toml').write_text('[tools]\nnode="9.88.77"\n')
        for name in ['global.toml', 'system.toml']:
            (self.base / name).write_text('')
        self.environment = sdk_environment.preserve_mise()
        for key in ['MISE_CONFIG_FILE', 'MISE_ENV', 'MISE_OVERRIDE_CONFIG_FILENAMES']:
            self.environment.pop(key, None)
        self.environment.update(COOKBOOK_CALLER_ROOT=str(self.root),
            MISE_CONFIG_DIR=str(self.base / 'config'), MISE_CACHE_DIR=str(self.base / 'cache'),
            MISE_STATE_DIR=str(self.base / 'state'), MISE_GLOBAL_CONFIG_FILE=str(self.base / 'global.toml'),
            MISE_SYSTEM_CONFIG_FILE=str(self.base / 'system.toml'),
            MISE_TRUSTED_CONFIG_PATHS=str(self.root), MISE_CEILING_PATHS=str(self.base), MISE_OFFLINE='true')

    def test_actual_node_and_package_managers_share_caller_install_from_component_cwd(self):
        for command in [['node', '-p', 'process.versions.node'], ['npm', '--version'], ['npx', '--version']]:
            values = sdk_environment.command_environment(command, self.app, self.environment)
            actual = subprocess.run(command, cwd=self.app, env=values, capture_output=True,
                                    text=True, check=True, timeout=30)
            self.assertTrue(actual.stdout.strip())
            if command[0] == 'node':
                self.assertEqual(actual.stdout.strip(), self.version)
            location = Path(subprocess.check_output(['mise', 'where', 'node@' + self.version],
                cwd=self.root, env=values, text=True, timeout=30).strip()).resolve()
            for executable in ['node', command[0]]:
                self.assertTrue(Path(shutil.which(executable, path=values['PATH'])).resolve().is_relative_to(location))
            self.assertEqual(values['COOKBOOK_CALLER_ROOT'], str(self.root))
        self.assertFalse((self.root / 'node_modules').exists())
        self.assertFalse((self.root / '.artifacts').exists())

    def test_missing_exact_caller_node_refuses_foreign_available_path_before_native_execution(self):
        installed = sdk_environment.command_environment(['node', '--version'], self.app, self.environment)
        available = str(Path(shutil.which('node', path=installed['PATH'])).resolve().parent)
        (self.root / 'mise.toml').write_text('[tools]\nnode="9.88.77"\n')
        values = {**self.environment, 'PATH': available + os.pathsep + self.environment.get('PATH', '')}
        with self.assertRaises(ValueError):
            sdk_environment.command_environment(['npm', '--version'], self.app, values)
        self.assertFalse((self.root / 'node_modules').exists())
        self.assertFalse((self.root / '.artifacts').exists())

    def test_relative_or_foreign_derived_caller_root_refuses_before_activation(self):
        for caller in ['relative-caller', str(self.base / 'other caller')]:
            with self.subTest(caller=caller), self.assertRaises(ValueError):
                sdk_environment.command_environment(['node', '--version'], self.app,
                    {**self.environment, 'COOKBOOK_CALLER_ROOT': caller})
        self.assertFalse((self.root / '.artifacts').exists())


if __name__ == '__main__':
    unittest.main()
