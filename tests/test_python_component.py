from source_fixture import SDK_ROOT
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import python_component as component


class PythonDependencies(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='python setup ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'tests').mkdir()
        (self.root / 'tests/requirements.txt').write_text('fixture==1 --hash=sha256:' + 'a' * 64 + '\n')
        self.value = {'requirements': 'tests/requirements.txt', 'venv': '.artifacts/python/contracts'}
        self.config = {'components': {'python': [self.value]}}
        self.commands = []

    def prepare(self, command, **kwargs):
        self.commands.append(command)
        if 'venv' in command:
            binary = Path(command[-1]) / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_bytes(b'prepared interpreter')

    def test_setup_is_explicit_hash_locked_and_tests_refuse_stale_dependencies(self):
        component.setup(self.root, self.config, os.environ.copy(), self.prepare)
        self.assertIn('--copies', self.commands[0])
        self.assertIn('--require-hashes', self.commands[1])
        self.assertIn('--only-binary', self.commands[1])
        binary = component.interpreter(self.root, self.config, self.value['venv'])
        self.assertTrue(Path(binary).is_file())
        (self.root / 'tests/requirements.txt').write_text('changed source')
        with self.assertRaisesRegex(ValueError, 'stale'):
            component.interpreter(self.root, self.config, self.value['venv'])
        self.assertEqual(len(self.commands), 2)

    def test_failed_install_invalidates_previously_prepared_environment(self):
        component.setup(self.root, self.config, {}, self.prepare)
        def fail(command, **kwargs):
            self.prepare(command, **kwargs)
            if 'pip' in command:
                raise subprocess.CalledProcessError(1, command)
        with self.assertRaises(subprocess.CalledProcessError):
            component.setup(self.root, self.config, {}, fail)
        with self.assertRaises(ValueError):
            component.interpreter(self.root, self.config, self.value['venv'])

    def test_generated_environment_cannot_replace_source_or_follow_symlinks(self):
        for name in ['tests', '../outside', '.artifacts/../tests']:
            with self.assertRaises(ValueError):
                component.inputs(self.root, {**self.value, 'venv': name})
        outside = self.root / 'outside'
        outside.mkdir()
        (self.root / '.artifacts').mkdir()
        (self.root / '.artifacts/python').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            component.setup(self.root, self.config, {}, self.prepare)


if __name__ == '__main__':
    unittest.main()
