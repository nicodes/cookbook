from source_fixture import SDK_ROOT, run_task, write_entrypoint
import importlib.util
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from workflow_fixtures import read_yaml
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
        self.assertEqual(set(files), {'.github/workflows/build-and-test.yml', '.github/workflows/release.yml'})

    def test_selected_CI_runner_also_runs_dependency_checks(self):
        declaration = (self.root / 'cookbook.toml').read_text()
        policy = self.root / '.github/dependency-policy.toml'
        policy.parent.mkdir()
        policy.write_text('version=2\nci_workflow="build-and-test.yml"\nrequired_checks=["Build and test / Test"]\ndispatch_workflow="build-and-test.yml"\nmode="review"\n')
        for selected in (None, 'blacksmith-4vcpu-ubuntu-2404'):
            with self.subTest(runner=selected):
                (self.root / 'cookbook.toml').write_text(declaration + (
                    '\n[ci]\nrunner="' + selected + '"\n' if selected else ''))
                runner = selected or 'ubuntu-24.04'
                files = generator.workflow_files(self.root, 'a' * 40)
                build = read_yaml(files['.github/workflows/build-and-test.yml'])['jobs']
                self.assertEqual(build['validate']['with']['runner'], runner)
                self.assertEqual(set(build), {'validate'})
                self.assertNotIn('.github/workflows/dependency-policy.yml', files)
                policy.write_text(policy.read_text().replace('"review"', '"auto"'))
                files = generator.workflow_files(self.root, 'a' * 40)
                dependency = read_yaml(files['.github/workflows/dependency-policy.yml'])
                self.assertEqual(dependency['jobs']['request']['with']['runner'], runner)
                self.assertEqual(dependency['on']['workflow_run']['branches'],
                                 ['dependabot/**', 'dependabot-*', 'dependabot_*'])
                self.assertIn("github.event.workflow_run.event == 'pull_request'",
                              dependency['jobs']['request']['if'])
                policy.write_text(policy.read_text().replace('"auto"', '"review"'))

    def test_host_dependency_policy_rejects_the_retired_check(self):
        policy = self.root / '.github/dependency-policy.toml'
        policy.parent.mkdir()
        policy.write_text('version=2\nci_workflow="build-and-test.yml"\nrequired_checks=["Build and test"]\n')
        with self.assertRaisesRegex(ValueError, 'host recipe dependency policy must require Build and test / Test'):
            generator.workflow_files(self.root, 'a' * 40)

    def test_native_dependency_policy_removes_duplicate_merge_workflow_and_limits_write_scope(self):
        policy = self.root / '.github/dependency-policy.toml'
        policy.parent.mkdir()
        declaration = ('version=2\nci_workflow="build-and-test.yml"\n'
                       'required_checks=["Build and test / Test"]\n'
                       'dispatch_workflow="build-and-test.yml"\n')
        policy.write_text(declaration + 'mode="review"\n')
        files = generator.workflow_files(self.root, 'a' * 40)
        self.assertNotIn('.github/workflows/merge-pr.yml', files)
        self.assertNotIn('.github/workflows/dependency-policy.yml', files)
        policy.write_text(declaration + 'mode="review"\nrequest_workflow="merge-pr.yml"\n')
        with self.assertRaisesRegex(ValueError, 'native dependency policy'):
            generator.workflow_files(self.root, 'a' * 40)
        policy.write_text(declaration.replace('"Build and test / Test"', '') + 'mode="review"\n')
        with self.assertRaises(ValueError):
            generator.workflow_files(self.root, 'a' * 40)
        policy.write_text(declaration + 'mode="auto"\n')
        files = generator.workflow_files(self.root, 'a' * 40)
        self.assertNotIn('.github/workflows/merge-pr.yml', files)
        body = read_yaml(files['.github/workflows/dependency-policy.yml'])
        self.assertEqual(body['permissions'], {})
        self.assertEqual(body['on']['workflow_run']['workflows'], ['Build and test'])
        job = body['jobs']['request']
        self.assertEqual(job['permissions']['contents'], 'write')
        self.assertEqual(job['permissions']['actions'], 'write')
        self.assertTrue(job['uses'].startswith('nicodes/cookbook/.github/workflows/dependency-policy.yml@'))
        self.assertNotIn('steps', job)
        shared = read_yaml((SDK_ROOT / '.github/workflows/dependency-policy.yml').read_text())
        self.assertNotIn('permissions', shared)
        self.assertNotIn('permissions', shared['jobs']['request'])
        self.assertIn("github.ref == 'refs/heads/main'", shared['jobs']['request']['if'])
        policy.write_text(declaration + 'mode="auto"\nrequest_workflow="merge-pr.yml"\n')
        with self.assertRaisesRegex(ValueError, 'native dependency policy'):
            generator.workflow_files(self.root, 'a' * 40)

    def test_UTF8_workflows_are_checked_under_a_non_UTF8_runner_locale(self):
        revision = 'a' * 40
        files = {**generator.entrypoint_files(self.root, revision),
                 **generator.workflow_files(self.root, revision)}
        self.assertTrue(any(not content.isascii() for content in files.values()))
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode('utf-8'))
        environment = {**os.environ, 'LC_ALL': 'C', 'PYTHONUTF8': '0', 'PYTHONCOERCECLOCALE': '0'}
        code = ('import locale,sys; assert locale.getencoding().lower().replace("-", "") != "utf8"; '
                'sys.path.insert(0,sys.argv.pop(1)); import generate; generate.main()')
        command = [sys.executable, '-X', 'utf8=0', '-B', '-c', code, str(COOKBOOK),
                   '--root', str(self.root), '--revision', revision, '--workflows', '--check']
        result = subprocess.run(command, env=environment, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.root / '.github/workflows/build-and-test.yml'
        target.write_bytes(target.read_bytes().replace(b'name: Build and test', b'name: Altered', 1))
        result = subprocess.run(command, env=environment, capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'workflow differs from the pinned recipe generator', result.stderr)

    def test_late_authorizer_can_render_its_already_admitted_data_without_reloading_source(self):
        revision='a'*40
        expected=generator.workflow_files(self.root,revision)
        admitted=generator.load(self.root)
        admitted['ci']={**admitted.get('ci',{}),**generator.ci_settings(admitted)}
        with patch.object(generator,'load',side_effect=AssertionError('admitted source must not be reloaded')):
            self.assertEqual(generator.workflow_files(self.root,revision,admitted),expected)

    def test_regeneration_removes_obsolete_generated_callers_and_preserves_custom_source(self):
        revision = 'a' * 40
        files = generator.workflow_files(self.root, revision)
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        obsolete = self.root / '.github/workflows/retired-caller.yml'
        obsolete.write_text(next(iter(files.values())))
        custom = self.root / '.github/workflows/custom.yml'
        custom.write_text('name: Custom\n')
        with self.assertRaisesRegex(ValueError, 'retired recipe files'):
            generator.check_workflows(self.root, revision)
        with patch('sys.argv', ['generate.py', '--root', str(self.root), '--revision', revision, '--workflows']):
            generator.main()
        self.assertFalse(obsolete.exists())
        self.assertEqual(custom.read_text(), 'name: Custom\n')
        generator.check_workflows(self.root, revision)

    def test_review_policy_retires_its_generated_caller_but_keeps_hand_written_source(self):
        revision = 'a' * 40
        policy = self.root / '.github/dependency-policy.toml'
        policy.parent.mkdir()
        declaration = ('version=2\nci_workflow="build-and-test.yml"\n'
                       'required_checks=["Build and test / Test"]\n'
                       'dispatch_workflow="build-and-test.yml"\n')
        policy.write_text(declaration + 'mode="auto"\n')
        stale = self.root / '.github/workflows/dependency-policy.yml'
        for name, content in generator.workflow_files(self.root, revision).items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        generator.check_workflows(self.root, revision)
        policy.write_text(declaration + 'mode="review"\n')
        with self.assertRaisesRegex(ValueError, 'retired recipe files must be removed: .*dependency-policy.yml'):
            generator.check_workflows(self.root, revision)
        with patch('sys.argv', ['generate.py', '--root', str(self.root), '--revision', revision, '--workflows']):
            generator.main()
        self.assertFalse(stale.exists())
        generator.check_workflows(self.root, revision)
        stale.write_text('name: Dependency policy\n')
        generator.check_workflows(self.root, revision)
        with patch('sys.argv', ['generate.py', '--root', str(self.root), '--revision', revision, '--workflows']):
            generator.main()
        self.assertEqual(stale.read_text(), 'name: Dependency policy\n')

    def test_separately_authored_workflows_follow_the_same_filename_convention(self):
        revision = 'a' * 40
        for name, content in generator.workflow_files(self.root, revision).items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        (self.root / '.github/workflows/arbitrary-path.yml').write_text('name: Custom operation\n')
        with self.assertRaisesRegex(ValueError, 'expected custom-operation.yml'):
            generator.check_workflows(self.root, revision)


if __name__ == '__main__':
    unittest.main()
