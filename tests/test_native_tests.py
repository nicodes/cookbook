from source_fixture import SDK_ROOT
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import subprocess
import tomllib

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
spec = importlib.util.spec_from_file_location('cookbook_native_tests', SDK_ROOT / 'cookbook/native_tests.py')
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class NativePrograms(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='native test checkout ')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'tests').mkdir()
        node = tomllib.loads((SDK_ROOT / 'mise.toml').read_text())['tools']['node']
        (self.root / 'mise.toml').write_text('[tools]\nnode="' + node + '"\n')

    def invoke(self, entry):
        environment = dict(os.environ, COOKBOOK_CALLER_ROOT=str(self.root.resolve()),
                           MISE_TRUSTED_CONFIG_PATHS=str(self.root), MISE_CEILING_PATHS=str(self.root.parent))
        native.run_entries(self.root, {'tests': {'unit': [entry]}}, environment, 'unit')

    def test_python_unittest_empty_discovery_cannot_pass(self):
        (self.root / 'tests/test_empty.py').write_text('value = 1\n')
        with self.assertRaisesRegex(ValueError, 'any test cases'):
            self.invoke({'runner': 'python-unittest', 'path': 'tests'})

    def test_python_unittest_preserves_failure_and_native_working_directory(self):
        program = self.root / 'tests/test_behavior.py'
        program.write_text('import unittest\nfrom pathlib import Path\nclass Behavior(unittest.TestCase):\n def test_location(self):\n  self.assertTrue(Path("owned.data").is_file())\n')
        entry = {'runner': 'python-unittest', 'path': 'tests', 'cwd': 'tests'}
        with self.assertRaises(subprocess.CalledProcessError):
            self.invoke(entry)
        (self.root / 'tests/owned.data').write_text('owned fixture')
        self.invoke(entry)

    def test_node_discovery_expands_only_declared_regular_test_sources(self):
        for name in ['first.test.mjs', 'second.test.mjs', 'fixture.txt']:
            (self.root / 'tests' / name).write_text('fixture')
        real_run = native.subprocess.run
        def run(command, **kwargs):
            if command[:2] == ['node', '--test']:
                return subprocess.CompletedProcess(command, 0)
            return real_run(command, **kwargs)
        with patch.object(native.subprocess, 'run', side_effect=run) as invoke:
            self.invoke({'runner': 'node-test', 'path': 'tests', 'pattern': '*.test.mjs'})
        self.assertEqual(invoke.call_args.args[0], ['node', '--test',
            str(self.root / 'tests/first.test.mjs'), str(self.root / 'tests/second.test.mjs')])
        self.assertEqual(invoke.call_args.kwargs['cwd'], self.root)
        self.assertEqual(invoke.call_args.kwargs['env']['COOKBOOK_CALLER_ROOT'], str(self.root.resolve()))


if __name__ == '__main__':
    unittest.main()
