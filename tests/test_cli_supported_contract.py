from source_fixture import SDK_ROOT
"""Exercise canonical CLI inputs at the real runtime and proof boundaries."""
from copy import deepcopy
from contextlib import chdir
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import native_ci
import rust_cli


class SupportedCLIContract(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='supported CLI ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'Cargo.toml').write_text('[package]\nname="example"\nversion="1.2.3"\n')
        for command in [['init', '-q'], ['add', '.'], ['-c', 'user.name=Contract',
                        '-c', 'user.email=contract@example.invalid', 'commit', '-qm', 'source']]:
            subprocess.run(['git', '-C', str(self.root), *command], check=True)
        self.source = subprocess.check_output(['git', '-C', str(self.root), 'rev-parse', 'HEAD'], text=True).strip()
        system, arch = rust_cli.helper('native_cli').host()
        self.target = dict(platform=system, arch=arch, runner=sorted(native_ci.HOSTED[(system, arch)])[0],
                           target=sorted(native_ci.TARGETS[(system, arch)])[0],
                           extension='zip' if system == 'Windows' else 'tar.gz')
        self.config = {'version': 1, 'recipe': 'rust-cli',
                       'components': {'rust': [{'name': 'cli', 'manifest': 'Cargo.toml', 'binaries': ['example']}]},
                       'package': {'binary': 'example', 'version_arguments': ['--version'],
                                   'version_template': '{binary} {version} ({commit})',
                                   'native_targets': [self.target]}}
        inherited = {name: value for name, value in os.environ.items()
                     if name.upper() in {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'}}
        self.clean_environment = patch.dict(os.environ, inherited, clear=True)
        self.clean_environment.start()
        self.addCleanup(self.clean_environment.stop)

    def graph(self, *names):
        self.config['components']['native'] = [
            dict(name=name, role='api', component='cli', binary='example',
                 output='.artifacts/native-api/' + name + '_{platform}_{arch}') for name in names]

    def environment(self):
        with patch.object(rust_cli, 'selected_target', return_value=self.target):
            return rust_cli.environment(self.root, self.config)

    def test_primary_identity_and_output_are_derived_without_mutating_source_data(self):
        original = deepcopy(self.config)
        package = rust_cli.package_data(self.root, self.config)
        self.assertEqual((package['component'], package['profile'], package['output']),
                         ('cli', 'release', '.artifacts/build/example'))
        self.assertEqual(self.config, original)
        values = self.environment()
        self.assertEqual(values['COMMIT'], self.source)
        self.assertEqual(values['COOKBOOK_CLI_BINARY'], str(rust_cli.binary_path(self.root, '.artifacts/build/example')))
        packet, _scope = native_ci.build_payloads(self.root, self.config, self.target | {'components': []})
        self.assertIn('.artifacts/build/example' + ('.exe' if os.name == 'nt' else ''), packet)
        for name, value in [('component', 'other'), ('profile', 'debug'), ('output', '.artifacts/other'), ('unknown', True)]:
            self.config['package'] = {**original['package'], name: value}
            with self.subTest(name=name), self.assertRaises(ValueError):
                rust_cli.package_data(self.root, self.config)

    def test_root_package_primary_component_and_shipping_profile_fail_closed(self):
        for name, value in [('name', 'other'), ('manifest', 'crates/example/Cargo.toml'),
                            ('profile', 'debug'), ('build', {'profile': 'debug'}), ('build', {'enabled': False}),
                            ('release', False), ('build', {'release': False})]:
            config = deepcopy(self.config)
            config['components']['rust'][0][name] = value
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                rust_cli.package_data(self.root, config)
        (self.root / 'Cargo.toml').write_text('[package]\nname="different"\n')
        with self.assertRaisesRegex(ValueError, 'root Cargo package identity'):
            rust_cli.package_data(self.root, self.config)

    def test_raw_artifact_and_identity_replacements_are_refused(self):
        for name, value in [('paths', []), ('environment', []), ('revision_environment', []),
                            ('binary_environment', {'COMMIT': '.artifacts/other'})]:
            self.config['artifacts'] = {name: value}
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.environment()
            with self.subTest(name=name), self.assertRaises(ValueError):
                rust_cli.verification_scope(self.root, self.config)

    def test_native_names_and_identity_tokens_have_one_non_overridable_binding(self):
        self.graph('api')
        self.config['components']['native'][0]['environment'] = {
            'SOURCE_COMMIT': '{commit}', 'EXAMPLE_BUILD_VERSION': '{version}'}
        values = self.environment()
        self.assertEqual(values['COOKBOOK_NATIVE_API_BINARY'],
                         str(self.root / ('.artifacts/native-api/api_' + self.target['platform'] + '_' + self.target['arch'])))
        self.assertEqual(values['SOURCE_COMMIT'], self.source)
        self.assertEqual(values['EXAMPLE_BUILD_VERSION'], 'dev')
        with patch.object(rust_cli, 'selected_target', return_value=self.target):
            scope = rust_cli.verification_scope(self.root, self.config)
        self.assertTrue({'VERSION', 'COMMIT', 'DATE', 'RUST_TARGET', 'SOURCE_COMMIT',
                         'EXAMPLE_BUILD_VERSION', 'COOKBOOK_NATIVE_COMPONENTS',
                         'COOKBOOK_NATIVE_BUILD_CHECKS'} <= set(scope['environment']))
        self.assertEqual(scope['revision_environment'], ['COMMIT', 'SOURCE_COMMIT'])
        with chdir(self.root), patch.dict(os.environ, values):
            verifier = rust_cli.helper('verification')
            accepted = verifier.identity(scope)
            os.environ['EXAMPLE_BUILD_VERSION'] = 'different-build-input'
            self.assertNotEqual(verifier.identity(scope), accepted)
            with self.assertRaisesRegex(ValueError, 'identity input differs'):
                self.environment()
        for name in ['COMMIT', 'VERSION', 'GITHUB_SHA', 'GH_TOKEN', 'COOKBOOK_NATIVE_COMPONENTS']:
            self.config['components']['native'][0]['environment'] = {name: '{version}'}
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.environment()
        self.graph('api', 'host')
        self.config['components']['native'][0]['environment'] = {'EXAMPLE_BUILD_VERSION': '{version}'}
        self.config['components']['native'][1]['environment'] = {'EXAMPLE_BUILD_VERSION': '{date}'}
        with self.assertRaisesRegex(ValueError, 'cannot conflict'):
            self.environment()
        self.graph('api')
        self.config['components']['native'][0]['environment'] = {'SOURCE_COMMIT': '{version}'}
        with self.assertRaisesRegex(ValueError, 'SOURCE_COMMIT'):
            self.environment()
        self.graph('source-cli', 'source_cli')
        with self.assertRaisesRegex(ValueError, 'binding conflicts'):
            self.environment()
        self.graph('api')
        os.environ['COOKBOOK_NATIVE_API_BINARY'] = '/different/checkout'
        with self.assertRaisesRegex(ValueError, 'binding conflicts'):
            self.environment()

    def test_fixture_bindings_and_wasi_outputs_are_derived_from_native_inputs(self):
        self.config['components']['fixtures'] = [dict(binary='reference', native_targets=[self.target])]
        values = self.environment()
        self.assertEqual(values['COOKBOOK_FIXTURE_REFERENCE_BINARY'],
                         str(self.root / '.artifacts/fixtures/reference' /
                             (self.target['platform'] + '-' + self.target['arch']) /
                             ('reference.exe' if self.target['platform'] == 'Windows' else 'reference')))
        self.config['components']['fixtures'][0]['environment'] = 'COMMIT'
        with self.assertRaises(ValueError):
            self.environment()
        self.config['components'].pop('fixtures')
        manifest = self.root / 'crates/plugin/testdata/probe/Cargo.toml'
        manifest.parent.mkdir(parents=True)
        manifest.write_text('[package]\nname="probe-fixture"\n[lib]\ncrate-type=["cdylib"]\n')
        self.config['components']['rust'].insert(0, dict(name='probe', manifest=manifest.relative_to(self.root).as_posix(),
                                                        target='wasm32-wasip1', test={'enabled': False}))
        self.assertEqual(rust_cli.verification_scope(self.root, self.config)['artifacts'], [
            'dist', '.artifacts/build', '.artifacts/native-package',
            '.artifacts/rust-target/wasm32-wasip1/release/probe_fixture.wasm'])


if __name__ == '__main__':
    unittest.main()
