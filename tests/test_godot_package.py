from source_fixture import SDK_ROOT
"""Prevent native packaging from consuming source trees or stale generated packs."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

TOOLS = SDK_ROOT
sys.path.insert(0, str(TOOLS / 'cookbook'))
import godot_package


class PackageControls(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'app/art').mkdir(parents=True)
        (self.root / 'app/art/prepare.py').write_text('from pathlib import Path\nPath(".artifacts").mkdir(exist_ok=True)\nPath(".artifacts/native-prepared").write_text("prepared")\n')
        (self.root / 'app/art/pack.gd').write_text('extends SceneTree\n')
        (self.root / '.gitignore').write_text('/.artifacts/\n/app/build/\n')
        for arguments in [['init', '-q'], ['add', '.'], ['-c', 'user.name=Controls', '-c', 'user.email=controls@example.invalid', 'commit', '-qm', 'native source']]:
            subprocess.run(['git', '-C', str(self.root), *arguments], check=True)
        self.source = self.root / 'app/build/web'
        self.source.mkdir(parents=True)
        names = ['res://main.scn']
        pack = b'GDPC' + struct.pack('<5IQ', 2, 4, 7, 2, 0, 0) + bytes(64) + struct.pack('<I', len(names))
        for name in names:
            encoded = name.encode() + b'\0'
            pack += struct.pack('<I', len(encoded)) + encoded + bytes(36)
        (self.source / 'index.html').write_text('<script src="index.js"></script>\nconst GODOT_CONFIG = {"executable":"index","fileSizes":{"index.wasm":7,"index.pck":8}};\n')
        for name, data in {'index.js': b'loader', 'index.wasm': b'engine', 'index.pck': pack}.items():
            (self.source / name).write_bytes(data)
        self.config = {'components': {'godot': {
            'exports': [{'preset': 'Web', 'output': 'app/build/web/index.html'}],
            'web_releases': [{'source': 'app/build/web', 'output': 'public', 'versioned_only': True, 'revision_metadata': 'release.json'}]}}}

    def test_native_serving_bytes_are_closed_and_raw_export_inputs_stay_unchanged(self):
        before = {path.name: path.read_bytes() for path in self.source.iterdir()}
        godot_package.validate(self.root, self.config, ['app/build/web', 'public'])
        godot_package.package(self.root, self.config, {'SOURCE_COMMIT': 'a' * 40})
        self.assertEqual({path.name: path.read_bytes() for path in self.source.iterdir()}, before)
        serving = self.root / 'public'
        manifest = json.loads((serving / 'release-manifest.json').read_text())
        for info in manifest['files'].values():
            self.assertEqual(hashlib.sha256((serving / info['path']).read_bytes()).hexdigest(), info['sha256'])
        self.assertEqual(json.loads((serving / 'release.json').read_text()), {'commit': 'a' * 40})
        self.assertFalse((serving / 'index.wasm').exists())

    def test_root_json_metadata_keeps_the_prior_mutable_serving_location(self):
        record=self.config['components']['godot']['web_releases'][0]
        record['root_json']=True
        (self.source/'runtime.json').write_text('{"fixture":true}')
        godot_package.validate(self.root,self.config,['app/build/web','public'])
        godot_package.package(self.root,self.config,{'SOURCE_COMMIT':'a'*40})
        self.assertEqual((self.root/'public/runtime.json').read_text(),'{"fixture":true}')
        manifest=json.loads((self.root/'public/release-manifest.json').read_text())
        self.assertEqual(manifest['files']['runtime.json']['path'],'runtime.json')
        retained=list((self.root/'public/releases').glob('*/runtime.json'))
        self.assertEqual(len(retained),1)
        self.assertEqual(retained[0].read_bytes(),(self.root/'public/runtime.json').read_bytes())

    def test_arbitrary_source_input_or_unverified_serving_tree_is_rejected(self):
        record = self.config['components']['godot']['web_releases'][0]
        record['source'] = 'app'
        with self.assertRaisesRegex(ValueError, 'declared native Web export'):
            godot_package.validate(self.root, self.config, ['public'])
        record['source'] = 'app/build/web'
        with self.assertRaisesRegex(ValueError, 'verified build artifact scope'):
            godot_package.validate(self.root, self.config, ['app/build/web'])
        self.assertFalse((self.root / 'public').exists())

    def test_packaging_refuses_tracked_outputs_before_clobbering_them(self):
        tracked = self.root / 'public/source.gd'
        tracked.parent.mkdir(); tracked.write_text('owned source')
        subprocess.run(['git', '-C', str(self.root), 'add', 'public/source.gd'], check=True)
        with self.assertRaisesRegex(ValueError, 'tracked source'):
            godot_package.validate(self.root, self.config, ['public'])
        self.assertEqual(tracked.read_text(), 'owned source')

    def test_authoring_components_run_only_at_the_explicit_build_phase(self):
        self.config['components']['godot']['prepare'] = [{'runner': 'python', 'path': 'app/art/prepare.py'}]
        godot_package.validate(self.root, self.config, ['public'])
        self.assertFalse((self.root / '.artifacts/native-prepared').exists())
        run = lambda command, root, env, timeout: subprocess.run(command, cwd=root, env=env or None, check=True, timeout=timeout)
        godot_package.author(self.root, self.config, {}, run, 'prepare')
        self.assertEqual((self.root / '.artifacts/native-prepared').read_text(), 'prepared')
        self.config['components']['godot']['prepare'] = [{'runner': 'python', 'path': 'tests/build.py'}]
        (self.root / 'tests').mkdir(); (self.root / 'tests/build.py').write_text('')
        with self.assertRaisesRegex(ValueError, 'owned native components'):
            godot_package.validate(self.root, self.config, ['public'])

    def test_native_extensions_and_digest_evidence_preserve_compiled_bytes(self):
        record = {'source': '.artifacts/compiled/native.wasm',
                  'destination': 'app/extensions/native/bin/native.wasm', 'format': 'wasm',
                  'record': '.artifacts/native-extension.json', 'target': 'web', 'profile': 'release'}
        self.config['components']['godot']['extensions'] = [record]
        source = self.root / record['source']; source.parent.mkdir(parents=True)
        original = b'\0asm\x01\0\0\0'; source.write_bytes(original)
        artifacts = ['public', record['source'], record['destination'], record['record']]
        godot_package.validate(self.root, self.config, artifacts)
        godot_package.extensions(self.root, self.config, {'SOURCE_COMMIT': 'a' * 40})
        self.assertEqual((self.root / record['destination']).read_bytes(), original)
        evidence = json.loads((self.root / record['record']).read_text())
        self.assertEqual(evidence['sha256'], hashlib.sha256(original).hexdigest())
        self.assertEqual(evidence['source_commit'], 'a' * 40)
        source.write_bytes(b'wrong binary format')
        with self.assertRaisesRegex(ValueError, 'wrong compiled binary format'):
            godot_package.extensions(self.root, self.config, {'SOURCE_COMMIT': 'a' * 40})
        self.assertEqual((self.root / record['destination']).read_bytes(), original)
        with self.assertRaisesRegex(ValueError, 'verified artifact scope'):
            godot_package.validate(self.root, self.config, ['public'])

    def test_old_native_pack_cannot_mask_a_generator_that_wrote_nothing(self):
        self.config['components']['godot']['packs'] = [{'program': 'app/art/pack.gd', 'output': 'app/build/web/extra.pck'}]
        output = self.source / 'extra.pck'; output.write_bytes(b'stale prior output')
        original = godot_package.helper
        def selected(name):
            return SimpleNamespace(main=lambda *args, **kwargs: 0) if name == 'godot-game-runner' else original(name)
        with patch.object(godot_package, 'helper', selected), self.assertRaisesRegex(ValueError, 'pack output is missing'):
            godot_package.packs(self.root, self.config, {'GODOT_BIN': 'unused'})
        self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
