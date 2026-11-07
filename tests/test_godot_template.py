from source_fixture import SDK_ROOT
import copy
import hashlib
import json
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import Mock, patch
sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import godot_template as template

MEMBERS = ['godot.js', 'godot.audio.worklet.js', 'godot.audio.position.worklet.js',
           'godot.html', 'godot.service.worker.js', 'godot.offline.html', 'godot.wasm']


class CustomEngineTemplate(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / 'app/engine').mkdir(parents=True)
        self.profile = self.root / 'app/engine/profile.toml'
        self.data = {'version': 1, 'godot_version': '4.7.2', 'source_commit': 'a'*40, 'source_sha256': 'b'*64,
                     'emsdk_image': 'emscripten/emsdk@sha256:'+'c'*64, 'scons_url': 'https://example.test/scons.whl',
                     'scons_sha256': 'd'*64, 'modules': ['gdscript'],
                     'options': {'platform': 'web', 'target': 'template_release', 'production': 'yes',
                         'optimize': 'size', 'lto': 'full', 'disable_3d': 'yes', 'threads': 'no',
                         'modules_enabled_by_default': 'no'}}
        template._source_config().write(self.profile, self.data)
        self.source = self.root / 'reviewed'; self.source.mkdir()
        self.archive = self.source / 'engine-template.zip'
        with zipfile.ZipFile(self.archive, 'w') as archive:
            for name in MEMBERS: archive.writestr(name, b'\0asmfixture' if name.endswith('.wasm') else b'fixture')
        receipt = template.template_receipt(self.archive, self.profile)
        receipt['build_inputs'] = receipt.pop('requested_build_inputs')
        self.receipt = self.source / 'engine-template.json'; self.receipt.write_text(json.dumps(receipt))
        self.pin = {'repository': 'owner/game', 'tag': 'template-4.7.2.1', 'archive': self.archive.name,
            'receipt': self.receipt.name, 'archive_sha256': receipt['archive_sha256'],
            'receipt_sha256': hashlib.sha256(self.receipt.read_bytes()).hexdigest(), 'profile_sha256': receipt['profile_sha256']}
        self.config = {'components': {'godot': {'templates': {'profile': 'app/engine/profile.toml', 'directory': 'app/.export-templates'}}}}
        self.cache = self.root / 'app/.export-templates'
        self.write_pins()

    def write_pins(self):
        text = '[env]\nGODOT_VERSION="4.7.2"\n'
        text += ''.join('GODOT_TEMPLATE_' + name + '="' + self.pin[name.lower()] + '"\n' for name in template.SDK_KEYS)
        (self.root / 'mise.toml').write_text(text)

    def download(self, destination):
        for name in [self.pin['archive'], self.pin['receipt']]: shutil.copyfile(self.source / name, destination / name)

    def install(self): return template.install(self.cache, self.pin, self.profile, download=self.download)

    def test_exact_seven_components_and_completed_profile_receipt_are_required(self):
        self.install()
        self.assertEqual(template.verify(self.root, self.config, {}), self.cache / self.pin['archive'])
        self.assertTrue((self.cache / '.gdignore').is_file())
        (self.cache / self.pin['archive']).write_bytes(b'foreign engine')
        with self.assertRaisesRegex(ValueError, 'digest'): template.verify(self.root, self.config, {})
        self.profile.write_text('version=2\n')
        with self.assertRaisesRegex(ValueError, 'provenance profile'): template.declaration(self.root, self.config)

    def test_toml_source_and_immutable_historical_profile_identity_are_separate(self):
        receipt = json.loads(self.receipt.read_bytes())
        receipt['profile_sha256'] = 'e' * 64  # Immutable native JSON profile origin, not current TOML bytes.
        self.receipt.write_text(json.dumps(receipt))
        self.pin['receipt_sha256'] = hashlib.sha256(self.receipt.read_bytes()).hexdigest()
        self.write_pins()
        self.install()
        template.verify(self.root, self.config, {})
        for kind in ['request', 'unknown']:
            data = copy.deepcopy(self.data)
            if kind == 'request': data['options']['optimize'] = 'speed'
            else: data['unbound_compiler_flag'] = 'yes'
            template._source_config().write(self.profile, data)
            changed = self.pin | {'profile_sha256': hashlib.sha256(self.profile.read_bytes()).hexdigest()}
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                template.verify_directory(self.cache, changed, self.profile)
        template._source_config().write(self.profile, self.data)
        (self.cache / self.pin['receipt']).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'digest'):
            template.verify_directory(self.cache, self.pin, self.profile)

    def test_offline_build_verification_never_fetches_or_repairs_a_missing_sdk(self):
        command = Mock()
        real_run = template.subprocess.run
        def readonly_source_or_native(arguments, *values, **options):
            if arguments[:1] == ['git'] and '-C' in arguments:
                operation = arguments[arguments.index('-C') + 2:]
                if (operation[:1] in [['rev-parse'], ['status']]
                        or operation == ['config', '--local', '--get-all', 'remote.origin.url']):
                    return real_run(arguments, *values, **options)
            return command(arguments, *values, **options)
        with patch.object(template.subprocess, 'run', side_effect=readonly_source_or_native):
            with self.assertRaisesRegex(ValueError, 'before offline export'): template.verify(self.root, self.config, {})
        command.assert_not_called()
        self.assertFalse(self.cache.exists())

    def test_linked_cache_and_repair_leaf_links_cannot_replace_outside_bytes(self):
        self.cache.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'traverse links'): template.install(self.cache, self.pin, self.profile, offline=True)
        self.cache.unlink(); self.cache.mkdir()
        outside = self.root / 'outside'; outside.write_bytes(b'preserve user bytes')
        (self.cache / self.pin['archive']).symlink_to(outside)
        (self.cache / '.gdignore').symlink_to(outside)
        self.install()
        self.assertEqual(outside.read_bytes(), b'preserve user bytes')
        self.assertFalse((self.cache / self.pin['archive']).is_symlink())
        self.assertFalse((self.cache / '.gdignore').is_symlink())

    def test_receipt_profile_and_component_inventory_must_match_even_under_a_reviewed_hash(self):
        self.install()
        for field in ['build_inputs', 'components', 'profile_sha256']:
            receipt = json.loads(self.receipt.read_text())
            receipt[field] = {} if field != 'profile_sha256' else 'invalid-origin'
            path = self.cache / self.pin['receipt']; path.write_text(json.dumps(receipt))
            pin = self.pin | {'receipt_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'disagree'):
                template.verify_directory(self.cache, pin, self.profile)

    def test_byte_inspection_does_not_claim_build_gameplay_or_performance_acceptance(self):
        receipt = template.template_receipt(self.archive, self.profile)
        self.assertEqual(receipt['gameplay_acceptance'], 'unverified')
        self.assertNotIn('build_inputs', receipt)
        self.assertEqual(len(receipt['components']), 7)
        with zipfile.ZipFile(self.archive, 'a') as archive: archive.writestr('../foreign.js', 'fixture')
        with self.assertRaisesRegex(ValueError, 'unexpected or missing'): template.template_receipt(self.archive, self.profile)

    def test_non_wasm_and_nonregular_engine_components_are_refused(self):
        for kind in ['wasm', 'link']:
            with zipfile.ZipFile(self.archive, 'w') as archive:
                for name in MEMBERS:
                    item = zipfile.ZipInfo(name)
                    item.external_attr = (stat.S_IFLNK if kind == 'link' and name == 'godot.js' else stat.S_IFREG) << 16
                    archive.writestr(item, b'foreign' if name == 'godot.wasm' and kind == 'wasm' else b'\0asmfixture')
            with self.subTest(kind=kind), self.assertRaises(ValueError): template.template_receipt(self.archive, self.profile)

    def test_active_sdk_authority_is_mise_and_native_paths_cannot_escape(self):
        self.assertEqual(template.declaration(self.root, self.config)['pin'], self.pin)
        with self.assertRaisesRegex(ValueError, 'Mise SDK authority'):
            template.verify(self.root, self.config, {'GODOT_TEMPLATE_TAG': 'foreign'})
        for profile, directory in [('../outside.json', 'app/.export-templates'), ('app/engine/profile.toml', '../outside')]:
            changed = copy.deepcopy(self.config); changed['components']['godot']['templates'] = {'profile': profile, 'directory': directory}
            with self.subTest(profile=profile, directory=directory), self.assertRaises(ValueError): template.declaration(self.root, changed)
        self.pin['tag'] = '--foreign'; self.write_pins()
        with self.assertRaises(ValueError): template.declaration(self.root, self.config)


if __name__ == '__main__': unittest.main()
