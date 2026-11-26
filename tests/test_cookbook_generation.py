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
        policy.write_text(declaration + 'mode="merge-on-green"\n')
        green_files = generator.workflow_files(self.root, 'a' * 40)
        self.assertEqual(green_files, files)
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

    def single_job_workflow(self, revision='a' * 40, runner='ubuntu-24.04', timeout=30, source='${{ github.sha }}',
                            dispatch=False, run_name_source='', cookbook=None, group='', extra_env='', environment=''):
        """Expected one-job Build and test caller; the golden text is kept literal on purpose."""
        cookbook = cookbook or revision
        return f"""# Generated cookbook caller. Change recipe data and regenerate.
name: Build and test
run-name: "Build and test · ${{{{ github.event.pull_request.number && format('PR #{{0}}', github.event.pull_request.number) || {run_name_source}github.ref_name }}}}"
'on':
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
  merge_group:
    types: [checks_requested]
{extra_env}permissions: {{}}
concurrency:
  group: ci-${{{{ github.ref }}}}{group}
  cancel-in-progress: true
jobs:
  build-test:
    name: Build and test
    runs-on: {runner}
    timeout-minutes: {timeout}
{environment}    permissions:
      contents: read
      actions: read
      pull-requests: read
    env:
      COOKBOOK_VALIDATION_TIMEOUT: '30'
      COOKBOOK_CANDIDATE_COMMIT: ${{{{ github.event.pull_request.head.sha || {'inputs.source || ' if dispatch else ''}github.sha }}}}
    steps:
      - name: Prepare
        timeout-minutes: 20
        uses: nicodes/cookbook/prepare@{revision}
        with:
          cookbook-ref: {cookbook}
          source: {source}
@@PREVIEW@@      - name: Build
        timeout-minutes: @@BUILD@@
        uses: nicodes/cookbook/build@{revision}
      - name: Test
        timeout-minutes: @@TEST@@
        uses: nicodes/cookbook/test@{revision}
      - name: Finish
        if: always()
        timeout-minutes: 5
        uses: nicodes/cookbook/finish@{revision}
        with:
          status: ${{{{ job.status }}}}
"""

    def policy(self, checks, dispatch='build-and-test.yml'):
        policy = self.root / '.github/dependency-policy.toml'
        policy.parent.mkdir(exist_ok=True)
        policy.write_text('version=2\nci_workflow="build-and-test.yml"\nrequired_checks=' + checks
                          + '\ndispatch_workflow="' + dispatch + '"\nmode="review"\n')

    def test_single_job_host_route_emits_one_job_golden_caller(self):
        path = self.root / 'cookbook.toml'
        path.write_text(path.read_text() + '\n[ci]\nsingle_job=true\n')
        files = generator.workflow_files(self.root, 'a' * 40)
        expected = self.single_job_workflow().replace('@@PREVIEW@@', '').replace('@@BUILD@@', '30').replace('@@TEST@@', '30')
        self.assertEqual(files, {'.github/workflows/build-and-test.yml': expected})
        self.assertNotIn('push:', expected)
        self.assertNotIn('workflow_dispatch', expected)

    def test_single_job_preview_environment_and_runner_follow_the_legacy_caller(self):
        (self.root / 'cookbook.toml').write_text('version=1\nrecipe="react-native"\n[ci]\nsingle_job=true\nrunner="blacksmith-4vcpu-ubuntu-2404"\ntimeout_minutes=45\npreview_environment="Preview"\n[[tests.unit]]\nrunner="python"\npath="tests/check.py"\n')
        text = generator.workflow_files(self.root, 'a' * 40)['.github/workflows/build-and-test.yml']
        expected = self.single_job_workflow(runner='blacksmith-4vcpu-ubuntu-2404', timeout=45,
            environment="    environment: ${{ github.event_name == 'pull_request' && 'Preview' || '' }}\n")
        expected = expected.replace("COOKBOOK_VALIDATION_TIMEOUT: '30'", "COOKBOOK_VALIDATION_TIMEOUT: '45'")
        expected = expected.replace('@@PREVIEW@@', "          preview-clerk-key: ${{ github.event_name == 'pull_request' && vars.CLERK_PUBLISHABLE_KEY || '' }}\n")
        expected = expected.replace('@@BUILD@@', '45').replace('@@TEST@@', '45')
        self.assertEqual(text, expected)

    def test_single_job_godot_route_budgets_the_build_unit_and_host_phases(self):
        (self.root / 'tests/unit.gd').write_text('extends Node\n')
        (self.root / 'cookbook.toml').write_text('version=1\nrecipe="godot-game"\n[ci]\nsingle_job=true\n[ci.godot]\nbuild_timeout_minutes=25\nunit_timeout_minutes=15\nhost_timeout_minutes=40\n[[tests.unit]]\nrunner="godot"\npath="tests/unit.gd"\nsentinel="UNIT PASS"\n')
        text = generator.workflow_files(self.root, 'a' * 40)['.github/workflows/build-and-test.yml']
        expected = self.single_job_workflow(timeout=80).replace('@@PREVIEW@@', '').replace('@@BUILD@@', '25').replace('@@TEST@@', '55')
        self.assertEqual(text, expected)
        (self.root / 'cookbook.toml').write_text((self.root / 'cookbook.toml').read_text().replace('build_timeout_minutes=25', 'build_timeout_minutes=120'))
        capped = read_yaml(generator.workflow_files(self.root, 'a' * 40)['.github/workflows/build-and-test.yml'])['jobs']['build-test']
        self.assertEqual(capped['timeout-minutes'], 175)
        (self.root / 'cookbook.toml').write_text((self.root / 'cookbook.toml').read_text().replace('host_timeout_minutes=40', 'host_timeout_minutes=90'))
        capped = read_yaml(generator.workflow_files(self.root, 'a' * 40)['.github/workflows/build-and-test.yml'])['jobs']['build-test']
        self.assertEqual(capped['timeout-minutes'], 180)
        self.assertEqual([step['timeout-minutes'] for step in capped['steps']], [20, 120, 105, 5])

    def test_single_job_keeps_the_manual_entry_only_when_dependency_policy_dispatches_it(self):
        path = self.root / 'cookbook.toml'
        path.write_text(path.read_text() + '\n[ci]\nsingle_job=true\n')
        self.policy('["Build and test"]')
        text = generator.workflow_files(self.root, 'a' * 40)['.github/workflows/build-and-test.yml']
        dispatch = ("  workflow_dispatch:\n    inputs:\n      source:\n        description: Exact source commit; defaults to this main revision\n"
            "        type: string\n        default: ''\n      cookbook:\n        description: Accepted cookbook commit; defaults to the pinned recipe\n"
            "        type: string\n        default: ''\n")
        expected = self.single_job_workflow(source='${{ inputs.source || github.sha }}', dispatch=True, run_name_source='inputs.source || ',
            cookbook="${{ inputs.cookbook || '" + 'a' * 40 + "' }}", group="-${{ inputs.source || 'current' }}", extra_env=dispatch)
        self.assertEqual(text, expected.replace('@@PREVIEW@@', '').replace('@@BUILD@@', '30').replace('@@TEST@@', '30'))
        (self.root / '.github/dependency-policy.toml').unlink()
        self.assertNotIn('workflow_dispatch', generator.workflow_files(self.root, 'a' * 40)['.github/workflows/build-and-test.yml'])

    def test_single_job_requires_the_one_build_and_test_check(self):
        path = self.root / 'cookbook.toml'
        path.write_text(path.read_text() + '\n[ci]\nsingle_job=true\n')
        for checks in ['["Build and test / Test"]', '["Build and test", "Build and test / Test"]', '[]']:
            self.policy(checks)
            with self.subTest(checks=checks), self.assertRaisesRegex(ValueError, 'single-job dependency policy must require Build and test and not Build and test / Test'):
                generator.workflow_files(self.root, 'a' * 40)
        self.policy('["Build and test"]')
        self.assertIn('build-test:', generator.workflow_files(self.root, 'a' * 40)['.github/workflows/build-and-test.yml'])

    def test_without_the_flag_the_legacy_callers_are_unchanged(self):
        path = self.root / 'cookbook.toml'
        legacy = generator.workflow_files(self.root, 'a' * 40)['.github/workflows/build-and-test.yml']
        for text in ['\n[ci]\nsingle_job=false\n', '\n[ci]\nrunner="ubuntu-24.04"\n']:
            path.write_text(path.read_text().split('\n[ci]')[0] + text)
            self.assertEqual(generator.workflow_files(self.root, 'a' * 40)['.github/workflows/build-and-test.yml'], legacy)
        self.assertIn('  push:\n    branches: [main]\n', legacy)
        self.assertIn('    name: Build and test\n    uses: nicodes/cookbook/.github/workflows/build-and-test-host.yml@' + 'a' * 40, legacy.replace('    name: Test\n', '    name: Build and test\n'))
        self.assertNotIn('nicodes/cookbook/prepare', legacy)


if __name__ == '__main__':
    unittest.main()
