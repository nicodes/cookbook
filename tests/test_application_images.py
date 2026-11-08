from source_fixture import SDK_ROOT
import importlib.util
import contextlib
import json
import hashlib
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
    def test_static_image_keeps_exact_bytes_and_reports_bounded_hash_differences(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';installed=root/'installed'
            source.mkdir();installed.mkdir()
            (source/'index.html').write_bytes(b'reviewed')
            (installed/'index.html').write_bytes(b'reviewed')
            def docker(command,**kwargs):
                if command[:2]==['docker','cp']:
                    shutil.copytree(installed,command[-1],dirs_exist_ok=True)
            with patch.object(images.subprocess,'check_output',return_value='a'*64),patch.object(images.subprocess,'run',side_effect=docker) as run:
                self.assertEqual(images.static_image('reviewed-image',source),
                                 {'index.html':hashlib.sha256(b'reviewed').hexdigest()})
                (installed/'index.html').write_bytes(b'changed')
                for i in range(20):(installed/('extra-'+str(i))).write_bytes(b'extra')
                (source/'missing').write_bytes(b'expected')
                with self.assertRaisesRegex(ValueError,'complete source bytes') as error:
                    images.static_image('reviewed-image',source)
                message=json.loads(str(error.exception).split(': ',1)[1])
                self.assertEqual(message['missing'],['missing'])
                self.assertEqual(message['extra_count'],20)
                self.assertEqual(len(message['extra']),10)
                self.assertEqual(message['changed'],[{'path':'index.html',
                    'source_sha256':hashlib.sha256(b'reviewed').hexdigest(),
                    'installed_sha256':hashlib.sha256(b'changed').hexdigest()}])
                self.assertEqual(run.call_args.args[0],['docker','rm','-v','a'*64])

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
        for name in ['api', 'gate', 'config']:
            (self.root / 'deploy/images' / (name + '.Dockerfile')).write_text('FROM scratch\n')
        self.value = {'image_base': 'ghcr.io/owner/example', 'inputs': {'images': [
            'ghcr.io/owner/example-api:{revision}', 'ghcr.io/owner/example-gate:{revision}',
            'ghcr.io/owner/example-config:{revision}']}}
        self.config = {'components': {'images': [{'name': name} for name in ['api', 'gate', 'config']]}}
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
                         ['ghcr.io/owner/example-' + name + ':' + 'a' * 40 for name in ['api', 'gate', 'config']])

    def test_missing_or_duplicate_shipping_components_fail_before_building(self):
        for records in [[{'name': 'api'}], [{'name': 'api'}, {'name': 'api'}, {'name': 'gate'}, {'name': 'config'}]]:
            with self.subTest(records=records), self.assertRaises(ValueError):
                self.build({'components': {'images': records}})
            self.assertEqual(self.calls, [])

    def test_native_preparation_is_never_silently_skipped(self):
        self.config['components']['images'][0]['native'] = [{'path': 'internal/compiler.py'}]
        with self.assertRaises(ValueError):
            self.build()
        self.assertEqual(self.calls, [])

    def test_compose_validation_uses_only_process_environment_and_no_dotenv(self):
        source = self.root / 'deploy/compose.yml'
        source.write_text('services: {}\n')
        (self.root / '.env').write_text('NEVER_READ=private-local-data\n')
        observed = []
        def validate(command, _root, environment):
            directory = Path(command[command.index('--project-directory') + 1])
            observed.append({path.name for path in directory.iterdir()})
            self.assertEqual(command[command.index('--env-file') + 1], os.devnull)
            self.assertIn('--no-env-resolution', command)
            self.assertEqual(environment, self.environment)
        config = {'components': {'compose': {}}}
        images.validate_compose(self.root, config, self.environment, validate)
        self.assertEqual(observed, [{'compose.yml'}])
        self.assertEqual(source.read_text(), 'services: {}\n')
        with self.assertRaises(ValueError):
            images.validate_compose(self.root, {'components': {'compose': {'environment_files': ['../outside']}}}, {}, validate)

    def test_runtime_model_never_materializes_or_reads_checkout_secrets(self):
        (self.root / 'deploy/compose.yml').write_text('services: {}\n')
        secret = self.root / 'deploy/secrets/postgres-owner.env'
        secret.parent.mkdir()
        secret.write_text("PRIVATE='deliberately-invalid\n")
        config = {'components': {'compose': {}}}
        observed = []
        def model(command, **kwargs):
            target = Path(command[command.index('--project-directory') + 1])
            observed.append(target)
            self.assertEqual({path.name for path in target.iterdir()}, {'compose.yml'})
            self.assertEqual(command[command.index('--env-file') + 1], os.devnull)
            self.assertIn('--no-env-resolution', command)
            return '{"services":{"api":{"read_only":true}}}'
        with patch.object(images.subprocess, 'check_output', side_effect=model):
            self.assertTrue(images.compose_model(self.root, config, {})['services']['api']['read_only'])
        self.assertFalse(observed[0].exists())
        self.assertEqual(secret.read_text(), "PRIVATE='deliberately-invalid\n")

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
            if command[1:3] == ['image', 'inspect']:
                return json.dumps([{'Id': 'sha256:' + 'b' * 64, 'Config': {'User': '10001:10001'}}])
            return 'c' * 64
        def docker(command, **_kwargs):
            if command[1] == 'cp':
                data = b'locked-dependencies' if command[2].endswith('/Cargo.lock') else binary.read_bytes() + (b'changed' if tampered else b'')
                Path(command[3]).write_bytes(data)
            if command[1] == 'rm':
                removed.append(command[3])
            return subprocess.CompletedProcess(command, 0)
        with patch.object(images.subprocess, 'check_output', side_effect=query), patch.object(images.subprocess, 'run', side_effect=docker):
            images.rust_image(self.root, 'image:source', contract)
            tampered.append(True)
            with self.assertRaisesRegex(ValueError, 'accepted native executable'):
                images.rust_image(self.root, 'image:source', contract)
        self.assertEqual(removed, ['c' * 64, 'c' * 64])

    def test_browser_fixture_uses_the_accepted_image_and_cleans_up_after_journey_failure(self):
        directory = self.root / '.artifacts/verification'
        directory.mkdir(parents=True)
        reference = 'ghcr.io/owner/example-gate:' + 'a' * 40
        (directory / 'build.json').write_text(json.dumps({'artifacts': {'image:' + reference: 'sha256:' + 'b' * 64}}))
        commands = []
        def docker(command, **_kwargs):
            commands.append(command)
            if command[1] == 'run':
                Path(command[command.index('--cidfile') + 1]).write_text('c' * 64)
                self.assertEqual(command[-1], 'sha256:' + 'b' * 64)
            return subprocess.CompletedProcess(command, 0, stdout='', stderr='')
        opener = SimpleNamespace(open=lambda request, timeout: contextlib.nullcontext(SimpleNamespace(status=200)))
        config = {'components': {'browser_gate': {'hostname': 'app.example.invalid'}}}
        with patch.object(images, 'definition', return_value=self.value), \
                patch.object(images.subprocess, 'run', side_effect=docker), \
                patch.object(images.subprocess, 'check_output', return_value='127.0.0.1:8181\n'), \
                patch.object(images.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(ValueError, 'journey assertion'):
                with images.gate_fixture(self.root, config, self.environment) as selected:
                    self.assertEqual(selected['E2E_URL'], 'http://app.example.invalid:8181/')
                    raise ValueError('journey assertion')
        self.assertEqual(commands[-1], ['docker', 'rm', '-f', 'c' * 64])


if __name__ == '__main__':
    unittest.main()
