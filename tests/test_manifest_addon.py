from source_fixture import SDK_ROOT
from pathlib import Path
import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import manifest_addon


class ManifestArchiveControls(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        addon = self.root / 'addon'
        addon.mkdir()
        (addon / 'plugin.cfg').write_text('[plugin]\nversion="1.2.3"\n')
        (addon / 'plugin.gd').write_bytes(b'extends EditorPlugin\n')
        (addon / 'plugin.gd.uid').write_text('uid://abc123\n')
        self.names = ['plugin.cfg', 'plugin.gd', 'plugin.gd.uid']
        (self.root / 'package.files').write_text('\n'.join(self.names) + '\n')
        (self.root / 'package.json').write_text('{"version":"1.2.3"}')
        manifest_addon.package_helper().source_config.write(self.root / 'package-allowlist.toml', dict(files=self.names, addon_files=3, zip_entries=4))
        self.config = {'recipe': 'godot-addon', 'package': dict(archive='dist/@owner_plugin.zip',
                                                               embedded_manifest=True, script_uids=True)}

    def test_build_retains_exact_payload_and_both_archive_shapes(self):
        manifest_addon.build(self.root, self.config)
        # Frozen from the accepted pre-contract writer, including ZIP order,
        # permissions, timestamps, manifest bytes and both checksum sidecars.
        expected = {
            '@owner_plugin.zip': '2f9a575721c1173ac67af8c8b12a282bf66f5d0bd3df7dc608299a3c3a666359',
            '@owner_plugin.zip.sha256': '36a11ad99a0426180e352d4e231b74a322e66d409123c2f441e639c16c6d2449',
            '@owner_plugin.gdam.zip': 'f307003054977cb0392525e315f6171e412731aeed4721e49e493efaed73d2c2',
            '@owner_plugin.gdam.zip.sha256': '153c0a1aefab49302a5a5ea5db243cc1bca84e96b2d4f8123a234c92728422a0',
            'PACKAGE_MANIFEST.json': '9a77b1263ce390af465e5245f9dd0b8433fd131f7070c58307226c481dc4fbb8',
        }
        self.assertEqual({path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in (self.root / 'dist').iterdir()}, expected)
        with zipfile.ZipFile(self.root / 'dist/@owner_plugin.zip') as archive:
            self.assertEqual(archive.namelist(), ['addons/@owner_plugin/' + name for name in self.names] + ['PACKAGE_MANIFEST.json'])
            for name in self.names:
                self.assertEqual(archive.read('addons/@owner_plugin/' + name), (self.root / 'addon' / name).read_bytes())
            self.assertEqual(json.loads(archive.read('PACKAGE_MANIFEST.json'))['plugin_version'], '1.2.3')
        with zipfile.ZipFile(self.root / 'dist/@owner_plugin.gdam.zip') as archive:
            self.assertEqual(archive.namelist(), self.names)
        first = (self.root / 'dist/@owner_plugin.zip').read_bytes()
        manifest_addon.build(self.root, self.config)
        self.assertEqual((self.root / 'dist/@owner_plugin.zip').read_bytes(), first)
        self.assertEqual(len(manifest_addon.artifact_paths(self.root, self.config)), 5)

    def test_mutated_payload_source_checksum_and_external_provenance_are_rejected(self):
        manifest_addon.build(self.root, self.config)
        (self.root / 'dist/PACKAGE_MANIFEST.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'external'):
            manifest_addon.verify(self.root, self.config)
        manifest_addon.build(self.root, self.config)
        (self.root / 'addon/plugin.gd').write_bytes(b'mutated source')
        with self.assertRaisesRegex(ValueError, 'source'):
            manifest_addon.verify(self.root, self.config)
        manifest_addon.build(self.root, self.config)
        archive = self.root / 'dist/@owner_plugin.gdam.zip'
        archive.with_suffix('.zip.sha256').write_text('0' * 64 + '  ' + archive.name + '\n')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            manifest_addon.verify(self.root, self.config)

    def test_only_untracked_native_dependency_output_may_be_excluded(self):
        self.config['components'] = {'addon_dependencies': {'mise_tool': 'github:owner/gdam',
            'destination': '.artifacts/bin/gdam', 'sha256': '0' * 64}}
        generated = self.root / 'addon/.gdam'
        generated.mkdir()
        (generated / 'deps.gd').write_text('generated fixture')
        manifest_addon.build(self.root, self.config)
        subprocess.run(['git', 'add', 'addon/.gdam'], cwd=self.root, check=True)
        with self.assertRaisesRegex(ValueError, 'tracked'):
            manifest_addon.build(self.root, self.config)

    def test_version_counts_and_uid_changes_fail_closed(self):
        (self.root / 'package.json').write_text('{"version":"1.2.4"}')
        with self.assertRaisesRegex(ValueError, 'versions'):
            manifest_addon.build(self.root, self.config)
        (self.root / 'package.json').write_text('{"version":"1.2.3"}')
        (self.root / 'addon/plugin.gd.uid').write_text('bad uid')
        with self.assertRaisesRegex(ValueError, 'UID'):
            manifest_addon.build(self.root, self.config)

    def test_flat_payload_matches_the_existing_low_level_writer_byte_for_byte(self):
        self.config['package'].pop('embedded_manifest')
        manifest_addon.build(self.root, self.config)
        archive = self.root / 'dist/@owner_plugin.zip'
        reference = self.root / 'legacy.zip'
        manifest_addon.package_helper().build(self.root / 'addon', self.root / 'package.files', reference, True)
        self.assertEqual(archive.read_bytes(), reference.read_bytes())
        self.assertEqual(manifest_addon.artifact_paths(self.root, self.config), [
            'dist/@owner_plugin.zip', 'dist/@owner_plugin.zip.sha256'])
        (self.root / 'addon/plugin.gd.uid').write_text('invalid')
        with self.assertRaisesRegex(ValueError, 'UID'):
            manifest_addon.verify(self.root, self.config)

    def test_paths_formats_and_generated_exclusions_are_not_caller_switches(self):
        for name, value in [('source', 'other'), ('manifest', 'other.files'), ('format', 'closed'),
                            ('registry_archive', 'dist/other.zip'), ('prefix', 'other/'),
                            ('version_source', 'other.json'), ('allowlist', 'other.json'),
                            ('generated_directories', ['addon'])]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                manifest_addon.build(self.root, {**self.config, 'package': {**self.config['package'], name: value}})
        self.assertFalse((self.root / 'dist').exists())


if __name__ == '__main__':
    unittest.main()
