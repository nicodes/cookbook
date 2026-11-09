from source_fixture import SDK_ROOT, run_task, write_entrypoint
import importlib.util
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import dependencies
import sdk_identity

COOKBOOK = SDK_ROOT / 'cookbook'
sys.path.insert(0, str(COOKBOOK))
spec = importlib.util.spec_from_file_location('cookbook_generate', COOKBOOK / 'generate.py')
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


class CookbookGeneration(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cookbook checkout ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'tests').mkdir()
        (self.root / 'tests/check.py').write_text('assert True\n')
        (self.root / 'cookbook.toml').write_text('version=1\nrecipe="godot-addon"\n[[tests.unit]]\nrunner="python"\npath="tests/check.py"\n')
        (self.root / 'mise.toml').write_text('[tools]\npython="3.13.11"\nactionlint="1.7.12"\n')

    def test_help_works_offline_before_sdk_or_cookbook_installation(self):
        write_entrypoint(self.root, 'a' * 40)
        result = run_task(self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('mise run build', result.stdout)
        self.assertIn('mise run test:unit', result.stdout)
        self.assertNotIn('mise run test:e2e', result.stdout)
        self.assertFalse((self.root / '.artifacts').exists())

    def test_missing_setup_does_not_silently_fetch_or_build_from_test(self):
        write_entrypoint(self.root, 'a' * 40)
        result = run_task(self.root, 'test')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / '.artifacts').exists())

    def test_installed_recipe_works_in_a_checkout_with_spaces(self):
        recipe = self.source(self.root / 'installed cookbook source')
        revision = sdk_identity.git(recipe, 'rev-parse', 'HEAD')
        installed = self.root / '.artifacts/cookbook' / revision
        installed.parent.mkdir(parents=True)
        subprocess.run(['git', 'clone', '--quiet', '--no-hardlinks', str(recipe), str(installed)], check=True)
        subprocess.run(['git', '-C', str(installed), 'remote', 'set-url', 'origin', 'https://github.com/nicodes/cookbook.git'], check=True)
        write_entrypoint(self.root, revision)
        result = run_task(self.root, 'build', cookbook_root=installed)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'validated-package')

    def test_explicit_tools_checkout_can_also_contain_spaces(self):
        tools = self.source(self.root / 'shared cookbook')
        write_entrypoint(self.root, sdk_identity.git(tools, 'rev-parse', 'HEAD'))
        result = run_task(self.root, 'build', cookbook_root=tools)
        self.assertEqual(result.returncode, 0, result.stderr)

    def source(self, directory):
        """Use a real clean pinned SDK fixture for bootstrap/path transport."""
        directory.mkdir()
        (directory / 'cookbook').mkdir()
        (directory / 'cookbook/dispatch.py').write_text('import sys\nassert sys.argv[1:]==["build"]\nprint("validated-package")\n')
        for name in ['provider_dependency.py', 'dependencies.py', 'sdk_identity.py', 'sdk_source.py']:
            shutil.copyfile(COOKBOOK / name, directory / 'cookbook' / name)
        (directory / 'mise.toml').write_text('[vars]\ntools_contract=1\ntools_ref="' + dependencies.tools_identity()['revision'] + '"\n')
        for args in [('init', '-q'), ('remote', 'add', 'origin', 'https://github.com/nicodes/cookbook.git'),
                     ('add', '.'), ('-c', 'user.name=Bootstrap control', '-c', 'user.email=control@example.invalid', 'commit', '-qm', 'immutable bootstrap fixture')]:
            subprocess.run(['git', '-C', str(directory), *args], check=True)
        return directory

    def test_floating_or_command_like_source_refs_are_refused(self):
        for revision in ['main', 'latest', 'a' * 39, 'a' * 40 + '; echo secret']:
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                generator.entrypoint_files(self.root, revision)

    def test_recipe_data_changes_update_generated_entrypoint_identity(self):
        write_entrypoint(self.root, 'a' * 40)
        before = generator.entrypoint_files(self.root, 'a' * 40)
        path = self.root / 'cookbook.toml'
        path.write_text(path.read_text() + '\n[clean]\npaths=["dist"]\n')
        self.assertNotEqual(before, generator.entrypoint_files(self.root, 'a' * 40))

    def test_generated_workflow_callers_are_valid_without_product_commands(self):
        executable = shutil.which('actionlint')
        self.assertIsNotNone(executable, 'the caller test requires the pinned actionlint SDK')
        path = self.root / 'cookbook.toml'
        path.write_text(path.read_text() + '\n[package]\narchive="dist/@owner_addon.zip"\n[publication]\ntag_prefix="gd-v"\n')
        files = generator.workflow_files(self.root, 'a' * 40)
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            self.assertNotIn('run:', content)
        result = subprocess.run([executable, *files], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(set(files), {'.github/workflows/ci.yml', '.github/workflows/request-merge.yml',
                                      '.github/workflows/release.yml'})

    def test_late_authorizer_can_render_its_already_admitted_data_without_reloading_source(self):
        revision='a'*40
        expected=generator.workflow_files(self.root,revision)
        admitted=generator.load(self.root)
        admitted['ci']={**admitted.get('ci',{}),**generator.ci_settings(admitted)}
        with patch.object(generator,'load',side_effect=AssertionError('admitted source must not be reloaded')):
            self.assertEqual(generator.workflow_files(self.root,revision,admitted),expected)


if __name__ == '__main__':
    unittest.main()
