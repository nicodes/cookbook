from pathlib import Path
import copy
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cookbook'))
import deployment
import artifact_packet


class DeploymentPacketTests(unittest.TestCase):
    def workload(self):
        return {'services': {}, 'jobs': {}, 'assets': {'web': {'artifact': 'web',
            'serving': {'headers': {'X-Content-Type-Options': 'nosniff'}}}},
            'networks': {}, 'volumes': {},
            'routes': [{'host': 'app.example.test', 'path': '/', 'target': {'kind': 'asset', 'name': 'web'}}],
            'readiness': [{'url': 'https://app.example.test/', 'status': 200}]}

    def test_static_manifest_matches_protocol_vector(self):
        body = deployment.static_manifest('sha256:' + 'a' * 64, 123)
        value = json.loads(body)
        self.assertEqual(value['config']['digest'], 'sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a')
        self.assertEqual(value['layers'], [{'mediaType': deployment.STATIC_MEDIA, 'digest': 'sha256:' + 'a' * 64, 'size': 123}])
        self.assertEqual(body, deployment.canonical(value))

    def test_static_application_has_no_image_runtime_or_legacy_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'public').mkdir()
            (root / 'public/index.html').write_bytes(b'<main>source-bound export</main>')
            definition = {'project': 'demo', 'image_base': 'ghcr.io/example/demo', 'inputs': {'images': []}}
            config = {'components': {'assets': [{'name': 'web', 'source': 'public',
                'max_bytes': 4096, 'max_file_bytes': 1024}]}}
            authored = {'kind': 'komizo.application', 'version': 1, 'application': 'demo', 'workload': self.workload()}
            source = {'repository': 'example/demo', 'repository_id': '123', 'revision': 'a' * 40}
            providers = {'cookbook_revision': 'b' * 40, 'tools_revision': 'c' * 40, 'core_revision': 'd' * 40}
            with patch.object(deployment, 'declaration', return_value=authored), patch.object(deployment, 'source', return_value=source), patch.object(deployment.dependencies, 'helper'), patch.object(deployment, 'native_validate'), patch.object(deployment, 'provider_provenance', return_value=providers):
                release = deployment.package(root, config, {'APP_VERSION': 'a' * 40}, definition)
                deployment.verify(root, {'APP_VERSION': 'a' * 40})
            self.assertEqual(release['providers'], providers)
            self.assertEqual(set(release['artifacts']), {'web'})
            self.assertEqual(release['artifacts']['web']['kind'], 'archive')
            self.assertEqual(artifact_packet.packet_files(root / '.artifacts/release'),
                             {'release.json', 'static/web.tar.gz', 'inventory.json'})
            archive = root / '.artifacts/release/static/web.tar.gz'
            self.assertEqual(release['artifacts']['web']['digest'], 'sha256:' + hashlib.sha256(archive.read_bytes()).hexdigest())

    def test_container_graph_cannot_gain_undeclared_authority(self):
        workload = self.workload()
        workload['services']['api'] = {'artifact': 'api', 'needs': {'migrate': 'completed'}, 'networks': ['private']}
        with self.assertRaisesRegex(ValueError, 'undeclared network'):
            deployment.validate_workload(workload)

        workload['networks']['private'] = {'internal': True}
        with self.assertRaisesRegex(ValueError, 'undeclared dependency'):
            deployment.validate_workload(workload)
        workload['jobs']['migrate'] = {'artifact': 'api', 'timeout_seconds': 60}
        deployment.validate_workload(workload)
        workload['jobs']['migrate']['privileged'] = True
        with self.assertRaisesRegex(ValueError, 'unknown fields'):
            deployment.validate_workload(workload)

    def test_actual_native_image_archive_and_failed_replacement_preserve_packet(self):
        if sys.platform != 'linux':
            self.skipTest('native Podman image packet acceptance requires Linux')
        if shutil.which('podman') is None:
            if sys.platform == 'linux' and os.environ.get('GITHUB_ACTIONS') == 'true':
                self.fail('the provider CI must prepare native Podman for image packet acceptance')
            self.skipTest('actual archive export runs in the prepared native provider CI')
        from source_fixture import tool_fixture
        import dependencies
        runtime = dependencies.helper('container_runtime')
        oci = dependencies.helper('oci_archive')
        fixture = tool_fixture('image_scan_fixture')
        revision = secrets.token_hex(20)
        family = 'ghcr.io/example/packet-' + secrets.token_hex(12)
        reference = family + '-api:' + revision
        environment = dict(os.environ, APP_VERSION=revision)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'public').mkdir()
            (root / 'public/index.html').write_bytes(b'accepted static bytes')
            incoming = root / 'incoming.oci.tar'
            inspected, _, _ = fixture.archive(incoming, reference=reference, source=revision,
                body=('owned native packet fixture ' + revision).encode())
            identity = inspected['Id']
            self.assertEqual(runtime.run(['image', 'exists', reference], environment=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30).returncode, 1)
            self.assertEqual(runtime.run(['image', 'exists', identity], environment=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30).returncode, 1)
            try:
                oci.load_image(incoming, reference, identity, environment=environment, timeout=60)
                runtime.run(['tag', identity, reference], environment=environment, check=True, timeout=30)
                workload = self.workload()
                workload['networks']['app'] = {'internal': False}
                workload['services']['api'] = {'artifact': 'api', 'user': '10001:10001',
                    'networks': ['app'], 'limits': {'memory_bytes': 64 << 20,
                        'memory_swap_bytes': 64 << 20, 'milli_cpus': 500, 'pids': 32}}
                authored = {'kind': 'komizo.application', 'version': 1,
                    'application': 'demo', 'workload': workload}
                source = {'repository': 'example/demo', 'repository_id': '123', 'revision': revision}
                providers = {'cookbook_revision': 'b' * 40, 'tools_revision': 'c' * 40,
                    'core_revision': 'd' * 40}
                config = {'components': {'assets': [{'name': 'web', 'source': 'public',
                    'max_bytes': 4096, 'max_file_bytes': 1024}]}}
                definition = {'project': 'demo', 'image_base': family,
                    'inputs': {'images': [family + '-api:{revision}']}}
                with patch.object(deployment, 'declaration', return_value=authored), \
                        patch.object(deployment, 'source', return_value=source), \
                        patch.object(deployment, 'provider_provenance', return_value=providers), \
                        patch.object(deployment, 'native_validate'):
                    release = deployment.package(root, config, environment, definition)
                    packet = root / '.artifacts/release'
                    metadata = oci.inspect_archive(packet / 'images/api.oci.tar', reference, identity)
                    self.assertEqual(release['artifacts']['api']['digest'], metadata['manifest_digest'])
                    self.assertEqual(release['artifacts']['api']['config_digest'], identity)
                    deployment.verify(root, environment, expected_source=source)
                    accepted = {name: (packet / name).read_bytes()
                        for name in artifact_packet.packet_files(packet)}
                    runtime.run(['untag', identity, reference], environment=environment,
                        check=True, timeout=30)
                    with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                        deployment.package(root, config, environment, definition)
                    self.assertEqual(accepted, {name: (packet / name).read_bytes()
                        for name in artifact_packet.packet_files(packet)})
                    self.assertFalse(list((root / '.artifacts').glob('.release-*')))
            finally:
                exists = runtime.run(['image', 'exists', identity], environment=environment,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30).returncode
                self.assertIn(exists, {0, 1})
                if exists == 0:
                    current = runtime.inspect_images([identity], environment=environment)[0]
                    self.assertEqual(current['Id'], identity)
                    self.assertEqual(current['Config']['Labels']['org.opencontainers.image.revision'], revision)
                    self.assertTrue(set(current.get('RepoTags') or []) <= {reference})
                    runtime.run(['rmi', identity], environment=environment, check=True, timeout=30)

    def test_provider_facts_are_closed_exact_source_revisions(self):
        providers = {'cookbook_revision': 'b' * 40, 'tools_revision': 'c' * 40, 'core_revision': 'd' * 40}
        deployment.validate_providers(providers)
        for changed in [providers | {'core_revision': 'main'}, providers | {'controller_revision': 'e' * 40},
                        {name: value for name, value in providers.items() if name != 'tools_revision'}]:
            with self.assertRaises(ValueError):
                deployment.validate_providers(changed)

    def test_route_cannot_both_redirect_and_expose_service(self):
        workload = self.workload()
        workload['routes'][0]['redirect'] = {'to': 'https://app.example.test', 'status': 308, 'preserve_uri': True}
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            deployment.validate_workload(workload)

    def test_closed_packet_rejects_previous_release_or_unselected_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'release.json').write_text('{}')
            (root / 'config-image.tar').write_bytes(b'legacy transport')
            with self.assertRaisesRegex(ValueError, 'undeclared'):
                artifact_packet.write_inventory(root, {'release.json': 'release'})


if __name__ == '__main__':
    unittest.main()
