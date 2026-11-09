from source_fixture import SDK_ROOT, write_source
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import delivery_workflows as delivery
import generate
from test_dependency_security import read_yaml


class DeliveryWorkflows(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        self.config={'delivery':{'definition':'delivery.toml'}}
        self.data=dict(version=1,project='demo',komizo_ref='a'*40,publish_runner='ubuntu-24.04',publish_timeout=35,
            deploy_runner='ubuntu-24.04',deploy_timeout=100,health_urls=['https://example.test/readyz'],
            notification_url='https://example.test/')

    def write(self):write_source(self.root/'delivery.toml',self.data)
    def jobs(self):self.write();return read_yaml(delivery.jobs(self.root,self.config,'b'*40))

    def test_same_source_publication_and_effects_use_separate_pinned_authorities(self):
        jobs=self.jobs();publish=jobs['publish'];deploy=jobs['deploy']
        self.assertEqual(publish['needs'],'validate');self.assertEqual(deploy['needs'],'publish')
        self.assertEqual(publish['uses'],'nicodes/komizo-actions/.github/workflows/cookbook-publish.yml@'+'a'*40)
        self.assertEqual(deploy['uses'],'nicodes/komizo-actions/.github/workflows/cookbook-deliver.yml@'+'a'*40)
        self.assertEqual(deploy['permissions']['id-token'],'write')
        self.assertNotIn('id-token',publish['permissions'])
        self.assertEqual(deploy['with']['source'],'${{ github.sha }}')
        self.assertEqual(deploy['with']['declaration'],'delivery.toml')
        self.assertEqual(deploy['secrets'],{'KOMIZO_DEPLOY_KEY':'${{ secrets.KOMIZO_DEPLOY_KEY }}'})
        for job in [publish,deploy]:
            self.assertNotIn('steps',job);self.assertNotIn('concurrency',job)
            self.assertIn('inputs.source == github.sha',job['if'])
        self.assertNotIn('secrets',publish)

    def test_old_transport_fields_unpinned_refs_and_unbounded_budget_are_rejected(self):
        for update in [{'komizo_ref':'main'},{'deploy_runner':'${{ vars.RUNNER }}'},{'deploy_timeout':181},
            {'health_urls':['http://example.test/']},{'secrets':{}},{'variables':{}},{'optional_secrets':{}},
            {'config':'deploy/config.Dockerfile'},{'expected_secret_generations':{'runtime':'bad'}}]:
            previous=dict(self.data);self.data.update(update);self.write()
            with self.subTest(update=update),self.assertRaises(ValueError):delivery.declaration(self.root,self.config)
            self.data=previous

    def test_backup_credentials_enter_only_explicit_protected_delivery(self):
        self.assertNotIn('BACKUP_S3_ACCESS_KEY',str(self.jobs()))
        self.data['upload_backup']=True;jobs=self.jobs()
        self.assertNotIn('secrets',jobs['publish'])
        self.assertEqual(jobs['deploy']['secrets']['BACKUP_S3_ACCESS_KEY'],'${{ secrets.BACKUP_S3_ACCESS_KEY }}')
        self.assertEqual(jobs['deploy']['secrets']['BACKUP_S3_SECRET_KEY'],'${{ secrets.BACKUP_S3_SECRET_KEY }}')

    def test_combined_delivery_keeps_original_runner_and_one_total_deadline(self):
        self.data.update(combined=True,publish_timeout=15,deploy_timeout=15)
        jobs=self.jobs();self.assertNotIn('publish',jobs);deploy=jobs['deploy']
        self.assertEqual(deploy['needs'],'validate');self.assertEqual(deploy['with']['timeout-minutes'],15)
        self.assertTrue(deploy['with']['combined']);self.assertEqual(deploy['permissions']['packages'],'write')
        self.data['publish_timeout']=35;self.write()
        with self.assertRaises(ValueError):delivery.declaration(self.root,self.config)

    def test_public_browser_keeps_its_separate_original_budget_and_is_required_for_reporting(self):
        self.data.update(combined=True,publish_timeout=15,deploy_timeout=15)
        with patch.object(delivery,'production_gates',return_value=[{'runner':'ubuntu-24.04','timeout_minutes':15}]):
            jobs=self.jobs()
        public=jobs['public_verification']
        self.assertEqual(public['needs'],'deploy')
        self.assertIn("outputs.phase == 'ready'",public['if'])
        self.assertEqual(public['permissions'],{'contents':'read'})
        self.assertNotIn('secrets',public)
        self.assertEqual(public['with']['timeout-minutes'],15)
        self.assertEqual(jobs['deploy']['with']['timeout-minutes'],15)
        self.assertEqual(public['with']['release-id'],'${{ needs.deploy.outputs.public-release-id }}')
        self.assertIn('public_verification',jobs['report']['needs'])
        self.assertIn('needs.public_verification.result',jobs['report']['with']['deployment'])

    def test_public_browser_cannot_run_without_the_accepted_combined_packet(self):
        self.write()
        with patch.object(delivery,'production_gates',return_value=[{'runner':'ubuntu-24.04','timeout_minutes':15}]):
            with self.assertRaisesRegex(ValueError,'accepted packet'):
                delivery.declaration(self.root,self.config)

    def test_literal_generation_selection_and_readiness_body_assertions_survive(self):
        self.data['expected_secret_generations']={'runtime':'c'*32,'migration':'d'*32}
        self.data['health_urls'].append({'url':'https://example.test/release.json','contains':'{revision}'})
        self.write();data=delivery.declaration(self.root,self.config)
        self.assertEqual(data['expected_secret_generations'],self.data['expected_secret_generations'])
        self.assertEqual(data['health_urls'][1]['contains'],'{revision}')
        self.assertNotIn('KOMIZO_SECRET_',str(self.jobs()))

    def test_product_effect_deadlines_and_reporting_choices_are_preserved(self):
        self.data.update(environment='Production',notify=False,step_timeouts={'deploy':25,'readiness':5,'diagnostics':3},
            report={'runner':'ubuntu-24.04','timeout_minutes':5,'step_timeouts':{'checkout':3,'setup':5,'report':3}})
        jobs=self.jobs();deploy=jobs['deploy'];report=jobs['report']
        self.assertEqual(deploy['with']['environment'],'Production')
        self.assertIn('"deploy": 25',deploy['with']['step-timeouts'])
        self.assertEqual(report['with']['report-runner'],'ubuntu-24.04')
        self.assertFalse(report['with']['notify']);self.assertNotIn('secrets',report)
        for value in [{'deploy':101},{'command':5},{'readiness':True}]:
            self.data['step_timeouts']=value;self.write()
            with self.subTest(value=value),self.assertRaises(ValueError):delivery.declaration(self.root,self.config)

    def test_readiness_operations_cannot_select_two_policies(self):
        self.data['readiness_operation']=True;self.write()
        with self.assertRaisesRegex(ValueError,'one native production policy'):delivery.declaration(self.root,self.config)
        self.config['operations']={'definition':'deploy/operations.toml'}
        self.assertTrue(delivery.declaration(self.root,self.config)['readiness_operation'])

    def test_production_workflow_retains_source_validation_without_duplicate_main_build(self):
        self.write();(self.root/'tests').mkdir();(self.root/'tests/unit.py').write_text('assert True\n')
        (self.root/'cookbook.toml').write_text('version=1\nrecipe="react-native"\n[delivery]\ndefinition="delivery.toml"\n[[tests.unit]]\nrunner="python"\npath="tests/unit.py"\n')
        files=generate.workflow_files(self.root,'b'*40)
        self.assertNotIn('  push:',files['.github/workflows/ci.yml']);self.assertIn('  push:',files['.github/workflows/cd.yml'])
        self.assertNotIn('  pull_request:',files['.github/workflows/cd.yml'])
        self.assertIn('merge-gate: false',files['.github/workflows/cd.yml'])
        self.assertIn('cookbook-deliver.yml@',files['.github/workflows/cd.yml'])
        self.assertIn('group: deploy-source-${{ github.sha }}-',files['.github/workflows/cd.yml'])
        self.assertNotIn('group: deploy-production',files['.github/workflows/cd.yml'])
