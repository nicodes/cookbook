from source_fixture import SDK_ROOT
"""Real native producer packets retain only their configured browser report root."""
from contextlib import chdir
import dependencies
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

TOOLS = SDK_ROOT
sys.path.insert(0, str(TOOLS / 'cookbook'))
import godot_browser_evidence as evidence
import godot_ci as ci


class BrowserReportControls(unittest.TestCase):
    tools = 'b' * 40
    environment = {'GITHUB_REPOSITORY': 'fixture/browser', 'GITHUB_RUN_ID': '42', 'GITHUB_RUN_ATTEMPT': '2',
        'GITHUB_WORKFLOW_REF': 'fixture/browser/.github/workflows/ci.yml@refs/pull/3/merge',
        'GITHUB_WORKFLOW_SHA': 'e' * 40}

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'tests/browser').mkdir(parents=True)
        (self.root / 'cookbook.toml').write_text('''version=1
recipe="godot-game"
[components.browser]
reports="release-reports/browser"
runner="tests/browser/driver.mjs"
[artifacts]
paths=["public"]
[ci.godot]
unit_shards=0
gates=[{name="browser",runner="ubuntu-24.04",timeout_minutes=25,suite="e2e",tests=["test-browser"],setup="browser"},{name="ordinary",runner="ubuntu-24.04",timeout_minutes=25,suite="e2e",tests=["test-ordinary"],setup="browser"}]
[[tests.e2e]]
name="test-browser"
runner="godot-browser"
path="tests/browser/driver.mjs"
timeout=180
[[tests.e2e]]
name="test-ordinary"
runner="godot-browser"
path="tests/browser/ordinary.mjs"
timeout=180
''')
        for program, body in [('driver', '''mkdirSync('release-reports/browser/123',{recursive:true});
writeFileSync('release-reports/browser/123/result.json',JSON.stringify({source:process.env.SOURCE_COMMIT,browser:'chromium',ok:true}));
writeFileSync('release-reports/browser/123/empty.log','');
writeFileSync('release-reports/browser/gate.log','driver native log');'''),
                ('ordinary', "writeFileSync('release-reports/browser/gate.log','ordinary native log');")]:
            (self.root / f'tests/browser/{program}.mjs').write_text("import {mkdirSync,writeFileSync} from 'node:fs';\n"
                "mkdirSync('release-reports/browser',{recursive:true});\n"
                "mkdirSync('release-reports/other',{recursive:true});\n"
                "writeFileSync('release-reports/other/excluded.json','{}');\n" + body)
        for args in [['init', '-q'], ['add', '.'], ['-c', 'user.name=Fixture', '-c',
                'user.email=fixture@example.invalid', 'commit', '-qm', 'Native report source fixture']]:
            subprocess.run(['git', '-C', str(self.root), *args], check=True)
        self.source = subprocess.check_output(['git', '-C', str(self.root), 'rev-parse', 'HEAD'], text=True).strip()
        self.tree = subprocess.check_output(['git', '-C', str(self.root), 'rev-parse', 'HEAD^{tree}'], text=True).strip()
        self.config = tomllib.loads((self.root / 'cookbook.toml').read_text())
        self.entries = [row for row in ci.plan(self.root, self.config)['requirements'] if row['kind'] != 'host']
        self.canonical = ci.configuration_hash(self.root)
        (self.root / 'public').mkdir(); (self.root / 'public/index.html').write_bytes(b'accepted fixture build')
        self.build = {'identity': 'f' * 64,
            'artifacts': {'public/index.html': hashlib.sha256(b'accepted fixture build').hexdigest()},
            'provenance': {'source_commit': self.source, 'source_tree': self.tree, 'tracked_source_clean': True,
                'input_identity': 'f' * 64, 'execution_context': {**self.environment, 'GITHUB_JOB': 'build'}}}
        self.downloads = self.root / 'incoming'; self.downloads.mkdir()

    def proof(self, entry):
        context = {**self.environment, 'GITHUB_JOB': ci.execution_job(entry)}
        return {'version': 1, 'coverage': 'godot-partial', 'complete_host': False, 'status': 'passed',
            'repository': self.environment['GITHUB_REPOSITORY'], 'source_commit': self.source,
            'source_tree': self.tree, **dependencies.binding(self.tools), 'configuration_sha256': self.canonical,
            'entry': entry, 'execution_context': context, 'executed_cases': entry.get('cases', []),
            'build': copy.deepcopy(self.build), 'worker_identity': '1' * 64,
            'receipt': {**context, 'tested_commit': self.source, 'source_commit': self.source,
                'source_tree': self.tree, 'tracked_source_clean': True, 'input_identity': '1' * 64,
                'execution_context': context}}

    def produce(self, entry):
        script = 'driver' if entry['id'] == 'gate-browser' else 'ordinary'
        subprocess.run(['node', f'tests/browser/{script}.mjs'], cwd=self.root,
            env={**os.environ, 'SOURCE_COMMIT': self.source, 'MISE_AUTO_INSTALL': 'false',
                 'MISE_EXEC_AUTO_INSTALL': 'false'}, check=True, timeout=30)

    def export(self, entry):
        proof = self.proof(entry)
        if entry['kind'] != 'build':
            self.produce(entry)
            # Keep the established source/build receipt boundary isolated while
            # exercising the actual producer -> collect -> packet -> host path.
            runtime = importlib.import_module('ci')
            receipt = self.root / '.artifacts/verification' / (entry['suite'] + '.json')
            receipt.parent.mkdir(parents=True, exist_ok=True); receipt.write_text(json.dumps(proof['receipt']))
            verifier = Mock()
            verifier.execution_context.return_value = proof['execution_context']
            verifier.identity.return_value = proof['worker_identity']
            with (patch.object(runtime, 'identity', return_value=self.tree),
                    patch.object(runtime, 'verification', return_value=verifier),
                    patch.dict(os.environ, proof['execution_context'])):
                proof = ci.collect_partial(self.root, self.config, self.source, self.tools, entry, self.build)
        stage = ci.export_packet(self.root, proof, self.config)
        destination = self.downloads / ci.artifact_name(self.source, entry, self.environment)
        shutil.copytree(stage, destination)
        if (self.root / 'release-reports').exists(): shutil.rmtree(self.root / 'release-reports')
        return proof, destination

    def matrix_packets(self):
        packets = [self.export(entry) for entry in self.entries]
        jobs = {'total_count': len(self.entries), 'jobs': [{'id': index + 1, 'name': 'Full / ' + entry['job'],
            'status': 'completed', 'conclusion': 'success', 'labels': [entry['runner']]}
            for index, entry in enumerate(self.entries)]}
        return packets, jobs

    def test_actual_native_producers_packet_host_join_and_final_evidence_keep_original_bytes(self):
        import godot_game
        packets, jobs = self.matrix_packets()
        self.assertFalse((self.root / 'release-reports').exists())
        with chdir(self.root), patch.dict(os.environ, self.environment):
            matrix = ci.prepare_host(self.root, self.config, self.source, self.tools, directory=self.downloads, jobs=jobs)
            ci.host_matrix(self.root, self.config, os.environ)
        reports = self.root / 'release-reports/browser'
        self.assertEqual((reports / 'gate-browser/gate.log').read_text(), 'driver native log')
        self.assertEqual((reports / 'gate-ordinary/gate.log').read_text(), 'ordinary native log')
        self.assertEqual((reports / 'gate-browser/123/empty.log').read_bytes(), b'')
        self.assertEqual(json.loads((reports / 'gate-browser/123/result.json').read_text())['source'], self.source)
        self.assertFalse((self.root / 'release-reports/other').exists())
        self.assertIn('release-reports/browser/gate-browser/123/result.json', godot_game.evidence_paths(self.root, self.config))
        # A later legitimate host producer can write outside imported namespaces.
        (reports / 'host-native.json').write_text('{}')
        evidence.verify_host(self.root, self.config, matrix['proofs'])
        for proof, directory in packets:
            ci.packet(self.root, directory, self.config, self.source, self.tools, proof['entry'], self.environment)

    def test_required_missing_empty_or_all_zero_reports_refuse_without_placeholder(self):
        entry = next(row for row in self.entries if row['id'] == 'gate-browser')
        for mode in ['missing', 'directory', 'empty-file']:
            with self.subTest(mode=mode):
                reports = self.root / 'release-reports/browser'
                if reports.exists(): shutil.rmtree(reports)
                if mode != 'missing': reports.mkdir(parents=True)
                if mode == 'empty-file': (reports / 'empty.log').touch()
                with self.assertRaisesRegex(ValueError, 'nonempty reports'):
                    evidence.capture(self.root, self.config, self.proof(entry))

    def test_old_proof_only_packet_cannot_impersonate_required_browser_evidence(self):
        entry = next(row for row in self.entries if row['id'] == 'gate-browser')
        directory = self.root / 'old-packet'; directory.mkdir()
        (directory / 'godot-proof.json').write_text(json.dumps(self.proof(entry)))
        with self.assertRaisesRegex(ValueError, 'omitted its evidence'):
            ci.packet(self.root, directory, self.config, self.source, self.tools, entry, self.environment)
        self.assertFalse((self.root / 'release-reports').exists())

    def test_producer_report_changes_before_export_refuse(self):
        entry = next(row for row in self.entries if row['id'] == 'gate-browser')
        self.produce(entry)
        proof = self.proof(entry); evidence.capture(self.root, self.config, proof)
        (self.root / 'release-reports/browser/123/result.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'changed before worker packet export'):
            ci.export_packet(self.root, proof, self.config)
        self.assertFalse((self.root / '.artifacts/godot-export/godot-proof.json').exists())

    def test_undeclared_and_nonproducer_no_report_behavior_stays_absent(self):
        entry = next(row for row in self.entries if row['id'] == 'gate-ordinary')
        proof = self.proof(entry); evidence.capture(self.root, self.config, proof)
        self.assertNotIn('browser_evidence', proof)
        config = copy.deepcopy(self.config); del config['components']['browser']
        evidence.capture(self.root, config, proof)
        self.assertNotIn('browser_evidence', proof)
        config['components']['browser'] = {}
        evidence.capture(self.root, config, proof)
        self.assertIsNone(evidence.validate(config, proof))
        self.assertNotIn('browser_evidence', proof)

    def test_gate_log_only_cannot_replace_required_json_at_capture_or_manifest_admission(self):
        entry = next(row for row in self.entries if row['id'] == 'gate-browser')
        self.produce(entry)
        proof = self.proof(entry); evidence.capture(self.root, self.config, proof)
        proof['browser_evidence']['files'] = {'gate.log': proof['browser_evidence']['files']['gate.log']}
        with self.assertRaisesRegex(ValueError, 'nonempty JSON'):
            evidence.validate(self.config, proof)
        (self.root / 'release-reports/browser/123/result.json').unlink()
        with self.assertRaisesRegex(ValueError, 'nonempty JSON'):
            evidence.capture(self.root, self.config, self.proof(entry))

    def test_stale_source_run_attempt_entry_and_root_refuse_before_host_copy(self):
        entry = next(row for row in self.entries if row['id'] == 'gate-browser')
        proof, directory = self.export(entry)
        for key, value in [('source_commit', '0' * 40), ('source_tree', '0' * 40), ('cookbook_commit', '0' * 40),
                ('configuration_sha256', '0' * 64), ('entry', 'gate-ordinary'), ('root', 'release-reports')]:
            bad = copy.deepcopy(proof); bad['browser_evidence'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): evidence.validate(self.config, bad)
        for key in ['GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_JOB']:
            bad = copy.deepcopy(proof); bad['browser_evidence']['execution_context'][key] = 'stale'
            with self.subTest(key=key), self.assertRaises(ValueError): evidence.validate(self.config, bad)
        self.assertFalse((self.root / 'release-reports').exists())

    def test_changed_missing_extra_linked_or_hardlinked_packet_reports_refuse(self):
        entry = next(row for row in self.entries if row['id'] == 'gate-browser')
        proof, directory = self.export(entry)
        original = directory / 'browser-evidence/123/result.json'; content = original.read_bytes()
        for mode in ['changed', 'missing', 'extra', 'symlink', 'hardlink']:
            with self.subTest(mode=mode):
                if mode == 'changed': original.write_bytes(b'changed')
                elif mode == 'missing': original.unlink()
                elif mode == 'extra': (original.parent / 'extra.json').write_text('{}')
                elif mode == 'symlink': original.unlink(); original.symlink_to(self.root / 'cookbook.toml')
                else: original.unlink(); os.link(self.root / 'cookbook.toml', original)
                with self.assertRaises(ValueError): evidence.packet(self.config, proof, directory)
                if original.exists() or original.is_symlink(): original.unlink()
                original.write_bytes(content)
                (original.parent / 'extra.json').unlink(missing_ok=True)

    def test_traversal_unknown_metadata_and_duplicate_worker_destinations_refuse(self):
        entry = next(row for row in self.entries if row['id'] == 'gate-browser')
        proof, directory = self.export(entry)
        for name in ['../foreign.json', '/foreign.json', '.git/config', 'a/../b', 'a//b', 'x\\y']:
            bad = copy.deepcopy(proof); bad['browser_evidence']['files'][name] = {'sha256': '1' * 64, 'size': 1}
            with self.subTest(name=name), self.assertRaises(ValueError): evidence.validate(self.config, bad)
        bad = copy.deepcopy(proof); bad['browser_evidence']['extra'] = True
        with self.assertRaises(ValueError): evidence.validate(self.config, bad)
        with self.assertRaisesRegex(ValueError, 'collide'):
            evidence.join_plan(self.root, self.config, [proof, proof], self.downloads,
                lambda value: ci.artifact_name(self.source, value['entry'], self.environment))

    def test_existing_host_data_and_post_join_tampering_refuse(self):
        packets, jobs = self.matrix_packets()
        reports = self.root / 'release-reports/browser'; reports.mkdir(parents=True)
        (reports / 'old.json').write_text('{}')
        with chdir(self.root), patch.dict(os.environ, self.environment), self.assertRaisesRegex(ValueError, 'collides'):
            ci.prepare_host(self.root, self.config, self.source, self.tools, directory=self.downloads, jobs=jobs)
        self.assertFalse(ci.matrix_path(self.root).exists())
        shutil.rmtree(reports)
        with chdir(self.root), patch.dict(os.environ, self.environment):
            ci.prepare_host(self.root, self.config, self.source, self.tools, directory=self.downloads, jobs=jobs)
            (reports / 'gate-browser/123/result.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'joined browser reports'):
                ci.host_matrix(self.root, self.config, os.environ)

    def test_final_aggregate_refuses_post_last_join_json_change_or_missing_extra_hash(self):
        import godot_game
        packets, jobs = self.matrix_packets()
        with chdir(self.root), patch.dict(os.environ, self.environment):
            matrix = ci.prepare_host(self.root, self.config, self.source, self.tools, directory=self.downloads, jobs=jobs)
            ci.host_matrix(self.root, self.config, os.environ)
        def aggregate():
            return {'status': 'passed', 'build_verified': True, 'tests_verified': True,
                'test_groups': matrix['host_groups'], 'source_commit': self.source, **dependencies.binding(self.tools),
                'evidence': {filename: hashlib.sha256((self.root / filename).read_bytes()).hexdigest()
                    for filename in godot_game.evidence_paths(self.root, self.config)}}
        host = aggregate()
        ci.finalize_host(host, matrix)
        ci.validate_acceptance(matrix, self.config, jobs, self.source, self.tools, self.environment,
            self.canonical, root=self.root, host=host)
        # This is after the final host-matrix check. The ordinary final proof
        # reader sees the newly changed bytes; the cached worker manifest does not.
        target = self.root / 'release-reports/browser/gate-browser/123/result.json'
        target.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'final host browser evidence'):
            ci.finalize_host(aggregate(), matrix)
        with self.assertRaisesRegex(ValueError, 'final host browser evidence'):
            ci.validate_acceptance(matrix, self.config, jobs, self.source, self.tools, self.environment,
                self.canonical, root=self.root, host=aggregate())
        for mode in ['missing', 'extra']:
            bad = copy.deepcopy(host)
            if mode == 'missing': bad['evidence'].clear()
            else: bad['evidence']['release-reports/browser/gate-browser/foreign.json'] = '1' * 64
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, 'final host browser evidence'):
                ci.finalize_host(bad, matrix)

    def test_tracked_or_linked_source_report_roots_refuse(self):
        entry = next(row for row in self.entries if row['id'] == 'gate-browser')
        self.produce(entry)
        report = self.root / 'release-reports/browser/123/result.json'
        subprocess.run(['git', '-C', str(self.root), 'add', str(report)], check=True)
        with self.assertRaisesRegex(ValueError, 'tracked source'):
            evidence.capture(self.root, self.config, self.proof(entry))
        subprocess.run(['git', '-C', str(self.root), 'reset', '-q'], check=True)
        directory = report.parents[1]; shutil.rmtree(directory); directory.symlink_to(self.root / 'tests')
        with self.assertRaises(ValueError): evidence.capture(self.root, self.config, self.proof(entry))


if __name__ == '__main__': unittest.main()
