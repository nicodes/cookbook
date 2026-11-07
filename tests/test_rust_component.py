from source_fixture import SDK_ROOT
import importlib.util
from pathlib import Path
import sys
import unittest
import tempfile

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
spec = importlib.util.spec_from_file_location('rust_component_controls', SDK_ROOT / 'cookbook/rust_component.py')
cargo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cargo)


class NativeCargoComponent(unittest.TestCase):
    def test_test_keeps_native_harness_compilation_distinct_from_product_build(self):
        declaration = {'manifest': 'crates/service/Cargo.toml', 'binaries': ['service'], 'release': True,
                       'test': {'binaries': [], 'lib': True, 'tests': True, 'features': ['isolated']}}
        unit = cargo.cargo_args(declaration, 'unit')
        build = cargo.cargo_args(declaration, 'build')
        self.assertEqual(unit[:2], ['cargo', 'test'])
        self.assertIn('--locked', unit)
        self.assertNotIn('--release', unit)
        self.assertNotIn('--bin', unit)
        self.assertIn('--lib', unit)
        self.assertIn('--tests', unit)
        self.assertEqual(build[:2], ['cargo', 'build'])
        self.assertIn('--release', build)

    def test_native_selectors_preserve_workspace_features_and_explicit_target(self):
        argv = cargo.cargo_args({'workspace': True, 'features': ['sdk/mock'], 'no_default_features': True},
                               'unit', target='aarch64-apple-darwin')
        self.assertIn('--workspace', argv)
        self.assertIn('--no-default-features', argv)
        self.assertEqual(argv[argv.index('--features') + 1], 'sdk/mock')
        self.assertEqual(argv[argv.index('--target') + 1], 'aarch64-apple-darwin')

    def test_standard_two_part_wasi_target_is_a_real_pinned_sdk_target(self):
        argv = cargo.cargo_args({'manifest': 'fixture/Cargo.toml', 'target': 'wasm32-wasip1'}, 'build')
        self.assertEqual(argv[argv.index('--target') + 1], 'wasm32-wasip1')
        self.assertIn('--offline', argv)
        for value in ['--target', 'wasm32-wasip1;echo', 'wasm32/wasip1']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                cargo.cargo_args({'target': value}, 'build')

    def test_shell_commands_and_flag_like_identifiers_are_not_component_data(self):
        for declaration in [{'commands': {'build': 'anything'}}, {'binaries': ['--offline']},
                            {'features': ['feature;run']}, {'manifest': '../Cargo.toml'},
                            {'test': {'command': 'echo'}}, {'release': 'false'}]:
            with self.subTest(declaration=declaration), self.assertRaises(ValueError):
                cargo.cargo_args(declaration, 'build')

    def test_empty_components_cannot_succeed_as_a_native_test_group(self):
        with self.assertRaises(ValueError):
            cargo.test(Path.cwd(), [], {}, lambda *_args: None)

    def test_lint_checks_format_without_mutation_and_denies_compiler_warnings(self):
        declaration = {'workspace': True}
        self.assertIn('--check', cargo.cargo_args(declaration, 'fmt-check'))
        self.assertNotIn('--check', cargo.cargo_args(declaration, 'fmt'))
        self.assertEqual(cargo.cargo_args(declaration, 'lint')[-3:], ['--', '-D', 'warnings'])
        self.assertIn('--lib', cargo.cargo_args({'lint': {'lib': True, 'binaries': ['owner']}}, 'lint'))

    def test_named_native_test_targets_and_ignored_acceptance_are_explicit(self):
        argv = cargo.cargo_args({'test': {'test_targets': ['runtime', 'providers'],
                                        'test_filter': 'http::tests::postgres_',
                                        'test_arguments': ['--ignored', '--nocapture']}}, 'unit')
        self.assertEqual(argv[argv.index('--test'):], ['--test', 'runtime', '--test', 'providers',
                         'http::tests::postgres_', '--', '--ignored', '--nocapture'])
        with self.assertRaises(ValueError):
            cargo.cargo_args({'test': {'test_arguments': ['--list']}}, 'unit')

    def test_nightly_std_build_is_pin_bound_offline_and_keeps_native_cargo_cwd(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / 'app/rust').mkdir(parents=True)
            (root / 'app/rust/Cargo.toml').write_text('[package]\nname="native"\nversion="0.0.1"\n')
            (root / 'mise.toml').write_text('[tools]\nrust="1.94.0"\n[env]\nRUST_NIGHTLY="nightly-2026-03-06"\n')
            component = {'manifest': 'app/rust/Cargo.toml', 'cwd': 'app/rust', 'toolchain_environment': 'RUST_NIGHTLY',
                         'build_std': True, 'target': 'wasm32-unknown-emscripten', 'target_directory': '.artifacts/web-target', 'timeout': 1200}
            calls = []
            def run(argv, cwd, environment, **options):
                calls.append((argv, cwd, environment, options))
            cargo.build(root, [component], {}, run)
            argv, cwd, environment, options = calls[0]
            self.assertEqual(argv[:5], ['rustup', 'run', 'nightly-2026-03-06', 'cargo', 'build'])
            self.assertIn('-Zbuild-std', argv)
            self.assertIn('--offline', argv)
            self.assertEqual(argv[argv.index('--manifest-path')+1], str(root / 'app/rust/Cargo.toml'))
            self.assertEqual(argv[argv.index('--target-dir')+1], str(root / '.artifacts/web-target'))
            self.assertEqual(cwd, root / 'app/rust')
            self.assertEqual(options, {'timeout': 1200})
            self.assertEqual(environment['RUSTUP_AUTO_INSTALL'], '0')
            with self.assertRaisesRegex(ValueError, 'match the exact'):
                cargo.build(root, [component], {'RUST_NIGHTLY': 'nightly-2026-03-07'}, run)
            self.assertEqual(len(calls), 1)

    def test_only_setup_installs_nightly_rust_sources_without_unneeded_prebuilt_std(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / 'Cargo.toml').write_text('[package]\nname="native"\nversion="0.0.1"\n')
            (root / 'mise.toml').write_text('[tools]\nrust="1.94.0"\n[env]\nRUST_NIGHTLY="nightly-2026-03-06"\n')
            component = {'toolchain_environment': 'RUST_NIGHTLY', 'build_std': True, 'target': 'wasm32-unknown-emscripten', 'timeout': 1200, 'lint': {'enabled': False}}
            calls = []
            cargo.setup(root, [component], {}, lambda argv, *_args: calls.append(argv))
            installs = [argv for argv in calls if argv[:3] == ['rustup', 'toolchain', 'install']]
            self.assertEqual(len(installs), 1)
            self.assertIn('rust-src', installs[0])
            self.assertNotIn('rustfmt', installs[0])
            self.assertNotIn('clippy', installs[0])
            self.assertFalse(any(argv[:3] == ['rustup', 'target', 'add'] for argv in calls))
            self.assertEqual(calls[-1][:5], ['rustup', 'run', 'nightly-2026-03-06', 'cargo', 'fetch'])
            self.assertIn('-Zbuild-std', calls[-1])
            self.assertEqual(cargo.cargo_args(component, 'fmt-check')[:2], ['cargo', 'fmt'])
            with self.assertRaises(ValueError):
                cargo.cargo_args(component, 'build', environment={'RUST_NIGHTLY': 'nightly'})
            with self.assertRaises(ValueError):
                cargo.cargo_args({'build_std': True}, 'build')

    def test_distributable_binary_selection_does_not_drop_library_or_workspace_tests(self):
        component = {'binaries': ['example'], 'workspace': True}
        self.assertIn('--bin', cargo.cargo_args(component, 'build'))
        self.assertNotIn('--bin', cargo.cargo_args(component, 'unit'))
        self.assertNotIn('--bin', cargo.cargo_args(component, 'lint'))

    def test_native_host_coverage_remains_distinct_from_release_musl_compilation(self):
        target = 'x86_64-unknown-linux-musl'
        component = {'binaries': ['example'], 'test': {'native_host': True}, 'lint': {'native_host': True}}
        self.assertNotIn('--target', cargo.cargo_args(component, 'unit', target=target))
        self.assertNotIn('--target', cargo.cargo_args(component, 'lint', target=target))
        self.assertIn('--target', cargo.cargo_args(component, 'build', target=target))
        self.assertIn('--target', cargo.cargo_args({'manifest': 'Cargo.toml'}, 'unit', target=target))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'Cargo.toml').write_text('[package]\nname="native"\nversion="0.0.1"\n')
            (root / 'mise.toml').write_text('[tools]\nrust="1.94.0"\n')
            calls = []
            cargo.setup(root, [component], {'RUST_TARGET': target}, lambda argv, *_args: calls.append(argv))
            fetches = [argv for argv in calls if argv[:2] == ['cargo', 'fetch']]
            self.assertEqual(len(fetches), 1)
            self.assertNotIn('--target', fetches[0])
            self.assertFalse(any('--offline' in argv for argv in fetches))
        with self.assertRaises(ValueError):
            cargo.cargo_args({'build': {'native_host': True}}, 'build')


if __name__ == '__main__':
    unittest.main()
