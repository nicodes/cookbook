from source_fixture import SDK_ROOT
"""Static delivery always binds its recipe-owned blueprint and release identity."""
from contextlib import chdir
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import godot_game as game
import godot_static as static
from source_fixture import read_source, write_source, dump_source


class StaticArtifactScope(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        files = {'app/project.godot': 'config_version=5\n',
            'deploy/config.toml': 'compose = "compose.yml"\n',
            'deploy/compose.yml': 'services: {}\n',
            '.gitignore': '.artifacts/\napp/build/\npublic/\n'}
        release = {'version': 1, 'project': 'game', 'image_base': 'ghcr.io/fixture/game',
            'inputs': {'artifacts': ['public'], 'images': [], 'environment': [], 'revision_environment': []},
            'tests': ['native']}
        delivery = {'mode': 'godot-static', 'komizo_ref': 'a' * 40, 'project': 'game', 'static': {
            'config': 'deploy/config.toml', 'compose': 'deploy/compose.yml',
            'release_manifest': 'public/release-manifest.json',
            'record': False, 'recovery': False, 'production': False}}
        files['deploy/release.toml'] = dump_source(release)
        files['deploy/delivery.toml'] = dump_source(delivery)
        for name, body in files.items():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Fixture', '-c',
            'user.email=fixture@example.test', 'commit', '-qm', 'static source fixture'], check=True)
        self.config = {'recipe': 'godot-game',
            'components': {'images': [{'name': 'gate'}], 'godot': {'exports': [
                {'preset': 'Web', 'output': 'app/build/web/index.html'}]}},
            'artifacts': {},
            'delivery': {'definition': 'deploy/delivery.toml'}}
        self.outputs = {'public/release-manifest.json': '{"release_id":"fixture"}\n',
            'app/build/.gdignore': '', static.CONFIG_ARTIFACT: '{"blueprint":"fixture"}\n',
            static.MANIFEST_ARTIFACT: '{"source_commit":"fixture"}\n'}
        for name, body in self.outputs.items():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)

    def accept(self):
        verification = game.helper('verification')
        scope = game.verification_scope(self.root, self.config)
        path = self.root / '.artifacts/verification/build.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'identity': verification.identity(scope),
            'artifacts': verification.artifacts(scope)}))
        game.current_build(self.root, self.config, verification)
        return verification

    def test_omitted_metadata_has_exactly_the_old_explicit_scope_and_artifact_hashes(self):
        explicit = copy.deepcopy(self.config)
        explicit['artifacts']['release_metadata'] = [static.CONFIG_ARTIFACT, static.MANIFEST_ARTIFACT]
        with chdir(self.root):
            before = game.verification_scope(self.root, explicit)
            after = game.verification_scope(self.root, self.config)
            self.assertEqual(after, before)
            self.assertEqual(after['artifacts'], ['public', 'app/build/.gdignore',
                static.CONFIG_ARTIFACT, static.MANIFEST_ARTIFACT])
            verification = game.helper('verification')
            self.assertEqual(verification.artifacts(after), verification.artifacts(before))
            self.assertEqual(verification.identity(after), verification.identity(before))

    def test_missing_blueprint_or_release_identity_cannot_be_accepted(self):
        with chdir(self.root):
            verification = self.accept()
            for name in [static.CONFIG_ARTIFACT, static.MANIFEST_ARTIFACT]:
                with self.subTest(name=name):
                    path = self.root / name
                    path.unlink()
                    try:
                        with self.assertRaisesRegex(ValueError, 'artifact is missing'):
                            game.current_build(self.root, self.config, verification)
                    finally:
                        path.write_text(self.outputs[name])

    def test_changed_blueprint_or_release_identity_invalidates_the_build_receipt(self):
        with chdir(self.root):
            verification = self.accept()
            for name in [static.CONFIG_ARTIFACT, static.MANIFEST_ARTIFACT]:
                with self.subTest(name=name):
                    path = self.root / name
                    path.write_text('{"tampered":true}\n')
                    try:
                        with self.assertRaisesRegex(ValueError, 'changed or stale'):
                            game.current_build(self.root, self.config, verification)
                    finally:
                        path.write_text(self.outputs[name])

    def test_nonstatic_delivery_does_not_gain_static_artifacts(self):
        path = self.root / 'deploy/delivery.toml'
        write_source(path, {'mode': 'application-images'})
        with chdir(self.root):
            scope = game.verification_scope(self.root, self.config)
        self.assertEqual(scope['artifacts'], ['public', 'app/build/.gdignore'])

    def test_existing_metadata_keeps_its_order_without_duplicate_derived_names(self):
        self.config['artifacts']['release_metadata'] = [static.MANIFEST_ARTIFACT, 'public/extra.json']
        with chdir(self.root):
            scope = game.verification_scope(self.root, self.config)
        self.assertEqual(scope['artifacts'], ['public', 'app/build/.gdignore',
            static.MANIFEST_ARTIFACT, 'public/extra.json', static.CONFIG_ARTIFACT])
        for name in [static.CONFIG_ARTIFACT, static.MANIFEST_ARTIFACT]:
            self.assertEqual(scope['artifacts'].count(name), 1)


if __name__ == '__main__':
    unittest.main()
