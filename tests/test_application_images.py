from source_fixture import SDK_ROOT
import importlib.util
import contextlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

COOKBOOK = SDK_ROOT / 'cookbook'
sys.path.insert(0, str(COOKBOOK))
spec = importlib.util.spec_from_file_location('application_images', COOKBOOK / 'images.py')
images = importlib.util.module_from_spec(spec)
spec.loader.exec_module(images)


class ApplicationImages(unittest.TestCase):
    def test_static_archive_retains_complete_source_bytes_without_any_container(self):
        import artifact_packet
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'public';source.mkdir()
            (source/'index.html').write_bytes(b'reviewed')
            nested=source/'assets';nested.mkdir();(nested/'payload.bin').write_bytes(b'opaque bytes')
            archive=root/'.artifacts/release/static/web.tar.gz';archive.parent.mkdir(parents=True)
            artifact_packet.pack_static(source,archive,max_bytes=4096,max_file_bytes=1024)
            config={'components':{'assets':[{'name':'web','source':'public','max_bytes':4096,'max_file_bytes':1024}]}}
            with patch.object(images.deployment,'verify',return_value={'application':'example'}),\
                 patch.object(images.native_runtime,'run') as run:
                self.assertEqual(images.validate_static_assets(root,config,{}),{'application':'example'})
                (source/'index.html').write_bytes(b'changed after packaging')
                (source/'assets/payload.bin').unlink()
                (source/'new-file').write_bytes(b'extra source member')
                with self.assertRaisesRegex(ValueError,'complete source bytes'):
                    images.validate_static_assets(root,config,{})
            run.assert_not_called()

    def test_empty_runtime_staging_never_copies_retained_private_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {'components': {'empty_directories': [{'path': '.artifacts/media', 'mode': 0o700}]}}
            images.stage_empty_directories(root, config)
            path = root / '.artifacts/media'
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            self.assertEqual(list(path.iterdir()), [path / '.keep'])
            (path / 'private-photo').write_bytes(b'retained private bytes')
            with self.assertRaisesRegex(ValueError, 'retained or private data'):
                images.stage_empty_directories(root, config)
            self.assertEqual((path / 'private-photo').read_bytes(), b'retained private bytes')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='image source ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'deploy/images').mkdir(parents=True)
        for name in ['api', 'gate', 'renderer']:
            (self.root / 'deploy/images' / (name + '.Dockerfile')).write_text('FROM scratch\n')
        self.value = {'image_base': 'ghcr.io/owner/example', 'inputs': {'images': [
            'ghcr.io/owner/example-api:{revision}', 'ghcr.io/owner/example-renderer:{revision}']}}
        self.config = {'components': {'images': [{'name': name} for name in ['api', 'gate', 'renderer']]}}
        self.environment = {'APP_VERSION': 'a' * 40}
        self.calls = []

    def capture(self, command, root, environment):
        self.calls.append((command, root, environment))

    def build(self, config=None, native=None):
        with patch.object(images, 'definition', return_value=self.value):
            images.build(self.root, config or self.config, self.environment, self.capture, native=native)

    def test_build_uses_only_declared_images_and_dedicated_component_cache(self):
        self.config['components']['images'][0].update(cache_key='api', build_arguments={'SOURCE_REVISION': '{revision}'})
        self.build()
        first = self.calls[0][0]
        self.assertIn('--cache-key', first)
        self.assertIn('SOURCE_REVISION=' + 'a' * 40, first)
        self.assertNotIn('--push', first)
        self.assertEqual([call[0][call[0].index('-t') + 1] for call in self.calls],
                         ['ghcr.io/owner/example-' + name + ':' + 'a' * 40 for name in ['api', 'gate', 'renderer']])

    def test_missing_or_duplicate_shipping_components_fail_before_building(self):
        for records in [[{'name': 'api'}], [{'name': 'api'}, {'name': 'api'}, {'name': 'gate'}, {'name': 'renderer'}]]:
            with self.subTest(records=records), self.assertRaises(ValueError):
                self.build({'components': {'images': records}})
            self.assertEqual(self.calls, [])

    def test_native_preparation_is_never_silently_skipped(self):
        self.config['components']['images'][0]['native'] = [{'path': 'internal/compiler.py'}]
        with self.assertRaises(ValueError):
            self.build()
        self.assertEqual(self.calls, [])



    def test_rust_scan_requires_every_lock_inventory_and_blocks_high_findings(self):
        manifests = ['api/Cargo.toml', 'internal/runtime/Cargo.toml']
        for name in manifests:
            directory = (self.root / name).parent
            directory.mkdir(parents=True)
            (directory / 'Cargo.lock').write_text('version=4\n')
        findings = []
        def scan(command, _root, _environment):
            report = {'SchemaVersion': 2, 'Results': [
                {'Target': str(index) + '/Cargo.lock', 'Type': 'cargo', 'Packages': [{'Name': 'dependency'}],
                 'Vulnerabilities': findings} for index in range(2)]}
            if command[command.index('--format')+1]=='cyclonedx':report={'bomFormat':'CycloneDX','components':[{'name':'dependency'}]}
            Path(command[command.index('--output') + 1]).write_text(json.dumps(report))
        images.rust_inventory(self.root, manifests, {}, scan,sbom=True)
        self.assertEqual(json.loads((self.root/'.artifacts/security/rust-source-sbom.json').read_text())['bomFormat'],'CycloneDX')
        findings.append({'Severity': 'HIGH', 'FixedVersion': ''})
        with self.assertRaisesRegex(ValueError, 'High/Critical'):
            images.rust_inventory(self.root, manifests, {}, scan)

    def test_rust_runtime_matches_exact_binary_and_always_removes_inspection_container(self):
        (self.root / 'api').mkdir()
        (self.root / 'api/Cargo.lock').write_bytes(b'locked-dependencies')
        (self.root / '.artifacts').mkdir()
        binary = self.root / '.artifacts/service'
        binary.write_bytes(b'\x7fELFcompiled-source')
        contract = {'lock_source': 'api/Cargo.lock', 'lock_installed': '/usr/share/service/Cargo.lock',
                    'binaries': [{'source': '.artifacts/service', 'installed': '/usr/local/bin/service'}]}
        removed, tampered = [], []
        def query(command, **_kwargs):
            return 'c' * 64
        def docker(command, **_kwargs):
            if command[0] == 'cp':
                data = b'locked-dependencies' if command[1].endswith('/Cargo.lock') else binary.read_bytes() + (b'changed' if tampered else b'')
                Path(command[2]).write_bytes(data)
            if command[0] == 'rm':
                removed.append(command[2])
            return subprocess.CompletedProcess(command, 0)
        with patch.object(images.native_runtime, 'inspect', return_value=[{'Id':'sha256:'+'b'*64,'Config':{'User':'10001:10001'}}]), patch.object(images.native_runtime, 'capture', side_effect=query), patch.object(images.native_runtime, 'run', side_effect=docker):
            images.rust_image(self.root, 'image:source', contract)
            tampered.append(True)
            with self.assertRaisesRegex(ValueError, 'accepted native executable'):
                images.rust_image(self.root, 'image:source', contract)
        self.assertEqual(removed, ['c' * 64, 'c' * 64])

    def test_browser_fixture_selects_only_the_authored_workload_hostname(self):
        import workload_fixture
        settings = {'components': {'workload_browser': {'hostname': 'app.example.invalid'}}}
        fixture = SimpleNamespace(environment={'E2E_URL': 'http://app.example.invalid:8181/'})
        with patch.object(workload_fixture, 'context', return_value=contextlib.nullcontext(fixture)) as launch:
            with images.browser_fixture(self.root, settings, self.environment) as selected:
                self.assertEqual(selected, fixture.environment)
        self.assertEqual(launch.call_args.kwargs, {'hostname': 'app.example.invalid'})
        settings['components']['workload_browser']['component'] = 'gate'
        with self.assertRaises(ValueError):
            with images.browser_fixture(self.root, settings, self.environment):
                self.fail('legacy image selector admitted')


if __name__ == '__main__':
    unittest.main()
