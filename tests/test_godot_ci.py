from source_fixture import SDK_ROOT
"""Require exact native shard closure and preserve distinct source-declared hosts."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
import subprocess
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import godot_ci
from source_fixture import read_source, write_source, dump_source


class GodotTopologyControls(unittest.TestCase):
    def delivery_source(self, root):
        (root/'deploy').mkdir()
        write_source(root/'deploy/delivery.toml', {'version':1,'validation_timeout_minutes':60})
        def git(*args):
            return subprocess.check_output(['git','-c','user.name=Fixture','-c','user.email=fixture@example.test',
                '-c','commit.gpgsign=false','-C',str(root),*args],text=True,stderr=subprocess.DEVNULL).strip()
        git('init');git('add','.');git('commit','-qm','Native deadline')
        source=git('rev-parse','HEAD')
        environment=dict(GITHUB_REPOSITORY='owner/demo',GITHUB_SHA=source,GITHUB_WORKFLOW_SHA=source,
            GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/cd.yml@refs/heads/main',
            GITHUB_REF='refs/heads/main',GITHUB_EVENT_NAME='push',SOURCE_SHA=source,COOKBOOK_VALIDATION_TIMEOUT='60')
        config=self.config();config['delivery']={'definition':'deploy/delivery.toml'}
        config['ci']['godot'].update(build_timeout_minutes=45,unit_timeout_minutes=45,host_timeout_minutes=45)
        return config,environment

    def test_only_exact_main_cd_retains_the_separate_tracked_host_deadline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);config,environment=self.delivery_source(root)
            actual=godot_ci.plan(root,config,environment)
            self.assertEqual([row['timeout_minutes'] for row in actual['requirements']], [45,45,45,60])
            for changes in [dict(GITHUB_EVENT_NAME='pull_request'),dict(GITHUB_EVENT_NAME='issue_comment'),
                    dict(GITHUB_REF='refs/pull/1/merge'),dict(GITHUB_WORKFLOW_SHA='a'*40),
                    dict(SOURCE_SHA='a'*40),dict(GITHUB_WORKFLOW_REF='owner/demo/.github/workflows/pr-preview.yml@refs/heads/main')]:
                with self.subTest(changes=changes):
                    plan=godot_ci.plan(root,config,environment|changes)
                    self.assertEqual(plan['requirements'][-1]['timeout_minutes'],45)
            with self.assertRaisesRegex(ValueError,'caller deadline'):
                godot_ci.plan(root,config,environment|{'COOKBOOK_VALIDATION_TIMEOUT':'180'})
            write_source(root/'deploy/delivery.toml', {'version':1,'validation_timeout_minutes':60,'modified':True})
            with self.assertRaisesRegex(ValueError,'exact tracked source'):
                godot_ci.plan(root,config,environment)

    def test_publication_recomputes_the_effective_source_bound_matrix_instead_of_trusting_its_deadline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);config,environment=self.delivery_source(root)
            declaration=godot_ci.plan(root,config,environment);source=environment['GITHUB_SHA'];tools='a'*40
            entries=[row for row in declaration['requirements'] if row['kind']!='host']
            matrix={key:declaration[key] for key in ['version','requirements','host_groups','unit_inventory']}
            matrix.update(source_commit=source,cookbook_commit=tools,jobs=['validated'],
                proofs=[{'entry':row,'build':{'same':'build'}} for row in entries])
            with patch.object(godot_ci,'validate_partial'),patch.object(godot_ci,'validate_jobs',return_value=['validated']):
                self.assertEqual(godot_ci.validate_acceptance(matrix,config,{},source,tools,environment,'b'*64,root=root),['validated'])
                mutated=copy.deepcopy(matrix);mutated['requirements'][-1]['timeout_minutes']=180
                with self.assertRaisesRegex(ValueError,'candidate contract'):
                    godot_ci.validate_acceptance(mutated,config,{},source,tools,environment,'b'*64,root=root)
                with self.assertRaisesRegex(ValueError,'candidate contract'):
                    godot_ci.validate_acceptance(matrix,config,{},source,tools,environment,'b'*64)

    def config(self):
        return {'recipe': 'godot-game', 'tests': {
            'unit': [{'name': 'test-case_' + str(index), 'runner': 'godot-check', 'path': f'app/tests/case_{index}.gd'} for index in range(59)] +
                    [{'runner': 'python', 'path': 'tests/native/test_sources.py'}],
            'integration': [{'name': 'test-backup', 'runner': 'python', 'path': 'tests/backup/test_restore.py'}]},
            'ci': {'runner': 'blacksmith-4vcpu-ubuntu-2404', 'timeout_minutes': 20,
                   'godot': {'unit_shards': 2, 'unit_workers': 4, 'build_timeout_minutes': 25,
                             'unit_timeout_minutes': 20, 'host_timeout_minutes': 20}}}

    def test_exact_sixty_original_groups_are_split_without_duplicate_assertions(self):
        config = self.config(); plan = godot_ci.plan(None, config)
        units = plan['units']['include']; host = next(value for value in plan['requirements'] if value['kind'] == 'host')
        self.assertEqual(len(plan['unit_inventory']), 60)
        self.assertEqual(len(plan['godot_unit_inventory']), 59)
        first, second = map(lambda value: set(value['cases']), units)
        self.assertFalse(first & second)
        all_cases = set(plan['written_case_inventory'])
        self.assertEqual(first | second | set(host['cases']), all_cases)
        self.assertEqual([value['timeout_minutes'] for value in units], [20, 20])
        self.assertEqual(next(value['timeout_minutes'] for value in plan['requirements'] if value['kind'] == 'build'), 25)

    def test_backup_and_stock_browser_use_their_actual_runner_and_deadlines(self):
        config = self.config(); config['node'] = {'packages': [{'directory': 'tests/e2e', 'manager': 'bun'}]}
        config['ci']['godot']['gates'] = [
            {'name': 'backup-fixture', 'runner': 'blacksmith-4vcpu-ubuntu-2404', 'timeout_minutes': 20,
             'suite': 'integration', 'tests': ['test-backup']},
            {'name': 'browser-capability', 'kind': 'capability', 'runner': 'blacksmith-4vcpu-ubuntu-2204', 'timeout_minutes': 8,
             'setup': 'browser', 'packages': ['tests/e2e'], 'entry': {'runner': 'bun', 'path': 'tests/e2e/browser_preflight.mjs', 'timeout': 120}},
            {'name': 'production', 'kind': 'production', 'runner': 'blacksmith-4vcpu-ubuntu-2204', 'timeout_minutes': 10,
             'setup': 'browser', 'packages': ['tests/e2e'], 'entry': {'runner': 'bun', 'path': 'tests/e2e/production_smoke.mjs', 'timeout': 360}}]
        plan = godot_ci.plan(None, config); gates = {value['id']: value for value in plan['gates']['include']}
        self.assertEqual(gates['gate-backup-fixture']['runner'], 'blacksmith-4vcpu-ubuntu-2404')
        self.assertEqual(gates['gate-browser-capability']['runner'], 'blacksmith-4vcpu-ubuntu-2204')
        self.assertEqual(gates['gate-browser-capability']['timeout_minutes'], 8)
        self.assertNotIn('gate-production', [value['id'] for value in plan['requirements']])
        self.assertEqual(plan['production']['include'][0]['timeout_minutes'], 10)
        backup = gates['gate-backup-fixture']['cases'][0]
        host = next(value for value in plan['requirements'] if value['kind'] == 'host')
        self.assertNotIn(backup, host['cases'])

    def test_real_go_unit_packages_require_native_host_unit_coverage(self):
        config=self.config();config['components']={'rust':[]}
        config['components']['go']=[{'directory':'deploy/native','package':'example.invalid/native/cmd/server',
            'output':'.artifacts/native/server','test_packages':['example.invalid/native/security/...']}]
        declaration=godot_ci.plan(None,config)
        self.assertIn('test-native',declaration['unit_inventory'])

    def test_unknown_empty_or_duplicate_slices_fail_before_execution(self):
        for shard in ['0/2', '1/0', '3/2', '1/3', '1', '1/2;echo', '-1/2']:
            with self.subTest(shard=shard), self.assertRaises(ValueError):
                godot_ci.partition(['test-one', 'test-two'], shard)
        for targets in [[], ['test-one', 'test-one'], ['--eval=bad']]:
            with self.assertRaises(ValueError): godot_ci.partition(targets, '1/1')
        config = self.config(); config['ci']['godot']['unit_workers'] = 8
        with self.assertRaises(ValueError): godot_ci.plan(None, config)

    def test_real_parallel_children_propagate_failure_and_restored_success(self):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'other-ran'
            cases = {'test-failure': [[sys.executable, '-c', 'raise SystemExit(19)']],
                     'test-other': [[sys.executable, '-c', 'from pathlib import Path;Path(' + repr(str(marker)) + ').write_text("ran")']]}
            def execute(commands):
                for command in commands:
                    result = subprocess.run(command, check=False)
                    if result.returncode: return result.returncode
                return 0
            self.assertEqual(godot_ci.run_cases(cases, 2, execute), 19)
            self.assertEqual(marker.read_text(), 'ran')
            cases['test-failure'] = [[sys.executable, '-c', 'raise SystemExit(0)']]
            self.assertEqual(godot_ci.run_cases(cases, 2, execute), 0)


    def test_production_browser_display_probe_policy_preserves_actual_native_program_scope(self):
        config=self.config()
        config['ci']['godot']['gates']=[{'name':'production','kind':'production','runner':'blacksmith-4vcpu-ubuntu-2204',
            'timeout_minutes':15,'setup':'browser','entry':{'runner':'node','path':'tests/browser/public.mjs','timeout':900},
            'browsers':['chromium','firefox','webkit'],'xvfb':['chromium','firefox'],'probe':False}]
        declaration=godot_ci.plan(None,config)
        self.assertFalse(any(row['kind']=='production' for row in declaration['requirements']))
        production=declaration['production']['include'][0]
        self.assertEqual(production['browsers'],['chromium','firefox','webkit'])
        self.assertEqual(production['xvfb'],['chromium','firefox']);self.assertIs(production['probe'],False)
        for key,value in [('browsers',['firefox']),('browsers',['chromium','chromium']),('xvfb',['missing']),('probe','false')]:
            candidate=copy.deepcopy(config);candidate['ci']['godot']['gates'][0][key]=value
            with self.assertRaises(ValueError):godot_ci.plan(None,candidate)


class SelectionEvidenceControls(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.targets = ['test-one', 'test-three', 'test-two']
        for number, selected in [(1, ['test-one', 'test-two']), (2, ['test-three'])]:
            folder = self.root / str(number); folder.mkdir()
            (folder / 'godot-test-selection.json').write_text(json.dumps({
                'source_commit': 'a' * 40, 'run_id': '123', 'attempt': '2',
                'shard': f'{number}/2', 'workers': 4, 'inventory': self.targets,
                'selected': selected, 'status': 'passed'}))

    def verify(self):
        return godot_ci.verify_selections(self.root, 'a' * 40, self.targets, 2, '123', '2')

    def test_exact_current_run_disjoint_inventory_passes(self):
        self.assertEqual(self.verify()['suite_targets'], 3)

    def test_stale_failed_repeated_or_partial_evidence_cannot_pass(self):
        path = self.root / '2/godot-test-selection.json'; original = json.loads(path.read_text())
        for key, value in [('source_commit', 'b' * 40), ('run_id', '122'), ('attempt', '1'),
                           ('status', 'failed'), ('workers', 8), ('shard', '1/2'),
                           ('selected', ['test-one']), ('inventory', self.targets[:-1])]:
            with self.subTest(field=key):
                path.write_text(json.dumps({**original, key: value}))
                with self.assertRaises(ValueError): self.verify()
        path.unlink()
        with self.assertRaises(ValueError): self.verify()

    def test_linked_proof_cannot_impersonate_completed_coverage(self):
        path = self.root / '2/godot-test-selection.json'; saved = self.root / 'saved.json'
        path.rename(saved); path.symlink_to(saved)
        with self.assertRaises(ValueError): self.verify()



if __name__ == '__main__':
    unittest.main()
