from source_fixture import SDK_ROOT
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import native_components as native


class NativeGraph(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='native graph ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'Cargo.toml').write_text('[package]\nname="fixture"\nversion="1.0.0"\n')
        self.config = {'components': {'rust': [{'name': 'api', 'manifest': 'Cargo.toml'}, {'name': 'owner', 'manifest': 'Cargo.toml'}], 'native': [
            {'name': 'api', 'role': 'api', 'component': 'api', 'binary': 'api', 'output': '.artifacts/native-api/api_{platform}_{arch}', 'environment': {'SOURCE_COMMIT': '{commit}'}},
            {'name': 'host', 'role': 'host', 'component': 'owner', 'binary': 'host', 'output': '.artifacts/native-host/host_{platform}_{arch}', 'needs': [
                {'component': 'api', 'path_environment': 'API_PAYLOAD', 'compression': 'gzip', 'output': '.artifacts/native-api/payload_{platform}_{arch}.gz'}]},
            {'name': 'cli', 'role': 'cli', 'component': 'owner', 'binary': 'cli', 'output': '.artifacts/native-cli/cli_{platform}_{arch}', 'needs': [
                {'component': 'api', 'path_environment': 'API_PAYLOAD', 'digest_environment': 'API_DIGEST', 'compression': 'gzip', 'output': '.artifacts/native-api/payload_{platform}_{arch}.gz'},
                {'component': 'host', 'platform': 'Linux/x86_64', 'path_environment': 'HOST_AMD64'},
                {'component': 'host', 'platform': 'Linux/arm64', 'path_environment': 'HOST_ARM64'}]}]}}
        self.env = {'COMMIT': 'a' * 40, 'VERSION': 'v1.2.3', 'DATE': 'today', 'CARGO_TARGET_DIR': str(self.root / '.artifacts/target')}
        self.x64 = {'platform': 'Linux', 'arch': 'x86_64', 'target': 'x86_64-unknown-linux-musl'}
        self.arm = {'platform': 'Linux', 'arch': 'arm64', 'target': 'aarch64-unknown-linux-musl'}
        self.commands = []

    def fake_run(self, argv, directory, environment):
        self.commands.append((argv, environment))
        self.assertEqual(argv[:4], ['cargo', 'build', '--locked', '--offline'])
        target = argv[argv.index('--target')+1]
        name = argv[argv.index('--bin')+1]
        output = Path(environment['CARGO_TARGET_DIR']) / target / 'release' / name
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes((name + target).encode())

    def phase_one(self):
        for record in [self.x64, self.arm]:
            native.build(self.root, self.config, record, self.env | {'COOKBOOK_NATIVE_COMPONENTS': '["api","host"]'}, self.fake_run)

    def test_two_wave_graph_requires_both_reviewed_host_inputs_and_binds_hashes(self):
        self.phase_one()
        environment = self.env | {'COOKBOOK_NATIVE_COMPONENTS': '["cli"]'}
        contexts = native.build(self.root, self.config, self.x64, environment, self.fake_run)
        self.assertEqual(len(self.commands), 5)
        values = contexts[0][1]
        self.assertEqual(values['HOST_ARM64'], str(self.root / '.artifacts/native-host/host_Linux_arm64'))
        self.assertTrue(Path(values['API_PAYLOAD']).is_file())
        self.assertEqual(len(values['API_DIGEST']), 64)
        receipt = json.loads((self.root / '.artifacts/native-cli/cli_Linux_x86_64.json').read_text())
        self.assertEqual(set(receipt['dependencies']), {'api-Linux_x86_64', 'host-Linux_x86_64', 'host-Linux_arm64'})
        native.verify(self.root, self.config, self.x64, environment)
        self.assertEqual(native.artifacts(self.root, self.config, self.x64, environment), ['.artifacts/native-cli/cli_Linux_x86_64', '.artifacts/native-cli/cli_Linux_x86_64.json'])

    def test_swapped_external_dependency_or_missing_build_never_invokes_compiler(self):
        environment = self.env | {'COOKBOOK_NATIVE_COMPONENTS': '["cli"]'}
        with self.assertRaises(FileNotFoundError):
            native.build(self.root, self.config, self.x64, environment, self.fake_run)
        self.assertEqual(self.commands, [])
        self.phase_one()
        before = len(self.commands)
        (self.root / '.artifacts/native-api/api_Linux_arm64').write_bytes(b'swapped source API')
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            native.build(self.root, self.config, self.x64, environment, self.fake_run)
        self.assertEqual(len(self.commands), before)

    def test_validation_never_repairs_changed_compressed_outputs(self):
        self.phase_one()
        environment = self.env | {'COOKBOOK_NATIVE_COMPONENTS': '["host"]'}
        compressed = self.root / '.artifacts/native-api/payload_Linux_x86_64.gz'
        compressed.write_bytes(b'changed compressed product')
        with self.assertRaisesRegex(ValueError, 'run make build'):
            native.verify(self.root, self.config, self.x64, environment)
        self.assertEqual(compressed.read_bytes(), b'changed compressed product')

    def test_unknown_selection_cycle_and_path_escape_are_source_errors(self):
        for value in ['[]', '["unknown"]', '["api","api"]', '"api"']:
            with self.assertRaises(ValueError):
                native.selection(self.config, self.env | {'COOKBOOK_NATIVE_COMPONENTS': value})
        self.config['components']['native'][0]['needs'] = [{'component': 'cli', 'path_environment': 'CLI'}]
        with self.assertRaisesRegex(ValueError, 'cycle'):
            native.records(self.config)
        with self.assertRaises(ValueError):
            native.output(self.root, {'output': '../escape'}, 'Linux/x86_64')


if __name__ == '__main__':
    unittest.main()
