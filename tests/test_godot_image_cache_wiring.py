from source_fixture import SDK_ROOT
"""Every image import uses the same fully admitted current-source image set."""
from contextlib import chdir
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = SDK_ROOT
sys.path.insert(0, str(TOOLS / 'cookbook'))
import ci
import godot_ci
import image_import_cache
import image_fixtures
from source_fixture import read_source, write_source, dump_source


class ImageCacheWiring(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'app').mkdir(); (self.root / 'app/project.godot').write_text('config_version=5\n')
        (self.root / 'app/assertion.gd').write_text('extends SceneTree\n')
        (self.root / 'cookbook.toml').write_text('version=1\nrecipe="godot-game"\n')
        for args in [['init', '-q'], ['add', '.'], ['-c', 'user.name=Fixture', '-c',
                     'user.email=fixture@example.test', 'commit', '-qm', 'source fixture']]:
            subprocess.run(['git', '-C', str(self.root), *args], check=True)
        self.source = subprocess.check_output(['git', '-C', str(self.root), 'rev-parse', 'HEAD'], text=True).strip()
        self.reference = 'example.invalid/fixture-image:' + self.source
        self.identity = 'sha256:' + '1' * 64
        self.path = '.artifacts/release/images/image.oci.tar'
        self.incoming = self.root / 'incoming'
        target = self.incoming / 'payload' / self.path; target.parent.mkdir(parents=True)
        target.write_bytes(b'accepted transfer; helper authenticates actual tar semantics')
        self.hash = hashlib.sha256(target.read_bytes()).hexdigest()
        self.config = {'recipe': 'godot-game', 'components': {'godot': {}},
            'ci': {'godot': {'unit_shards': 1}},
            'tests': {'unit': [{'name': 'test-one', 'runner': 'godot-check', 'path': 'app/assertion.gd'}]},
            'artifacts': {'paths': ['.artifacts/release'], 'images': [self.reference]}}
        import deployment
        admitted=patch.object(deployment,'verify',return_value={'artifacts':{'image':{'kind':'image'}}})
        admitted.start();self.addCleanup(admitted.stop)
        self.proof = {'source_commit': self.source, 'cookbook_commit': 'b' * 40,
            'configuration_sha256': godot_ci.configuration_hash(self.root), 'executables': {self.path: False},
            'build': {'identity': '2' * 64, 'artifacts': {self.path: self.hash, 'image:' + self.reference: self.identity}}}

    def test_admitted_images_use_closed_scope_and_original_total_cap_before_receipt(self):
        with (chdir(self.root), patch.object(ci, 'identity') as admit,
                patch.object(image_import_cache, 'reload') as reload,
                patch.object(godot_ci, 'configuration_hash', return_value=self.proof['configuration_sha256']),
                patch.object(godot_ci.subprocess, 'run') as legacy_load):
            godot_ci.import_build(self.root, self.config, self.incoming, self.proof)
        admit.assert_called_once_with(self.root, self.source, 'b' * 40)
        reload.assert_called_once()
        self.assertEqual(reload.call_args.args[:4], (self.root, self.source,
            [{'path': self.path, 'sha256': self.hash}], {self.reference: self.identity}))
        self.assertEqual(reload.call_args.kwargs, {'timeout': 600})
        legacy_load.assert_not_called()
        self.assertEqual(json.loads((self.root / '.artifacts/verification/build.json').read_text()), self.proof['build'])

    def test_file_only_import_requires_no_engine_state_or_image_admission(self):
        del self.proof['build']['artifacts']['image:' + self.reference]
        del self.config['artifacts']['images']
        with (chdir(self.root), patch.object(image_import_cache, 'reload') as reload,
                patch.object(ci, 'identity') as admit, patch.object(godot_ci.subprocess, 'run') as load):
            godot_ci.import_build(self.root, self.config, self.incoming, self.proof)
        reload.assert_not_called()
        admit.assert_not_called()
        load.assert_not_called()
        self.assertEqual(json.loads((self.root / '.artifacts/verification/build.json').read_text()), self.proof['build'])

    def test_shipping_and_prepared_nonshipping_archive_share_the_same_closed_reload(self):
        directory = '.artifacts/fixture-images'
        self.config['components']['fixture_images'] = {'directory': directory}
        self.config['components']['images'] = [{'name': 'image'}, {'name': 'probe'}]
        reference = 'example.invalid/fixture-probe:' + self.source
        identity = 'sha256:' + '3' * 64
        archive = directory + '/probe.oci.tar'
        manifest = directory + '/images.json'
        payload = self.incoming / 'payload'; (payload / directory).mkdir(parents=True)
        (payload / archive).write_bytes(b'prepared diagnostic image archive')
        fixture_hash = hashlib.sha256((payload / archive).read_bytes()).hexdigest()
        (payload / manifest).write_text(json.dumps({'version': 1, 'source_commit': self.source,
            'images': {reference: identity}, 'archive_sha256': fixture_hash}))
        for name in [archive, manifest]:
            self.proof['build']['artifacts'][name] = hashlib.sha256((payload / name).read_bytes()).hexdigest()
            self.proof['executables'][name] = False
        self.proof['build']['artifacts']['image:' + reference] = identity
        definition = {'version': 1, 'project': 'fixture', 'image_base': 'example.invalid/fixture',
            'inputs': {'artifacts': ['.artifacts/release'], 'images': ['example.invalid/fixture-image:{revision}'],
                'environment': [], 'revision_environment': []}, 'tests': ['native']}
        path = self.root / 'deploy/release.toml'; path.parent.mkdir()
        write_source(path, definition)
        subprocess.run(['git', '-C', str(self.root), 'add', 'deploy/release.toml'], check=True)
        self.proof['configuration_sha256'] = godot_ci.configuration_hash(self.root)
        with (chdir(self.root), patch.object(ci, 'identity'),
                patch.object(image_fixtures.images, 'definition', return_value=definition),
                patch.object(image_fixtures,'verify',return_value={reference:identity}),
                patch.object(image_import_cache, 'reload') as reload):
            godot_ci.import_build(self.root, self.config, self.incoming, self.proof)
        self.assertEqual(reload.call_args.args[2], [
            {'path': self.path, 'sha256': self.hash}, {'path': archive, 'sha256': fixture_hash}])
        self.assertEqual(reload.call_args.args[3], {self.reference: self.identity, reference: identity})
        self.assertEqual(reload.call_args.kwargs, {'timeout': 600})

    def test_scope_source_configuration_or_transferred_bytes_refuse_before_reset_or_copy(self):
        for mutation in ['source', 'config', 'scope', 'omitted-images', 'bytes']:
            with self.subTest(mutation=mutation):
                proof = json.loads(json.dumps(self.proof))
                if mutation == 'config': proof['configuration_sha256'] = 'f' * 64
                if mutation == 'scope': proof['build']['artifacts']['image:example.invalid/foreign:' + self.source] = self.identity
                if mutation == 'omitted-images': del proof['build']['artifacts']['image:' + self.reference]
                if mutation == 'bytes': (self.incoming / 'payload' / self.path).write_bytes(b'changed transfer')
                with (chdir(self.root),
                        patch.object(ci, 'identity', side_effect=ValueError('bad source') if mutation == 'source' else None),
                        patch.object(image_import_cache, 'reload') as reset):
                    with self.assertRaises(ValueError): godot_ci.import_build(self.root, self.config, self.incoming, proof)
                reset.assert_not_called()
                self.assertFalse((self.root / '.artifacts').exists())

    def test_cache_failure_cannot_write_accepted_build_receipt_and_retired_selector_is_refused(self):
        with chdir(self.root), patch.object(ci, 'identity'), patch.object(image_import_cache, 'reload', side_effect=ValueError('cache refusal')):
            with self.assertRaisesRegex(ValueError, 'cache refusal'):
                godot_ci.import_build(self.root, self.config, self.incoming, self.proof)
        self.assertFalse((self.root / '.artifacts/verification/build.json').exists())
        for value in [True, False, 'true', 1, {}, []]:
            self.config['ci']['godot']['reset_import_cache'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'unsupported Godot CI'):
                godot_ci.plan(self.root, self.config)


if __name__ == '__main__': unittest.main()
