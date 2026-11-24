"""Prepare private output parents before any source SDK can create them."""
from source_fixture import SDK_ROOT
import contextlib
import json
import shlex
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

import dependencies
import private_directory
import workflow_bootstrap
from workflow_fixtures import read_yaml


class WindowsSourcePreparation(unittest.TestCase):
    def test_provider_parallel_jobs_keep_all_supported_setup_matrix_and_native_checks(self):
        value = read_yaml((SDK_ROOT / '.github/workflows/build-and-test.yml').read_text())
        jobs = value['jobs']
        self.assertEqual(set(jobs), {'build-test', 'native-cache-shell', 'test', 'single-job', 'single-job-rust-cli', 'single-job-integration',
            'single-job-second-wave', 'windows-sdk-setup', 'ubuntu-mirrors', 'ubuntu-ci-setup', 'postgresql-client'})
        self.assertEqual(jobs['windows-sdk-setup']['strategy']['matrix']['runner'], ['windows-2025', 'windows-11-arm'])
        self.assertEqual(jobs['windows-sdk-setup']['timeout-minutes'], 10)
        self.assertEqual([(r['runner'], r['architecture'], r['profile'])
            for r in jobs['ubuntu-ci-setup']['strategy']['matrix']['include']],
            [('ubuntu-24.04', 'amd64', 'owner-rehearsal'), ('ubuntu-24.04-arm', 'arm64', 'linker'),
             ('ubuntu-22.04', 'amd64', 'linker')])
        self.assertEqual(jobs['ubuntu-ci-setup']['timeout-minutes'], 8)
        self.assertEqual(jobs['ubuntu-mirrors']['timeout-minutes'], 6)
        self.assertEqual(jobs['postgresql-client']['timeout-minutes'], 15)
        for name in ['windows-sdk-setup.yml', 'ubuntu-ci-setup.yml', 'postgresql-client-check.yml']:
            self.assertFalse((SDK_ROOT / '.github/workflows' / name).exists())

    def test_provider_required_result_cannot_hide_any_missing_failed_or_skipped_platform_job(self):
        jobs = read_yaml((SDK_ROOT / '.github/workflows/build-and-test.yml').read_text())['jobs']
        aggregate = jobs['build-test']
        self.assertEqual(aggregate['name'], 'Build and test')
        self.assertEqual(set(aggregate['needs']), set(jobs) - {'build-test'})
        self.assertEqual(aggregate['if'], 'always()')
        script = aggregate['steps'][0]['run'].replace('python3 -I -B -', shlex.quote(sys.executable) + ' -I -B -', 1)
        valid = {name: {'result': 'success', 'outputs': {}} for name in aggregate['needs']}
        def execute(value):
            return subprocess.run(['bash', '-euc', script], capture_output=True, text=True,
                env=dict(os.environ, CI_NEEDS_JSON=json.dumps(value)), timeout=5).returncode
        self.assertEqual(execute(valid), 0)
        for name in valid:
            for result in ['failure', 'skipped', 'cancelled']:
                with self.subTest(job=name, result=result):
                    self.assertNotEqual(execute({**valid, name: {'result': result, 'outputs': {}}}), 0)
            self.assertNotEqual(execute({key: value for key, value in valid.items() if key != name}), 0)

    def test_complete_source_routes_check_public_documentation_before_building(self):
        for filename, job in [('build-and-test.yml', 'test'), ('build-and-test-godot.yml', 'plan'),
                              ('build-and-test-native.yml', 'plan'), ('build-and-test-host.yml', 'validate')]:
            value = read_yaml((SDK_ROOT / '.github/workflows' / filename).read_text())
            steps = value['jobs'][job]['steps']
            check = next(s for s in steps if s['name'] == 'Check documentation boundary')
            self.assertNotIn('REQUIRE_CLEAN_HISTORY', check.get('env', {}))
            self.assertIn('$TOOLS_ROOT/helpers/documentation-policy.py', check['run'])
            self.assertEqual(check['timeout-minutes'], 3)
            checkout = next(s for s in steps if s.get('uses', '').startswith('actions/checkout@'))
            self.assertEqual(checkout['with']['fetch-depth'], 1)

    def test_every_reusable_source_checkout_has_the_exact_early_private_preparation(self):
        block = workflow_bootstrap.private_steps(indent=6)
        count = 0
        for path in sorted((SDK_ROOT / '.github/workflows').glob('*.yml')):
            text = path.read_text()
            sources = [match for match in text.split('repository: nicodes/cookbook')[1:]]
            if not sources:
                continue
            with self.subTest(workflow=path.name):
                self.assertEqual(text.count(block), len(sources))
                for fragment in text.split('repository: nicodes/cookbook')[:-1]:
                    self.assertGreater(fragment.rfind(block), fragment.rfind('repository: nicodes/cookbook'))
            count += len(sources)
        # The two native waves share one written checkout and preparation block.
        self.assertEqual(count, 16)

    def test_provider_uses_one_direct_exact_interpreter_and_disables_implicit_install_before_hooks(self):
        text = (SDK_ROOT / '.github/workflows/build-and-test.yml').read_text()
        text = text.split('\n  windows-sdk-setup:', 1)[1].split('\n  ubuntu-mirrors:', 1)[0]
        before_mise = text.split('uses: jdx/mise-action@', 1)[0]
        for name in ['MISE_AUTO_INSTALL', 'MISE_EXEC_AUTO_INSTALL', 'MISE_TASK_RUN_AUTO_INSTALL']:
            self.assertIn("      " + name + ": 'false'\n", before_mise)
        self.assertLess(text.index('private artifact parent'), text.index('mise run _tools -- setup'))
        self.assertIn('mise where python@3.13.11', text)
        self.assertIn('"$COOKBOOK_BOOTSTRAP_PYTHON" -I -B -', text)
        self.assertIn("verification.protected_directory(Path('.artifacts/bootstrap-control'))", text)
        for filename in ['test_recipe_cargo_paths.py', 'test_sdk_identity.py',
                         'test_mise_runtime.py', 'test_cookbook_bootstrap.py']:
            self.assertIn('"$COOKBOOK_BOOTSTRAP_PYTHON" -m unittest discover -s tests -p ' + filename + ' -v', text)
        self.assertIn('    timeout-minutes: 10\n', text)
        self.assertIn('runner: [windows-2025, windows-11-arm]', text)

    def execute(self, root):
        environment = dict(os.environ, GITHUB_ENV=str(root / 'environment'))
        return subprocess.run([sys.executable, '-I', '-B', '-c', private_directory.SCRIPT],
                              cwd=root, env=environment, capture_output=True, text=True, timeout=10)

    def test_actual_stdlib_creates_the_missing_private_parent_and_unchanged_policy_admits_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.execute(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            info = (root / '.artifacts').stat()
            if os.name != 'nt':
                self.assertEqual(stat.S_IMODE(info.st_mode), 0o700)
            with contextlib.chdir(root):
                self.assertEqual(dependencies.helper('verification').protected_directory(Path('.artifacts')),
                                 Path('.artifacts'))

    def test_existing_unsafe_parent_is_not_repaired_and_the_actual_policy_still_refuses_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / '.artifacts'; path.mkdir()
            if os.name != 'nt':
                path.chmod(0o777)  # Adversarial owned fixture only; never production preparation.
            before = path.stat()
            result = self.execute(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            after = path.stat()
            self.assertEqual((after.st_ino, after.st_mode), (before.st_ino, before.st_mode))
            if os.name != 'nt':
                with contextlib.chdir(root), self.assertRaisesRegex(ValueError, 'not protected'):
                    dependencies.helper('verification').protected_directory(Path('.artifacts'))

    def test_linked_or_nondirectory_parent_refuses_without_changing_its_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); target = root / 'owned'; target.mkdir()
            sentinel = target / 'sentinel'; sentinel.write_bytes(b'unchanged')
            (root / '.artifacts').symlink_to(target, target_is_directory=True)
            self.assertNotEqual(self.execute(root).returncode, 0)
            self.assertEqual(sentinel.read_bytes(), b'unchanged')
            (root / '.artifacts').unlink(); (root / '.artifacts').write_bytes(b'unchanged file')
            self.assertNotEqual(self.execute(root).returncode, 0)
            self.assertEqual((root / '.artifacts').read_bytes(), b'unchanged file')


if __name__ == '__main__':
    unittest.main()
