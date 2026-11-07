"""Prepare private output parents before any source SDK can create them."""
from source_fixture import SDK_ROOT
import contextlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

import dependencies
import dependency_security
import private_directory
import workflow_bootstrap


class WindowsSourcePreparation(unittest.TestCase):
    def test_every_reusable_source_checkout_has_the_exact_early_private_preparation(self):
        block = workflow_bootstrap.private_steps(indent=6)
        count = 0
        for path in sorted((SDK_ROOT / '.github/workflows').glob('cookbook-*.yml')):
            text = path.read_text()
            sources = [match for match in text.split('repository: nicodes/cookbook')[1:]]
            if not sources:
                continue
            if path.name == 'cookbook-dependency-security.yml':
                self.assertEqual(dependency_security.RUNNERS, {'ubuntu-22.04', 'ubuntu-24.04',
                    'blacksmith-4vcpu-ubuntu-2204', 'blacksmith-4vcpu-ubuntu-2404'})
                self.assertEqual(len(sources), 1)
                self.assertNotIn(block, text)
                for runner in ['windows-2025', 'windows-11-arm', 'macos-15']:
                    with self.subTest(audit_runner=runner), self.assertRaises(ValueError):
                        dependency_security.declaration({'ci': {'dependency_security': {
                            'runner': runner, 'timeout_minutes': 10, 'setup_timeout_minutes': 5}}})
                continue
            with self.subTest(workflow=path.name):
                self.assertEqual(text.count(block), len(sources))
                for fragment in text.split('repository: nicodes/cookbook')[:-1]:
                    self.assertGreater(fragment.rfind(block), fragment.rfind('repository: nicodes/cookbook'))
            count += len(sources)
        self.assertEqual(count, 20)

    def test_provider_uses_one_direct_exact_interpreter_and_disables_implicit_install_before_hooks(self):
        text = (SDK_ROOT / '.github/workflows/windows-bootstrap.yml').read_text()
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
