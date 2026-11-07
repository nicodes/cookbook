from source_fixture import SDK_ROOT
from pathlib import Path
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import addon_dependencies
from source_fixture import write_source


class RegistrySdkControls(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        subprocess.run(['git', 'init', '--quiet', str(self.root)], check=True)
        (self.root / '.gitignore').write_text('.artifacts/\n/gdam.json\n')
        write_source(self.root / 'gdam.toml', {'addons': {}})
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)
        (self.root / 'mise.toml').write_text('[tools]\n"github:owner/gdam" = {version="v0.1.5",asset_pattern="gdam_Linux_x86_64.tar.gz",bin="gdam"}\n')
        binary = b'verified native SDK bytes'
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w:gz') as archive:
            entry = tarfile.TarInfo('gdam')
            entry.size = len(binary)
            archive.addfile(entry, io.BytesIO(binary))
        self.archive = data.getvalue()
        self.config = {'components': {'addon_dependencies': dict(mise_tool='github:owner/gdam',
                       sha256=hashlib.sha256(self.archive).hexdigest(),destination='.artifacts/bin/gdam')}}
        self.downloads = []
        self.commands = []
        self.options = []

    def fetch(self, url, destination):
        self.downloads.append(url)
        destination.write_bytes(self.archive)

    def execute(self, command, **options):
        self.commands.append(command)
        self.options.append(options)
        return subprocess.CompletedProcess(command, 0, 'gdam 0.1.5\n', '')

    def test_reviewed_bytes_are_installed_and_only_frozen_restore_is_run(self):
        addon_dependencies.setup(self.root, self.config, {}, fetch=self.fetch, execute=self.execute)
        self.assertIn('/v0.1.5/gdam_Linux_x86_64.tar.gz', self.downloads[0])
        self.assertEqual(self.commands[-1][1:], ['install', '--frozen-lockfile'])
        addon_dependencies.setup(self.root, self.config, {}, fetch=self.fetch, execute=self.execute)
        self.assertEqual(len(self.downloads), 1)
        (self.root / '.artifacts/bin/gdam').write_bytes(b'cache mutation')
        addon_dependencies.setup(self.root, self.config, {}, fetch=self.fetch, execute=self.execute)
        self.assertEqual(len(self.downloads), 2)

    def package(self):
        (self.root / 'addon').mkdir()
        write_source(self.root / 'gdam.toml', {'addons': {}, 'package': 'addon'})
        write_source(self.root / 'addon/gdam.toml', {'addons': {'@example/leaf': {'tag': 'v1.0.0'}}})
        (self.root / '.gitignore').write_text('.artifacts/\n/gdam.json\n/addon/gdam.json\n')
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)

    def test_package_projection_exists_only_during_unchanged_frozen_native_call(self):
        self.package()
        original = {path: (self.root / path).read_bytes() for path in ('gdam.toml', 'addon/gdam.toml')}
        def native(command, **options):
            result = self.execute(command, **options)
            if command[1:] == ['install', '--frozen-lockfile']:
                self.assertEqual(json.loads((self.root / 'addon/gdam.json').read_bytes()),
                                 {'addons': {'@example/leaf': {'tag': 'v1.0.0'}}})
                self.assertEqual(options, dict(cwd=self.root, env={'NATIVE': 'unchanged'}, timeout=180, check=True))
                bridge = self.root / 'addon/.gdam/deps.gd'
                bridge.parent.mkdir()
                bridge.write_text('native generated bridge\n')
            return result
        addon_dependencies.setup(self.root, self.config, {'NATIVE': 'unchanged'}, fetch=self.fetch, execute=native)
        self.assertFalse((self.root / 'addon/gdam.json').exists())
        receipts = list((self.root / '.artifacts/runtime-config').glob('*/gdam.receipt.json'))
        self.assertEqual(len(receipts), 1)  # only the existing root projection survives
        self.assertTrue((self.root / 'addon/.gdam/deps.gd').is_file())
        self.assertEqual(original, {path: (self.root / path).read_bytes() for path in original})

    def test_package_projection_cleans_exact_owned_bytes_on_native_failure(self):
        self.package()
        def native(command, **options):
            if command[1:] == ['install', '--frozen-lockfile']:
                raise subprocess.CalledProcessError(19, command)
            return self.execute(command, **options)
        with self.assertRaises(subprocess.CalledProcessError) as error:
            addon_dependencies.setup(self.root, self.config, {}, fetch=self.fetch, execute=native)
        self.assertEqual(error.exception.returncode, 19)
        self.assertFalse((self.root / 'addon/gdam.json').exists())

    def test_invalid_or_missing_package_source_refuses_before_install(self):
        self.package()
        (self.root / 'linked').symlink_to(self.root / 'addon', target_is_directory=True)
        for value in ('', '.', '..', '/tmp', '.git', 'linked', 'absent', 'mise.toml', 3):
            with self.subTest(package=value):
                write_source(self.root / 'gdam.toml', {'addons': {}, 'package': value})
                self.commands.clear()
                with self.assertRaises((ValueError, OSError)):
                    addon_dependencies.setup(self.root, self.config, {}, fetch=self.fetch, execute=self.execute)
                self.assertTrue(all(command[1:] != ['install', '--frozen-lockfile'] for command in self.commands))
        write_source(self.root / 'gdam.toml', {'addons': {}, 'package': 'addon'})
        (self.root / 'addon/gdam.toml').unlink()
        with self.assertRaises((ValueError, OSError)):
            addon_dependencies.setup(self.root, self.config, {}, fetch=self.fetch, execute=self.execute)
        (self.root / 'untracked').mkdir()
        write_source(self.root / 'untracked/gdam.toml', {'addons': {}})
        write_source(self.root / 'gdam.toml', {'addons': {}, 'package': 'untracked'})
        with self.assertRaisesRegex(ValueError, 'tracked canonical'):
            addon_dependencies.setup(self.root, self.config, {}, fetch=self.fetch, execute=self.execute)

    def test_foreign_package_projection_is_preserved_and_never_installed(self):
        self.package()
        target = self.root / 'addon/gdam.json'
        target.write_text('{"foreign":true}')
        with self.assertRaisesRegex(ValueError, 'absent'):
            addon_dependencies.setup(self.root, self.config, {}, fetch=self.fetch, execute=self.execute)
        self.assertEqual(target.read_text(), '{"foreign":true}')
        self.assertTrue(all(command[1:] != ['install', '--frozen-lockfile'] for command in self.commands))

    def test_checksum_mismatch_never_executes_unverified_sdk(self):
        self.config['components']['addon_dependencies']['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'checksum'):
            addon_dependencies.setup(self.root, self.config, {}, fetch=self.fetch, execute=self.execute)
        self.assertEqual(self.commands, [])

    def test_sole_mise_version_authority_and_native_binary_type_are_checked(self):
        (self.root / 'mise.toml').write_text('[tools]\n"github:owner/gdam" = "latest"\n')
        with self.assertRaisesRegex(ValueError, 'exact Mise'):
            addon_dependencies.setup(self.root, self.config, {}, fetch=self.fetch, execute=self.execute)
        self.assertEqual(self.downloads, [])


if __name__ == '__main__':
    unittest.main()
