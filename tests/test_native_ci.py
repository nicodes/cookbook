from source_fixture import SDK_ROOT
import dependencies
import copy
import hashlib
import importlib.util
import json
import os
from types import SimpleNamespace
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import native_ci as native


class NativeMatrix(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='native matrix ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'tests').mkdir()
        (self.root / 'tests/boundary.py').write_text('assert True\n')
        (self.root / 'Cargo.toml').write_text('[package]\nname="example"\nversion="0.0.1"\n')
        (self.root / 'cookbook.toml').write_text('version=1\nrecipe="rust-cli"\n')
        for argv in [['init', '-q'], ['add', '.'], ['-c', 'user.name=Native Matrix', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture']]:
            subprocess.run(['git', '-C', str(self.root), *argv], check=True)
        self.source = native.ci.git(self.root, 'rev-parse', 'HEAD')
        self.tree = native.ci.git(self.root, 'rev-parse', 'HEAD^{tree}')
        self.tools = 'b' * 40
        self.environment = {key: 'value' for key in native.CONTEXT}
        self.environment.update(GITHUB_REPOSITORY='owner/project', GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='2')
        self.context = self.environment | {'GITHUB_JOB': 'native_one'}
        self.targets = [
            {'platform': 'Linux', 'arch': 'x86_64', 'runner': 'ubuntu-24.04', 'target': 'x86_64-unknown-linux-musl', 'extension': 'tar.gz', 'checks': ['tests/boundary.py']},
            {'platform': 'Linux', 'arch': 'arm64', 'runner': 'ubuntu-24.04-arm', 'target': 'aarch64-unknown-linux-musl', 'extension': 'tar.gz', 'checks': ['tests/boundary.py']},
            {'platform': 'Darwin', 'arch': 'x86_64', 'runner': 'macos-15-intel', 'target': 'x86_64-apple-darwin', 'extension': 'tar.gz', 'checks': []},
            {'platform': 'Darwin', 'arch': 'arm64', 'runner': 'macos-26', 'target': 'aarch64-apple-darwin', 'extension': 'tar.gz', 'checks': []},
            {'platform': 'Windows', 'arch': 'x86_64', 'runner': 'windows-2025', 'target': 'x86_64-pc-windows-msvc', 'extension': 'zip', 'checks': []},
            {'platform': 'Windows', 'arch': 'arm64', 'runner': 'windows-11-arm', 'target': 'aarch64-pc-windows-msvc', 'extension': 'zip', 'checks': []},
        ]
        self.config = {'recipe': 'rust-cli', 'components': {'rust': [{'name': 'cli', 'manifest': 'Cargo.toml'}]},
            'package': {'binary': 'example', 'files': [], 'native_targets': self.targets,
                        'version_arguments': ['--version'], 'version_template': '{binary} {version} ({commit})'},
            'tests': {'integration': [{'runner': 'python', 'path': 'tests/boundary.py'}]}}

    def declaration(self):
        return native.plan(self.root, self.config)

    def packet(self, entry, directory):
        required, _allowed = native.build_payloads(self.root, self.config, entry)
        artifacts = {}
        for name in required:
            path = directory / 'payload' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(('exact ' + name).encode())
            artifacts[name] = native.digest(path)
        context = self.environment | {'GITHUB_JOB': 'native_one' if entry['wave'] == 1 else 'native_two'}
        provenance = {'source_commit': self.source, 'source_tree': self.tree, 'tracked_source_clean': True,
                      'input_identity': 'a' * 64, 'execution_context': context}
        proof = {'version': 1, 'coverage': 'native-partial', 'complete_host': False, 'status': 'passed',
            'repository': self.environment['GITHUB_REPOSITORY'], 'source_commit': self.source, 'source_tree': self.tree,
            **dependencies.binding(self.tools), 'configuration_sha256': native.digest(self.root / 'cookbook.toml'),
            'entry': entry, 'execution_context': context, 'version_identity': 'dev', 'date_identity': 'unknown',
            'build': {'identity': 'a' * 64, 'artifacts': artifacts, 'provenance': provenance},
            'tests': {name: provenance | {'tested_commit': self.source} for name in native.expected_receipts(entry)}}
        (directory / 'native-proof.json').write_text(json.dumps(proof))
        return proof

    def jobs(self, entries):
        return {'total_count': len(entries), 'jobs': [{'id': index + 1, 'name': 'Full / ' + entry['job'],
            'status': 'completed', 'conclusion': 'success', 'labels': [entry['runner']]} for index, entry in enumerate(entries)]}

    def test_all_six_platforms_remain_real_native_requirements(self):
        plan = self.declaration()
        self.assertEqual(len(plan['wave1']['include']), 6)
        self.assertEqual(plan['wave2']['include'], [])
        self.assertEqual({value['arch'] for value in plan['requirements'] if value['platform'] == 'Windows'}, {'x86_64', 'arm64'})
        self.assertTrue(all(value['groups'] == ['unit'] for value in plan['requirements']))
        self.assertEqual(native.plan(None, self.config), plan)

    def test_two_waves_preserve_role_dependencies_and_explicit_partial_cargo_checks(self):
        self.config['package']['native_targets'] = self.targets[:4]
        self.config['components']['native'] = [
            {'name': 'api', 'role': 'api', 'component': 'cli', 'binary': 'api', 'output': '.artifacts/native-api/api_{platform}_{arch}'},
            {'name': 'host', 'role': 'host', 'component': 'cli', 'binary': 'host', 'output': '.artifacts/native-host/host_{platform}_{arch}', 'needs': [{'component': 'api', 'path_environment': 'API'}]},
            {'name': 'cli', 'role': 'cli', 'component': 'cli', 'binary': 'example', 'output': '.artifacts/native-cli/example_{platform}_{arch}', 'needs': [
                {'component': 'host', 'platform': 'Linux/x86_64', 'path_environment': 'HOST_AMD64'},
                {'component': 'host', 'platform': 'Linux/arm64', 'path_environment': 'HOST_ARM64'}], 'checks': ['tests/boundary.py']}]
        self.config['components']['rust_tests'] = [
            {'name': 'native-api-unit', 'component': 'cli', 'roles': ['api'], 'selectors': {'lib': True}},
            {'name': 'source-install', 'component': 'cli', 'roles': ['host'], 'platforms': ['Linux/x86_64'], 'suite': 'build', 'selectors': {'test_targets': ['source_build'], 'test_arguments': ['--ignored']}}]
        plan = self.declaration()
        self.assertEqual(len(plan['requirements']), 8)
        self.assertTrue(all(value['components'] == ['api', 'host'] and value['cargo'] == ['native-api-unit'] and not value['groups'] for value in plan['wave1']['include']))
        self.assertTrue(all(value['components'] == ['cli'] and not value['cargo'] and not value['groups'] for value in plan['wave2']['include']))
        self.assertEqual([value['id'] for value in plan['requirements'] if value['build_checks']], ['1-Linux-x86_64'])
        self.assertEqual(native.plan(None, self.config), plan)

    def test_wrong_runner_omitted_boundary_and_duplicate_targets_fail_source_validation(self):
        original = copy.deepcopy(self.config)
        for changed in ['runner', 'checks', 'target', 'duplicate']:
            self.config = copy.deepcopy(original)
            if changed == 'runner':
                self.config['package']['native_targets'][0]['runner'] = 'self-hosted'
            elif changed == 'checks':
                self.config['package']['native_targets'][0]['checks'] = ['tests/not-declared.py']
            elif changed == 'target':
                self.config['package']['native_targets'][0]['target'] = 'aarch64-unknown-linux-musl'
            else:
                self.config['package']['native_targets'].append(self.targets[0])
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.declaration()

    def test_native_driver_uses_explicit_make_primitives_and_exports_only_after_success(self):
        self.config['components']['rust_tests'] = [{'name': 'native-library', 'component': 'cli', 'selectors': {'lib': True}}]
        entry = self.declaration()['requirements'][0]
        calls = []
        def execute(argv, **options):
            calls.append((argv, dict(options['env'])))
            return subprocess.CompletedProcess(argv, 0)
        with patch.dict(os.environ, self.environment, clear=False), patch.object(native, 'load', return_value=self.config), patch.object(native.ci, 'identity', return_value=self.tree), patch.object(native, 'helper', return_value=SimpleNamespace(host=lambda: ('Linux', 'x86_64'))), patch.object(native.recipe, 'configure_environment'), patch.object(native.subprocess, 'run', side_effect=execute), patch.object(native, 'collect', return_value={'native': 'proof'}) as collect, patch.object(native, 'export') as export:
            native.run_entry(self.root, self.source, self.tools, entry['id'])
            self.assertEqual([argv[-1] for argv, _env in calls], ['setup', 'build', 'test:unit', 'test:integration'])
            self.assertEqual(json.loads(calls[2][1]['COOKBOOK_NATIVE_TESTS']), ['native-library'])
            self.assertNotIn('COOKBOOK_TEST_PATHS', calls[2][1])
            self.assertEqual(json.loads(calls[3][1]['COOKBOOK_TEST_PATHS']), ['tests/boundary.py'])
            self.assertNotIn('COOKBOOK_NATIVE_TESTS', calls[3][1])
            collect.assert_called_once()
            export.assert_called_once_with(self.root, {'native': 'proof'})
        def failed(argv, **options):
            if argv[-1] == 'test:integration':
                raise subprocess.CalledProcessError(1, argv)
            return subprocess.CompletedProcess(argv, 0)
        with patch.dict(os.environ, self.environment, clear=False), patch.object(native, 'load', return_value=self.config), patch.object(native.ci, 'identity', return_value=self.tree), patch.object(native, 'helper', return_value=SimpleNamespace(host=lambda: ('Linux', 'x86_64'))), patch.object(native.recipe, 'configure_environment'), patch.object(native.subprocess, 'run', side_effect=failed), patch.object(native, 'collect') as collect, patch.object(native, 'export') as export:
            with self.assertRaises(subprocess.CalledProcessError):
                native.run_entry(self.root, self.source, self.tools, entry['id'])
            collect.assert_not_called()
            export.assert_not_called()

    def test_packet_checks_source_execution_tests_and_exact_payload_hashes(self):
        entry = self.declaration()['requirements'][0]
        directory = self.root / 'packet';directory.mkdir()
        proof = self.packet(entry, directory)
        self.assertEqual(native.packet(self.root, directory, self.config, self.source, self.tools, entry, self.environment), proof)
        for change in ['source', 'attempt', 'omitted-check', 'complete-claim', 'not-tested']:
            altered = copy.deepcopy(proof)
            if change == 'source': altered['source_commit'] = '0' * 40
            if change == 'attempt': altered['execution_context']['GITHUB_RUN_ATTEMPT'] = '1'
            if change == 'omitted-check': altered['tests'].pop(native.slug('boundary', 'tests/boundary.py'))
            if change == 'complete-claim': altered['coverage'] = 'complete-host'
            if change == 'not-tested': altered['tests']['unit']['tested_commit'] = None
            (directory / 'native-proof.json').write_text(json.dumps(altered))
            with self.subTest(change=change), self.assertRaises(ValueError):
                native.packet(self.root, directory, self.config, self.source, self.tools, entry, self.environment)
        (directory / 'native-proof.json').write_text(json.dumps(proof))
        first = next(iter(proof['build']['artifacts']))
        (directory / 'payload' / first).write_bytes(b'swapped artifact')
        with self.assertRaisesRegex(ValueError, 'accepted build hash'):
            native.packet(self.root, directory, self.config, self.source, self.tools, entry, self.environment)

    def test_only_complete_current_attempt_matrix_can_join_complete_host_proof(self):
        declaration = self.declaration()
        entries = declaration['requirements']
        self.assertEqual(len(native.validate_jobs(entries, self.jobs(entries))), 6)
        for change in ['missing', 'skipped', 'wrong-runner', 'truncated']:
            jobs = self.jobs(entries)
            if change == 'missing': jobs['jobs'].pop();jobs['total_count'] -= 1
            if change == 'skipped': jobs['jobs'][0]['conclusion'] = 'skipped'
            if change == 'wrong-runner': jobs['jobs'][0]['labels'] = ['windows-2025']
            if change == 'truncated': jobs['total_count'] += 1
            with self.subTest(change=change), self.assertRaises(ValueError):
                native.validate_jobs(entries, jobs)
        matrix = {'source_commit': self.source, **dependencies.binding(self.tools), 'host_groups': declaration['host_groups']}
        host = {'status': 'passed', 'build_verified': True, 'tests_verified': True, 'source_commit': self.source,
                **dependencies.binding(self.tools), 'test_groups': declaration['host_groups']}
        self.assertEqual(native.finalize_host(host, matrix)['coverage'], 'complete-host-and-native-matrix')
        for missing in ['tests_verified', 'build_verified', 'test_groups']:
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                native.finalize_host({key: value for key, value in host.items() if key != missing}, matrix)

    def test_import_requires_exact_artifact_set_and_restores_only_declared_native_inputs(self):
        entries = self.declaration()['requirements'][:1]
        directory = self.root / 'downloads';directory.mkdir()
        with self.assertRaisesRegex(ValueError, 'matrix is incomplete'):
            native.import_packets(self.root, self.config, self.source, self.tools, entries, directory, self.environment)
        folder = directory / native.artifact_name(self.source, entries[0], self.environment);folder.mkdir()
        self.packet(entries[0], folder)
        native.import_packets(self.root, self.config, self.source, self.tools, entries, directory, self.environment)
        restored = self.root / '.artifacts/native-archives'
        self.assertEqual(len(list(restored.iterdir())), 2)
        self.assertFalse((self.root / 'dist').exists())
        self.assertFalse((self.root / '.artifacts/build').exists())
        (directory / 'unexpected').mkdir()
        with self.assertRaisesRegex(ValueError, 'matrix is incomplete'):
            native.import_packets(self.root, self.config, self.source, self.tools, entries, directory, self.environment)

    def test_controller_rederives_requirements_instead_of_trusting_transport_metadata(self):
        declaration = self.declaration()
        entries = declaration['requirements']
        proofs = []
        for entry in entries:
            folder = self.root / ('packet-' + entry['id']);folder.mkdir()
            proofs.append(self.packet(entry, folder))
        matrix = {'version': 1, 'requirements': entries, 'host_groups': declaration['host_groups'],
                  'source_commit': self.source, **dependencies.binding(self.tools), 'proofs': proofs,
                  'jobs': native.validate_jobs(entries, self.jobs(entries))}
        args = (self.config, self.jobs(entries), self.source, self.tools, self.environment, native.digest(self.root / 'cookbook.toml'))
        self.assertEqual(len(native.validate_acceptance(matrix, *args)), 6)
        changed = copy.deepcopy(matrix)
        changed['requirements'].pop()
        with self.assertRaisesRegex(ValueError, 'candidate source contract'):
            native.validate_acceptance(changed, *args)
        changed = copy.deepcopy(matrix)
        changed['proofs'].pop()
        with self.assertRaisesRegex(ValueError, 'omitted or repeated'):
            native.validate_acceptance(changed, *args)


if __name__ == '__main__':
    unittest.main()
