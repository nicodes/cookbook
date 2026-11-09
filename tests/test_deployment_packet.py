from pathlib import Path
import copy
import hashlib
import json
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
