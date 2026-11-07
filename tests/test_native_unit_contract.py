"""A real explicit Cargo unit selector owns coverage even with a disabled base."""
from source_fixture import SDK_ROOT, SDK_REF, TOOLS_ROOT
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import orchestration
import bootstrap
import mise_entrypoint
import sdk_environment
import seed_transport


class NativeUnitContract(unittest.TestCase):
    def source(self, **overrides):
        value = {'version': 1, 'recipe': 'rust-cli', 'components': {
            'rust': [{'name': 'cli', 'manifest': 'Cargo.toml', 'test': {'enabled': False}}],
            'rust_tests': [{'name': 'native-owner', 'component': 'cli',
                            'selectors': {'enabled': True, 'lib': True}}]},
            'package': {'native_targets': [{'platform': 'Linux', 'arch': 'x86_64',
                'target': 'x86_64-unknown-linux-gnu', 'runner': 'ubuntu-24.04', 'extension': 'tar.gz'}]}}
        value['components']['rust_tests'][0].update(overrides)
        return value

    def test_explicit_enabled_native_unit_keeps_complete_declared_matrix_and_does_not_mutate_source(self):
        source = self.source()
        before = deepcopy(source)
        value = orchestration.contract(source)
        self.assertEqual(value['route'], 'native')
        self.assertEqual(value['test_groups'], ['lint', 'security', 'unit'])
        self.assertEqual(value['matrix']['requirements'][0]['cargo'], ['native-owner'])
        self.assertEqual(source, before)

    def test_disabled_build_only_unknown_or_missing_native_selector_cannot_supply_real_unit_coverage(self):
        changes = [{'selectors': {'enabled': False, 'lib': True}}, {'suite': 'build'},
            {'selectors': {'lib': True}}, {'component': 'missing'},
            {'selectors': {'enabled': True, 'command': 'false'}},
            {'selectors': {'enabled': 1}}, {'selectors': 'cargo test'}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                orchestration.contract(self.source(**change))
        source = self.source()
        source['components']['rust_tests'] = []
        with self.assertRaisesRegex(ValueError, 'real native unit checks'):
            orchestration.contract(source)

    def test_generated_recipe_bootstrap_cannot_select_the_unit_only_adapter_seam(self):
        with tempfile.TemporaryDirectory(prefix='Unit seam refusal ') as directory:
            root = Path(directory)
            raw = b'version=1\nrecipe="godot-addon"\n'
            (root / 'cookbook.toml').write_bytes(raw)
            (root / 'mise.toml').write_text(mise_entrypoint.render('[tools]\npython="3.13.11"\n',
                SDK_REF, 'godot-addon', raw, ['lint', 'unit', 'security'], []))
            environment = sdk_environment.preserve_mise()
            for name in ['MISE_CONFIG_FILE', 'MISE_OVERRIDE_CONFIG_FILENAMES', 'MISE_ENV']:
                environment.pop(name, None)
            environment.update(COOKBOOK_ROOT=str(SDK_ROOT), TOOLS_ROOT=str(TOOLS_ROOT),
                MISE_TRUSTED_CONFIG_PATHS=str(root), MISE_CEILING_PATHS=str(root.parent),
                MISE_OFFLINE='true', MISE_AUTO_INSTALL='false', MISE_EXEC_AUTO_INSTALL='false')
            source = seed_transport.seed_transport(bootstrap.SEED, encoded=bootstrap.ENCODED_SEED,
                length=bootstrap.SEED_LENGTH, sha256=bootstrap.SEED_SHA256)
            result = subprocess.run([sys.executable, '-I', '-B', '-c', source, SDK_REF,
                'exec', 'tests/adapter_fixture.py', 'build'], cwd=root, env=environment,
                capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('select an explicit Cookbook entrypoint', result.stderr)
            self.assertFalse((root / '.artifacts').exists())


if __name__ == '__main__':
    unittest.main()
