from source_fixture import SDK_ROOT
"""Transport every derived build output without relaxing native packet identity."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import configuration
import native_ci as native
import test_native_ci


def literal(value):
    if isinstance(value, dict):
        return '{' + ','.join(name + '=' + literal(item) for name, item in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ','.join(literal(item) for item in value) + ']'
    if isinstance(value, bool):
        return str(value).lower()
    return json.dumps(value)


class DerivedNativePacketScope(unittest.TestCase):
    def setUp(self):
        self.fixture = test_native_ci.NativeMatrix()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.config = self.fixture.root, self.fixture.config
        self.config['version'] = 1
        self.config['tests']['unit'] = [{'runner': 'python', 'path': 'tests/boundary.py'}]
        self.config['components']['ui'] = [{'required_files': ['index.html']}]
        self.config['node'] = {'packages': [{'directory': 'app', 'build': 'build'}]}
        (self.root / 'app').mkdir()
        (self.root / 'app/package.json').write_text('{"scripts":{"build":"vite build"}}')
        manifest = self.root / 'crates/probe/Cargo.toml'
        manifest.parent.mkdir(parents=True)
        manifest.write_text('[package]\nname="probe-fixture"\nversion="0.0.1"\n[lib]\ncrate-type=["cdylib"]\n')
        self.config['components']['rust'].insert(0, {'name': 'probe', 'manifest': 'crates/probe/Cargo.toml',
            'target': 'wasm32-wasip1', 'profile': 'release', 'test': {'enabled': False}})
        (self.root / '.gitignore').write_text('/.artifacts/\n/dist/\n')
        environment = patch.dict(os.environ, {'CARGO_TARGET_DIR': str(self.root / '.artifacts/rust-target')})
        environment.start()
        self.addCleanup(environment.stop)
        self.commit_configuration()

    def commit_configuration(self):
        (self.root / 'cookbook.toml').write_text('\n'.join(name + '=' + literal(value)
                                                       for name, value in self.config.items()) + '\n')
        for args in [['add', '.'], ['-c', 'user.name=Native Scope', '-c', 'user.email=scope@example.invalid',
                                   'commit', '-qm', 'owned native source']]:
            subprocess.run(['git', '-C', str(self.root), *args], check=True)
        self.fixture.source = native.ci.git(self.root, 'rev-parse', 'HEAD')
        self.fixture.tree = native.ci.git(self.root, 'rev-parse', 'HEAD^{tree}')
        self.assertEqual(configuration.load(self.root), self.config)

    def exported(self, entry):
        folder = self.root / ('seed-' + entry['id'])
        folder.mkdir()
        proof = self.fixture.packet(entry, folder)
        extras = {
            '.artifacts/ui/index.html': b'current UI html',
            '.artifacts/ui/assets/app.js': b'current UI javascript',
            '.artifacts/rust-target/wasm32-wasip1/release/probe_fixture.wasm': b'current WASI fixture',
        }
        for name, content in extras.items():
            path = folder / 'payload' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            proof['build']['artifacts'][name] = native.digest(path)
        for name in proof['build']['artifacts']:
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(folder / 'payload' / name, destination)
        return native.export(self.root, proof), proof

    def admit(self, directory, entry):
        return native.packet(self.root, directory, self.config, self.fixture.source, self.fixture.tools,
                             entry, self.fixture.environment)

    def test_derived_ui_and_wasi_outputs_round_trip_on_every_declared_native_platform(self):
        for entry in self.fixture.declaration()['requirements']:
            with self.subTest(platform=entry['platform'], arch=entry['arch']):
                directory, proof = self.exported(entry)
                accepted = self.admit(directory, entry)
                self.assertEqual(accepted, proof)
                required, _allowed = native.build_payloads(self.root, self.config, entry)
                executable = '.artifacts/build/example' + ('.exe' if entry['platform'] == 'Windows' else '')
                self.assertIn(executable, required)
                self.assertTrue(required <= set(accepted['build']['artifacts']))
                self.assertEqual((directory / 'payload/.artifacts/ui/index.html').read_bytes(), b'current UI html')

    def test_undeclared_paths_and_other_compiler_outputs_are_refused_even_with_valid_hashes(self):
        entry = self.fixture.declaration()['requirements'][0]
        directory, original = self.exported(entry)
        for name in ['.artifacts/unrelated/private.json', '.artifacts/rust-target/wasm32-wasip1/release/undeclared.wasm']:
            with self.subTest(name=name):
                proof = copy.deepcopy(original)
                path = directory / 'payload' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'correct hash, undeclared output')
                proof['build']['artifacts'][name] = native.digest(path)
                (directory / 'native-proof.json').write_text(json.dumps(proof))
                with self.assertRaisesRegex(ValueError, 'undeclared output'):
                    self.admit(directory, entry)
                path.unlink()

    def test_required_members_source_run_hash_and_native_coverage_guards_remain_mandatory(self):
        entry = self.fixture.declaration()['requirements'][0]
        directory, original = self.exported(entry)
        for mutation in ['source', 'tree', 'sdk', 'configuration', 'attempt', 'coverage', 'required']:
            proof = copy.deepcopy(original)
            if mutation == 'source': proof['source_commit'] = '0' * 40
            elif mutation == 'tree': proof['source_tree'] = '0' * 40
            elif mutation == 'sdk': proof['cookbook_commit'] = '0' * 40
            elif mutation == 'configuration': proof['configuration_sha256'] = '0' * 64
            elif mutation == 'attempt': proof['execution_context']['GITHUB_RUN_ATTEMPT'] = '99'
            elif mutation == 'coverage': proof['tests'].pop('unit')
            else: proof['build']['artifacts'].pop('dist/checksums.txt')
            (directory / 'native-proof.json').write_text(json.dumps(proof))
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.admit(directory, entry)
        (directory / 'native-proof.json').write_text(json.dumps(original))
        (directory / 'payload/.artifacts/ui/index.html').write_bytes(b'changed after build')
        with self.assertRaisesRegex(ValueError, 'accepted build hash'):
            self.admit(directory, entry)

    def test_api_only_wave_cannot_transport_shipping_ui_or_wasi_outputs(self):
        self.config['components']['native'] = [
            {'name': 'api', 'role': 'api', 'component': 'cli', 'binary': 'api',
             'output': '.artifacts/native-api/api_{platform}_{arch}'},
            {'name': 'cli', 'role': 'cli', 'component': 'cli', 'binary': 'example',
             'output': '.artifacts/native-cli/example_{platform}_{arch}',
             'needs': [{'component': 'api', 'platform': 'Linux/x86_64', 'path_environment': 'API_PAYLOAD'}]},
        ]
        self.commit_configuration()
        entry = self.fixture.declaration()['wave1']['include'][0]
        self.assertEqual(entry['components'], ['api'])
        directory, _proof = self.exported(entry)
        with self.assertRaisesRegex(ValueError, 'undeclared output'):
            self.admit(directory, entry)


if __name__ == '__main__':
    unittest.main()
