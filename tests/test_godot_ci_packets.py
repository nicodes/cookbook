from source_fixture import SDK_ROOT, native_entry
"""Reject incomplete, stale, substituted and falsely complete Godot evidence."""
import dependencies
import copy
from contextlib import chdir
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import godot_ci as c


class GodotPacketControls(unittest.TestCase):
    source = 'a' * 40
    tools = 'b' * 40
    canonical = 'c' * 64
    tree = 'd' * 40
    environment = {'GITHUB_REPOSITORY': 'fixture/game', 'GITHUB_RUN_ID': '12', 'GITHUB_RUN_ATTEMPT': '2',
                   'GITHUB_WORKFLOW_REF': 'fixture/game/.github/workflows/ci.yml@refs/pull/3/merge',
                   'GITHUB_WORKFLOW_SHA': 'e' * 40}

    def config(self):
        return {'recipe': 'godot-game', 'tests': {'unit': [
            {'name': 'test-one', 'runner': 'godot-check', 'path': 'app/tests/one.gd'},
            {'name': 'test-two', 'runner': 'godot-check', 'path': 'app/tests/two.gd'},
            {'runner': 'python', 'path': 'tests/native/check.py'}]},
            'ci': {'runner': 'ubuntu-24.04', 'godot': {'unit_shards': 2}}}

    def proof(self, entry):
        context = {**self.environment, 'GITHUB_JOB': c.execution_job(entry)}
        provenance = {'source_commit': self.source, 'source_tree': self.tree, 'tracked_source_clean': True,
                      'input_identity': 'f' * 64, 'execution_context': {**self.environment, 'GITHUB_JOB': 'build'}}
        return {'version': 1, 'coverage': 'godot-partial', 'complete_host': False, 'status': 'passed',
                'repository': self.environment['GITHUB_REPOSITORY'], 'source_commit': self.source,
                'source_tree': self.tree, **dependencies.binding(self.tools), 'configuration_sha256': self.canonical,
                'entry': entry, 'execution_context': context, 'executed_cases': entry.get('cases', []),
                'build': {'identity': 'f' * 64, 'artifacts': {'public/index.html': hashlib.sha256(b'fixture').hexdigest()},
                          'provenance': provenance}, 'worker_identity': '1' * 64,
                'receipt': {**provenance, 'execution_context': context, 'tested_commit': self.source, 'input_identity': '1' * 64}}

    def matrix(self):
        plan = c.plan(None, self.config())
        entries = [row for row in plan['requirements'] if row['kind'] != 'host']
        jobs = {'total_count': len(entries), 'jobs': [{'id': index + 1, 'name': 'Full / ' + row['job'],
            'status': 'completed', 'conclusion': 'success', 'labels': [row['runner']]} for index, row in enumerate(entries)]}
        matrix = {'version': 1, 'source_commit': self.source, **dependencies.binding(self.tools),
                  'requirements': plan['requirements'], 'host_groups': plan['host_groups'], 'unit_inventory': plan['unit_inventory'],
                  'proofs': [self.proof(row) for row in entries], 'jobs': c.validate_jobs(entries, jobs)}
        return matrix, jobs

    def accept(self, matrix, jobs):
        return c.validate_acceptance(matrix, self.config(), jobs, self.source, self.tools, self.environment, self.canonical)

    def test_actual_complete_disjoint_same_execution_matrix_passes(self):
        matrix, jobs = self.matrix()
        self.assertEqual(len(self.accept(matrix, jobs)), 3)
        host = {'status': 'passed', 'build_verified': True, 'tests_verified': True, 'test_groups': matrix['host_groups'],
                'source_commit': self.source, **dependencies.binding(self.tools)}
        self.assertEqual(c.finalize_host(host, matrix)['coverage'], 'complete-host-and-godot-matrix')

    def test_missing_repeated_stale_or_falsely_complete_partial_is_rejected(self):
        for mutation in ['missing', 'repeat', 'source', 'tools', 'config', 'attempt', 'scope', 'complete', 'cases', 'receipt']:
            with self.subTest(mutation=mutation):
                matrix, jobs = self.matrix()
                proof = matrix['proofs'][1]
                if mutation == 'missing': matrix['proofs'].pop()
                elif mutation == 'repeat': matrix['proofs'][2] = copy.deepcopy(proof)
                elif mutation == 'source': proof['source_commit'] = '0' * 40
                elif mutation == 'tools': proof['cookbook_commit'] = '0' * 40
                elif mutation == 'config': proof['configuration_sha256'] = '0' * 64
                elif mutation == 'attempt': proof['execution_context']['GITHUB_RUN_ATTEMPT'] = '1'
                elif mutation == 'scope': proof['entry'] = {**proof['entry'], 'cases': []}
                elif mutation == 'complete': proof['complete_host'] = True
                elif mutation == 'cases': proof['executed_cases'] = []
                elif mutation == 'receipt': proof['receipt']['tested_commit'] = '0' * 40
                with self.assertRaises(ValueError): self.accept(matrix, jobs)

    def test_different_product_builds_and_source_image_tags_are_rejected(self):
        matrix, jobs = self.matrix()
        matrix['proofs'][1]['build']['artifacts']['public/index.html'] = '0' * 64
        with self.assertRaises(ValueError): self.accept(matrix, jobs)
        for name, value in [('image:ghcr.io/fixture/game-gate:latest', 'sha256:' + '1' * 64),
                            ('image:ghcr.io/fixture/game-gate:' + self.source, 'mutable')]:
            matrix, jobs = self.matrix()
            for proof in matrix['proofs']: proof['build']['artifacts'][name] = value
            with self.assertRaises(ValueError): self.accept(matrix, jobs)

    def test_job_runner_completion_ambiguity_and_attempt_substitution_are_rejected(self):
        for mutation in ['runner', 'conclusion', 'incomplete', 'repeat', 'id']:
            matrix, jobs = self.matrix()
            if mutation == 'runner': jobs['jobs'][0]['labels'] = ['ubuntu-22.04']
            elif mutation == 'conclusion': jobs['jobs'][0]['conclusion'] = 'failure'
            elif mutation == 'incomplete': jobs['total_count'] += 1
            elif mutation == 'repeat': jobs['jobs'].append(copy.deepcopy(jobs['jobs'][0])); jobs['total_count'] += 1
            elif mutation == 'id': jobs['jobs'][0]['id'] = 999
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): self.accept(matrix, jobs)

    def test_partial_matrix_cannot_replace_host_or_omit_host_groups(self):
        matrix, _ = self.matrix()
        for host in [matrix['proofs'][1], {'status': 'passed', 'build_verified': True, 'tests_verified': True,
                       'source_commit': self.source, **dependencies.binding(self.tools), 'test_groups': ['unit']}]:
            with self.assertRaises(ValueError): c.finalize_host(host, matrix)

    def test_build_packet_checks_bytes_closed_paths_modes_and_links(self):
        entry = c.plan(None, self.config())['requirements'][0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {**self.config(), 'artifacts': {'paths': ['public']}}
            stage = root / 'incoming'; (stage / 'payload/public').mkdir(parents=True)
            payload = stage / 'payload/public/index.html'; payload.write_bytes(b'fixture')
            proof = self.proof(entry); proof['executables'] = {'public/index.html': False}
            (stage / 'godot-proof.json').write_text(json.dumps(proof))
            with patch.object(c, 'configuration_hash', return_value=self.canonical), patch('ci.git', return_value=self.tree):
                accepted = c.packet(root, stage, config, self.source, self.tools, entry, self.environment)
                with self.assertRaisesRegex(ValueError, 'current exact checkout'):
                    c.import_build(root, config, stage, accepted)
                self.assertFalse((root / 'public').exists())
                with chdir(root):
                    c.import_build(root, config, stage, accepted)
                    self.assertEqual((root / 'public/index.html').read_bytes(), b'fixture')
                    receipt = root / '.artifacts/verification/build.json'
                    self.assertEqual(json.loads(receipt.read_text()), accepted['build'])
                    receipt.unlink()
                    receipt.symlink_to(root / 'outside-receipt.json')
                    with self.assertRaisesRegex(ValueError, 'owned regular file'):
                        c.import_build(root, config, stage, accepted)
                    self.assertFalse((root / 'outside-receipt.json').exists())
                payload.write_bytes(b'changed')
                with self.assertRaises(ValueError): c.packet(root, stage, config, self.source, self.tools, entry, self.environment)
                payload.write_bytes(b'fixture'); (stage / 'extra').write_bytes(b'undeclared')
                with self.assertRaises(ValueError): c.packet(root, stage, config, self.source, self.tools, entry, self.environment)
                (stage / 'extra').unlink(); payload.unlink(); payload.symlink_to(root / 'elsewhere')
                with self.assertRaises((ValueError, FileNotFoundError)): c.packet(root, stage, config, self.source, self.tools, entry, self.environment)

    def test_host_filters_only_sharded_cases_and_local_tests_remain_whole(self):
        config = self.config(); matrix, _ = self.matrix()
        self.assertEqual(len(c.selected_entries(Path('/fixture'), config, {}, 'unit')), 3)
        with patch.object(c, 'host_matrix', return_value=matrix):
            environment = {'COOKBOOK_GODOT_HOST': '/fixture/.artifacts/godot-host-matrix.json'}
            selected = c.selected_entries(Path('/fixture'), config, environment, 'unit')
            self.assertEqual([entry['runner'] for entry in selected], ['python'])
            self.assertEqual(len(c.require_units(Path('/fixture'), config, environment)), 3)
            matrix['proofs'].pop()
            with self.assertRaises(ValueError): c.require_units(Path('/fixture'), config, environment)

    def test_host_revalidation_writes_a_real_protected_checkout_receipt(self):
        import ci
        import godot_game
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            subprocess.run(['git','init','-q'],cwd=root,check=True)
            (root/'fixture.txt').write_text('native source\n')
            subprocess.run(['git','add','fixture.txt'],cwd=root,check=True)
            subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
                'commit','-qm','Native fixture'],cwd=root,check=True)
            source=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
            (root/'public').mkdir();(root/'public/index.html').write_bytes(b'fixture')
            config={**self.config(),'artifacts':{'paths':['public']}}
            verify=ci.verification()
            with chdir(root):
                artifacts=verify.artifacts(godot_game.verification_scope(root,config))
            matrix={'source_commit':source,**dependencies.binding(self.tools),
                'proofs':[{'entry':{'kind':'build'},'build':{'artifacts':artifacts}}]}
            with self.assertRaisesRegex(ValueError,'current exact checkout'):
                c.revalidate_host_build(root,config,matrix,{})
            with chdir(root),patch.object(c,'host_matrix',return_value=matrix),patch.object(ci,'verification',return_value=verify),patch.object(verify,'invoke') as native_validation:
                c.revalidate_host_build(root,config,matrix,{})
                receipt=json.loads((root/'.artifacts/verification/build.json').read_text())
                self.assertEqual(receipt['artifacts'],artifacts)
                self.assertEqual(receipt['provenance']['source_commit'],source)
                self.assertTrue(receipt['provenance']['tracked_source_clean'])
            self.assertEqual(native_entry(native_validation.call_args.args[0])[2:],
                ['validate-import','--source',source,'--cookbook-ref',self.tools])

    def test_entirely_delegated_e2e_group_requires_an_actual_exact_join(self):
        config=self.config()
        config['tests']['e2e']=[{'name':'test-browser','runner':'node','path':'tests/browser/flows.mjs'}]
        config['ci']['godot']['gates']=[{'name':'browser','runner':'ubuntu-24.04','timeout_minutes':15,'suite':'e2e','tests':['test-browser']}]
        plan=c.plan(None,config)
        matrix={'requirements':plan['requirements'],'host_groups':plan['host_groups'],
                'proofs':[self.proof(entry) for entry in plan['requirements'] if entry['kind']!='host']}
        with patch.object(c,'host_matrix',return_value=matrix):
            joined=c.require_group(Path('/fixture'),config,{},'e2e')
            self.assertEqual(joined['local_cases'],[])
            self.assertEqual(len(joined['delegated_cases']),1)
            gate=next(proof for proof in matrix['proofs'] if proof['entry']['kind']=='test')
            gate['executed_cases']=[]
            with self.assertRaises(ValueError):c.require_group(Path('/fixture'),config,{},'e2e')


if __name__ == '__main__':
    unittest.main()
