"""Preserve scheduled work and narrow authority when maintenance shares a workflow."""
from source_fixture import SDK_ROOT, write_source
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import maintenance_workflows as maintenance
import operational_workflows as operations
from workflow_fixtures import read_yaml


class MaintenanceWorkflows(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'deploy').mkdir()
        self.config = {'recipe': 'react-native', 'delivery': {'definition': 'deploy/delivery.toml'},
            'operations': {'definition': 'deploy/operations.toml'}}
        write_source(self.root / 'deploy/delivery.toml', dict(version=1, project='demo', komizo_ref='a'*40,
            publish_runner='ubuntu-24.04', publish_timeout=35, deploy_runner='ubuntu-24.04',
            deploy_timeout=100, health_urls=['https://example.test/readyz'], notification_url='https://example.test/'))
        self.settings = {'tools': {'runner': 'ubuntu-24.04', 'timeout_minutes': 40,
                'schedule': ['5 2 * * *', '10 2 * * 1'], 'concurrency': 'dependency-maintenance-tools',
                'step_timeouts': {'checkout': 3, 'setup': 5, 'check': 12, 'report': 3}},
            'packages': {'runner': 'ubuntu-24.04', 'timeout_minutes': 130,
                'schedule': ['10 2 * * 1'], 'concurrency': 'dependency-maintenance-packages',
                'step_timeouts': {'checkout': 3, 'setup': 5, 'check': 90, 'artifact': 5, 'report': 3}}}
        self.data = {'version': 1, 'project': 'demo', 'maintenance': self.settings,
            'security': {'runner': 'ubuntu-24.04', 'timeout_minutes': 110,
                'report_timeout': 5, 'concurrency': 'security'}}

    def workflow(self):
        write_source(self.root / 'deploy/operations.toml', self.data)
        files = operations.workflow_files(self.root, 'b'*40, self.config)
        title = 'Security' if 'security' in self.data else 'Maintenance'
        path = f'.github/workflows/{title.lower()}.yml'
        self.assertEqual(set(files), {path})
        workflow = read_yaml(files[path])
        self.assertEqual(workflow['name'], title)
        return workflow

    def enabled(self, job, event, task='', cron=''):
        text = job['if'].removeprefix('${{ ').removesuffix(' }}').replace('&&', 'and').replace('||', 'or')
        return eval(text, {'__builtins__': {}}, {'github': SimpleNamespace(ref='refs/heads/main',
            event_name=event, event=SimpleNamespace(schedule=cron)), 'inputs': SimpleNamespace(task=task),
            'contains': lambda values, item: item in values, 'fromJSON': json.loads})

    def test_roles_keep_exact_schedules_and_explicit_manual_selection(self):
        value = self.workflow()
        self.assertEqual(value['on']['workflow_dispatch']['inputs']['task']['options'], ['dependencies', 'security'])
        self.assertEqual(value['on']['workflow_dispatch']['inputs']['task']['default'], 'dependencies')
        self.assertEqual(value['on']['schedule'], [{'cron': '5 2 * * *'}, {'cron': '10 2 * * 1'}])
        jobs = value['jobs']
        for event, task, cron, expected in [('workflow_dispatch', 'dependencies', '', {'tools', 'packages'}),
                ('workflow_dispatch', 'security', '', {'security'}), ('workflow_dispatch', 'all', '', set()),
                ('schedule', '', '5 2 * * *', {'tools'}), ('schedule', '', '10 2 * * 1', {'tools', 'packages'}),
                ('schedule', '', '0 0 * * *', set())]:
            with self.subTest(event=event, task=task, cron=cron):
                self.assertEqual({name for name in ['security', 'tools', 'packages']
                    if self.enabled(jobs[name], event, task, cron)}, expected)
        self.data['security']['schedule'] = ['15 3 * * *']
        jobs = self.workflow()['jobs']
        self.assertTrue(self.enabled(jobs['security'], 'schedule', cron='15 3 * * *'))
        self.assertFalse(self.enabled(jobs['tools'], 'schedule', cron='15 3 * * *'))

    def test_jobs_preserve_limits_outputs_and_separate_privileges_and_locks(self):
        value = self.workflow()
        self.assertEqual(value['permissions'], {})
        self.assertNotIn('concurrency', value)
        jobs = value['jobs']
        self.assertEqual(set(jobs), {'security', 'report-security-failure', 'tools', 'packages'})
        self.assertNotIn('issues', jobs['security']['permissions'])
        self.assertNotIn('packages', jobs['security']['permissions'])
        self.assertNotIn('report-timeout', jobs['security']['with'])
        self.assertTrue(all(not name.startswith('report') for name in json.loads(jobs['security']['with']['step-timeouts'])))
        report = jobs['report-security-failure']
        self.assertEqual(report['permissions'], {'contents': 'read', 'issues': 'write'})
        self.assertEqual(report['with']['timeout-minutes'], 5)
        self.assertIn("needs.security.result == 'failure'", report['if'])
        self.assertEqual(jobs['tools']['concurrency']['group'], 'dependency-maintenance-tools')
        self.assertEqual(jobs['packages']['concurrency']['group'], 'dependency-maintenance-packages')
        self.assertEqual(jobs['security']['concurrency']['group'], 'security')
        self.assertEqual(jobs['packages']['timeout-minutes'], 130)
        self.assertEqual(next(s for s in jobs['tools']['steps'] if s['name']=='Set up development tools')['with'], {'tools': 'python'})
        self.assertEqual(next(s for s in jobs['packages']['steps'] if s['name']=='Set up development tools')['with'], {'tools': 'python bun'})
        self.assertEqual(next(s for s in jobs['packages']['steps'] if s['name']=='Check package versions')['timeout-minutes'], 90)
        artifact = next(s for s in jobs['tools']['steps'] if s['name']=='Upload artifacts')
        self.assertNotIn('timeout-minutes', artifact)
        self.assertEqual(artifact['with']['name'], 'tool-updates')
        self.assertEqual(artifact['with']['retention-days'], 30)
        self.assertTrue(all('secrets' not in job for job in jobs.values()))

    def test_dispatch_exposes_only_existing_roles(self):
        self.data.pop('maintenance')
        task = self.workflow()['on']['workflow_dispatch']['inputs']['task']
        self.assertEqual(task['options'], ['security'])
        self.assertEqual(task['default'], 'security')
        self.data['maintenance'] = self.settings
        self.data.pop('security')
        task = self.workflow()['on']['workflow_dispatch']['inputs']['task']
        self.assertEqual(task['options'], ['dependencies'])
        self.assertEqual(task['default'], 'dependencies')

    def test_backup_storage_is_a_protected_explicit_manual_check(self):
        self.settings['backup-storage'] = {'runner': 'ubuntu-24.04', 'timeout_minutes': 5,
                                           'environment': 'Production'}
        workflow = self.workflow()
        inputs = workflow['on']['workflow_dispatch']['inputs']
        self.assertEqual(inputs['task']['options'], ['dependencies', 'security', 'backup-storage'])
        self.assertFalse(inputs['storage-capability']['default'])
        storage = workflow['jobs']['backup-storage']
        self.assertIn("github.event_name == 'workflow_dispatch'", storage['if'])
        self.assertIn('&& inputs.storage-capability', storage['if'])
        self.assertEqual(storage['permissions'], {'contents': 'read'})
        self.assertEqual(storage['environment'], 'Production')
        self.assertNotIn('BACKUP_S3_ACCESS_KEY', str(storage['steps'][:2]))
        for settings in [{'environment': 'Preview'}, {'schedule': ['0 0 * * *']}]:
            invalid = copy.deepcopy(self.settings)
            invalid['backup-storage'].update(settings)
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                maintenance.declaration(invalid)

    def test_declared_jobs_cannot_select_programs_credentials_or_unbounded_work(self):
        for changes in [{'secrets': {'KEY': 'value'}}, {'program': 'deploy/paid.py'},
                {'environment': 'Production'}, {'timeout_minutes': True}, {'timeout_minutes': 181},
                {'runner': '${{ vars.RUNNER }}'}, {'schedule': [{}]}, {'schedule': ['0 0 * * *']*2},
                {'step_timeouts': {'command': 1}}, {'step_timeouts': {'check': 41}}]:
            value = copy.deepcopy(self.settings)
            value['tools'].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                maintenance.declaration(value)
        with self.assertRaises(ValueError):
            maintenance.declaration({'paid': self.settings['tools']})


if __name__ == '__main__':
    unittest.main()
