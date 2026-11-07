from source_fixture import SDK_ROOT, write_entrypoint, run_task, run_unit_adapter
import importlib.util
import hashlib
from source_fixture import write_source
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TOOLS = SDK_ROOT
sys.path.insert(0, str(TOOLS / 'cookbook'))
spec = importlib.util.spec_from_file_location('addon_make_generation', TOOLS / 'cookbook/generate.py')
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


class AddonRecipe(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='addon checkout ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'addon').mkdir()
        (self.root / 'tests').mkdir()
        (self.root / '.github').mkdir()
        readme = '# Fixture addon\nNative test fixture.\n'
        (self.root / 'README.md').write_text(readme)
        write_source(self.root / '.github/readme-source.toml', {
            'version': 1, 'sha256': hashlib.sha256(readme.encode()).hexdigest()})
        (self.root / 'addon/plugin.cfg').write_text('[plugin]\nversion="0.0.1"\n')
        (self.root / 'package.files').write_text('plugin.cfg\n')
        (self.root / 'tests/test_behavior.py').write_text('assert 2 + 2 == 4\n')
        (self.root / '.gitignore').write_text('/.artifacts/\n/dist/\n')
        (self.root / 'cookbook.toml').write_text('''version=1
recipe="godot-addon"
[package]
archive="dist/@owner_addon.zip"
[clean]
paths=["dist"]
[[tests.unit]]
runner="python"
path="tests/test_behavior.py"
[[tests.integration]]
runner="python"
path="tests/test_behavior.py"
''')
        write_entrypoint(self.root)
        for argv in [['init', '-q'], ['add', '.'], ['-c', 'user.name=Recipe Controls',
                     '-c', 'user.email=recipe@example.invalid', 'commit', '-qm', 'fixture']]:
            subprocess.run(['git', '-C', str(self.root), *argv], check=True)

    def make(self, operation):
        return run_task(self.root, operation)

    def succeeded(self, operation):
        result = self.make(operation)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_integration_requires_build_without_silently_creating_one(self):
        self.assertNotEqual(self.make('test:integration').returncode, 0)
        self.assertFalse((self.root / 'dist').exists())
        self.succeeded('build')
        self.succeeded('test:integration')

    def test_test_aggregates_fresh_group_receipts_without_mutating_build(self):
        self.succeeded('build')
        build = (self.root / '.artifacts/verification/build.json').read_bytes()
        archive = (self.root / 'dist/@owner_addon.zip').read_bytes()
        self.succeeded('test')
        receipt = json.loads((self.root / '.artifacts/verification/test.json').read_text())
        self.assertEqual(set(receipt['test_receipts']), {'lint', 'unit', 'integration', 'security'})
        self.assertEqual((self.root / '.artifacts/verification/build.json').read_bytes(), build)
        self.assertEqual((self.root / 'dist/@owner_addon.zip').read_bytes(), archive)

    def test_mutated_artifact_and_stale_source_are_rejected(self):
        self.succeeded('build')
        (self.root / 'dist/@owner_addon.zip').write_bytes(b'mutated')
        self.assertNotEqual(self.make('test:integration').returncode, 0)
        self.assertEqual((self.root / 'dist/@owner_addon.zip').read_bytes(), b'mutated')
        self.succeeded('build')
        (self.root / 'addon/plugin.cfg').write_text('[plugin]\nversion="0.0.2"\n')
        self.assertNotEqual(self.make('test:integration').returncode, 0)

    def test_failed_test_invalidates_previous_aggregate_success(self):
        self.succeeded('build')
        self.succeeded('test')
        (self.root / 'tests/test_behavior.py').write_text('raise SystemExit(4)\n')
        self.assertNotEqual(self.make('test').returncode, 0)
        self.assertFalse((self.root / '.artifacts/verification/test.json').exists())


if __name__ == '__main__':
    unittest.main()
