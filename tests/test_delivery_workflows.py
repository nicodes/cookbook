from source_fixture import SDK_ROOT, write_source
from pathlib import Path
import os
import json
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import delivery_workflows as delivery
import generate
from workflow_fixtures import read_yaml


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
        self.assertEqual(publish['uses'],'nicodes/komizo-actions/.github/workflows/publish.yml@'+'a'*40)
        self.assertEqual(deploy['uses'],'nicodes/komizo-actions/.github/workflows/deploy.yml@'+'a'*40)
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
        self.assertIn('needs.public_verification.result',jobs['notify']['with']['deployment'])

    def test_public_browser_cannot_run_without_the_accepted_combined_packet(self):
        self.write()
        with patch.object(delivery,'production_gates',return_value=[{'runner':'ubuntu-24.04','timeout_minutes':15}]):
            with self.assertRaisesRegex(ValueError,'accepted packet'):
                delivery.declaration(self.root,self.config)

    def test_manual_public_verification_uses_the_same_provider_and_read_only_permissions(self):
        self.data.update(combined=True, publish_timeout=15, deploy_timeout=15)
        self.write()
        (self.root / 'tests').mkdir()
        (self.root / 'tests/unit.py').write_text('assert True\n')
        (self.root / 'cookbook.toml').write_text(
            'version=1\nrecipe="react-native"\n[delivery]\ndefinition="delivery.toml"\n'
            '[[tests.unit]]\nrunner="python"\npath="tests/unit.py"\n')
        gate = {'runner': 'ubuntu-24.04', 'timeout_minutes': 15}
        with patch.object(delivery, 'production_gates', return_value=[gate]):
            files = generate.workflow_files(self.root, 'b' * 40)
        deploy = read_yaml(files['.github/workflows/deploy.yml'])
        inputs = deploy['on']['workflow_dispatch']['inputs']
        self.assertIn('release_id', inputs)
        self.assertIn('source_commit', inputs)
        automatic, manual = [deploy['jobs'][key] for key in ['public_verification', 'manual_public_verification']]
        self.assertEqual(manual['uses'], automatic['uses'])
        self.assertEqual(manual['permissions'], {'contents': 'read'})
        self.assertNotIn('secrets', manual)
        self.assertNotIn('needs', manual)
        self.assertIn("inputs.mode == 'verify'", manual['if'])
        self.assertEqual(manual['with']['release-id'], '${{ inputs.release_id }}')
        self.assertEqual(manual['with']['expected-source'], '${{ inputs.source_commit }}')

    def test_stopped_staging_cannot_report_live_success_or_fail_for_a_skipped_public_browser(self):
        # Evaluate the generated finite &&/||/== expression on nonempty status
        # strings, whose truth values match the Actions expression language.
        for public,reporting in [(False,True),(False,False),(True,True)]:
            self.data.pop('report',None)
            if not reporting:
                self.data['report']=False
            if public:
                self.data.update(combined=True,publish_timeout=15,deploy_timeout=15)
            with patch.object(delivery,'production_gates',return_value=(
                    [{'runner':'ubuntu-24.04','timeout_minutes':15}] if public else [])):
                jobs=self.jobs()
            expression=jobs['notify']['with']['deployment']
            self.assertIn('notify.yml@',jobs['notify']['uses'])
            self.assertEqual('report' in jobs, reporting)
            if reporting:self.assertIn('report-failure.yml@',jobs['report']['uses'])
            body=expression.removeprefix('${{ ').removesuffix(' }}').replace('&&','and').replace('||','or')
            for result,phase,browser,expected in [
                    ('success','prepared_stopped','skipped','prepared_stopped'),
                    ('success','prepared_stopped','failure','prepared_stopped'),
                    ('success','ready','success','success'),
                    ('success','ready','skipped','failure' if public else 'success'),
                    ('success','ready','failure','failure' if public else 'success'),
                    ('success','','skipped','failure'),
                    ('failure','ready','success','failure'),
                    ('failure','prepared_stopped','skipped','failure'),
                    ('cancelled','','skipped','cancelled'),
                    ('skipped','','skipped','skipped')]:
                needs=SimpleNamespace(deploy=SimpleNamespace(result=result,outputs=SimpleNamespace(phase=phase)),
                    public_verification=SimpleNamespace(result=browser))
                with self.subTest(public=public,reporting=reporting,result=result,phase=phase,browser=browser):
                    self.assertEqual(eval(body,{'__builtins__':{}},{'needs':needs}),expected)
                    if reporting:
                        needs.validate = SimpleNamespace(result='success')
                        needs.publish = SimpleNamespace(result='success')
                        condition = jobs['report']['if'].replace('&&','and').replace('||','or')
                        condition = condition.replace('!cancelled()', 'not cancelled()').replace('!contains(', 'not contains(')
                        actual = eval(condition, {'__builtins__': {}}, {'needs': needs,
                            'github': SimpleNamespace(ref='refs/heads/main', event_name='push', sha='c'*40),
                            'inputs': SimpleNamespace(source=''), 'always': lambda: True,
                            'cancelled': lambda: result=='cancelled', 'fromJSON': json.loads,
                            'contains': lambda values, item: item in values})
                        self.assertEqual(bool(actual), expected not in {'success','prepared_stopped','cancelled'})
            self.assertEqual(jobs['notify']['with']['deployment-url'],'https://example.test/')
            self.assertEqual(jobs['notify']['permissions'], {'contents': 'read'})
            self.assertEqual(jobs['notify']['secrets'], {'DISCORD_WEBHOOK_URL': '${{ secrets.DISCORD_WEBHOOK_URL }}'})
            self.assertNotIn('notify', jobs['notify']['with'])

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
        self.assertEqual(report['with']['runner'],'ubuntu-24.04')
        self.assertEqual(report['permissions'],{'contents':'read','issues':'write'})
        self.assertNotIn('notify',jobs);self.assertNotIn('secrets',report)
        for value in [{'deploy':101},{'command':5},{'readiness':True}]:
            self.data['step_timeouts']=value;self.write()
            with self.subTest(value=value),self.assertRaises(ValueError):delivery.declaration(self.root,self.config)

    def test_readiness_operations_cannot_select_two_policies(self):
        self.data['readiness_operation']=True;self.write()
        with self.assertRaisesRegex(ValueError,'one native production policy'):delivery.declaration(self.root,self.config)
        self.config['operations']={'definition':'deploy/operations.toml'}
        self.assertTrue(delivery.declaration(self.root,self.config)['readiness_operation'])

    def test_reporting_defaults_follow_CI_runner_and_preserve_explicit_overrides(self):
        for runner in ('ubuntu-24.04', 'blacksmith-4vcpu-ubuntu-2404'):
            with self.subTest(runner=runner):
                self.config['ci'] = {'runner': runner}
                self.data.pop('report', None)
                self.data.pop('notification', None)
                jobs = self.jobs()
                self.assertEqual(jobs['report']['with']['runner'], runner)
                self.assertEqual(jobs['notify']['with']['notify-runner'], runner)
                self.assertEqual(jobs['report']['with']['timeout-minutes'], 10)
                self.assertEqual(jobs['notify']['with']['notify-timeout'], 2)
                self.assertEqual(jobs['report']['permissions'], {'contents': 'read', 'issues': 'write'})
                self.assertEqual(jobs['notify']['permissions'], {'contents': 'read'})
                self.data['report'] = {'runner': 'blacksmith-2vcpu-ubuntu-2404', 'timeout_minutes': 5}
                self.data['notification'] = {'runner': 'ubuntu-24.04', 'timeout_minutes': 3}
                explicit = self.jobs()
                self.assertEqual(explicit['report']['with']['runner'], 'blacksmith-2vcpu-ubuntu-2404')
                self.assertEqual(explicit['notify']['with']['notify-runner'], 'ubuntu-24.04')
                self.assertEqual(explicit['report']['with']['timeout-minutes'], 5)
                self.assertEqual(explicit['notify']['with']['notify-timeout'], 3)

    def test_production_workflow_retains_source_validation_without_duplicate_main_build(self):
        self.write();(self.root/'tests').mkdir();(self.root/'tests/unit.py').write_text('assert True\n')
        (self.root/'cookbook.toml').write_text('version=1\nrecipe="react-native"\n[ci]\npreview_environment="Preview"\n[delivery]\ndefinition="delivery.toml"\n[[tests.unit]]\nrunner="python"\npath="tests/unit.py"\n')
        files=generate.workflow_files(self.root,'b'*40)
        self.assertNotIn('  push:',files['.github/workflows/build-and-test.yml']);self.assertIn('  push:',files['.github/workflows/deploy.yml'])
        self.assertNotIn('  pull_request:',files['.github/workflows/deploy.yml'])
        self.assertNotIn('  merge_group:',files['.github/workflows/deploy.yml'])
        self.assertIn('  merge_group:',files['.github/workflows/build-and-test.yml'])
        self.assertIn('build-and-test-host.yml@',files['.github/workflows/deploy.yml'])
        self.assertIn('mode: full',files['.github/workflows/deploy.yml'])
        self.assertNotIn('merge-gate:',files['.github/workflows/deploy.yml'])
        self.assertIn('deploy.yml@',files['.github/workflows/deploy.yml'])
        self.assertIn('group: deploy-source-${{ github.sha }}-',files['.github/workflows/deploy.yml'])
        self.assertNotIn('group: deploy-production',files['.github/workflows/deploy.yml'])

        ci=read_yaml(files['.github/workflows/build-and-test.yml'])['jobs']['validate']['with']
        production=read_yaml(files['.github/workflows/deploy.yml'])['jobs']['validate']['with']
        self.assertNotIn('preview-environment',production)
        guard=next(line.strip() for line in (SDK_ROOT/'.github/workflows/build-and-test-host.yml').read_text().splitlines()
            if line.strip().startswith('[[ -z "$PREVIEW_ENVIRONMENT"'))
        for event,expected in [('pull_request','Preview'),('push',''),('workflow_dispatch','')]:
            value=ci['preview-environment']
            if value.startswith('${{ '):
                expression=value[4:-3].replace('github.event_name',repr(event)).replace('&&',' and ').replace('||',' or ')
                value=eval(expression,{'__builtins__':{}},{})
            self.assertEqual(value,expected,event)
            for inputs in [ci,production]:
                selected=value if inputs is ci else ''
                result=subprocess.run(['bash','-euc',guard],capture_output=True,text=True,
                    env=dict(os.environ,PREVIEW_ENVIRONMENT=selected,COOKBOOK_MODE=inputs['mode'],
                        GITHUB_EVENT_NAME=event,REQUEST_HEAD='',REQUEST_BASE=''))
                self.assertEqual(result.returncode,0,(event,selected,result.stderr))

    # Inputs declared by the reusable Deploy workflow, read from nicodes/komizo-actions
    # at commit a154d03b26714521d2f616f0c2298c4aaba8e23a (.github/workflows/deliver.yml).
    # This repository cannot read that sibling in CI, so a change there needs this list updated.
    DELIVER_INPUTS = {'cookbook-ref', 'komizo-ref', 'project', 'declaration', 'runner', 'timeout-minutes',
        'deploy-timeout-minutes', 'environment', 'step-timeouts', 'deployment-url', 'notify', 'report',
        'preview-clerk-key'}
    DELIVER_REQUIRED = {'cookbook-ref', 'komizo-ref', 'project', 'declaration', 'runner', 'timeout-minutes',
        'deploy-timeout-minutes', 'environment', 'step-timeouts', 'deployment-url'}
    DELIVER_SECRETS = {'KOMIZO_DEPLOY_KEY', 'BACKUP_S3_ACCESS_KEY', 'BACKUP_S3_SECRET_KEY', 'DISCORD_WEBHOOK_URL'}
    DELIVER_STEP_TIMEOUTS = {'artifact', 'build', 'capture', 'checkout', 'connect', 'deploy', 'download', 'plan',
        'prepare', 'publish', 'readiness', 'setup', 'test', 'upload'}

    def single(self, recipe='react-native', ci='', **changes):
        self.data.update(changes);self.write();(self.root/'tests').mkdir(exist_ok=True)
        (self.root/'tests/unit.py').write_text('assert True\n')
        (self.root/'cookbook.toml').write_text('version=1\nrecipe="'+recipe+'"\n[ci]\nsingle_job=true\n'+ci
            +'[delivery]\ndefinition="delivery.toml"\n[[tests.unit]]\nrunner="python"\npath="tests/unit.py"\n')
        return generate.workflow_files(self.root,'b'*40)

    def test_single_job_deploy_without_backup_is_one_reusable_job(self):
        files=self.single()
        self.assertEqual(set(files),{'.github/workflows/build-and-test.yml','.github/workflows/deploy.yml'})
        self.assertEqual(files['.github/workflows/deploy.yml'],"""# Generated cookbook caller. Change recipe data and regenerate.
name: Deploy
run-name: "Deploy · ${{ inputs.source || github.ref_name }}"
'on':
  push:
    branches: [main]
  workflow_dispatch:
    inputs:
      source:
        description: Exact source commit; defaults to this main revision
        type: string
        default: ''
      cookbook:
        description: Accepted cookbook commit; defaults to the pinned recipe
        type: string
        default: ''
permissions: {}
concurrency:
  group: deploy-source-${{ github.sha }}
  cancel-in-progress: false
jobs:
  deploy:
    name: Deploy
    if: github.ref == 'refs/heads/main' && (inputs.source == '' || inputs.source == github.sha)
    uses: nicodes/komizo-actions/.github/workflows/deliver.yml@""" + 'a'*40 + """
    permissions:
      contents: read
      actions: read
      pull-requests: read
      packages: write
      attestations: read
      id-token: write
      issues: write
    with:
      cookbook-ref: ${{ inputs.cookbook || '""" + 'b'*40 + """' }}
      komizo-ref: """ + 'a'*40 + """
      project: demo
      declaration: delivery.toml
      runner: ubuntu-24.04
      timeout-minutes: 165
      deploy-timeout-minutes: 135
      environment: production
      deployment-url: "https://example.test/"
      step-timeouts: "{\\"artifact\\": 10, \\"build\\": 30, \\"capture\\": 15, \\"checkout\\": 5, \\"connect\\": 3, \\"deploy\\": 100, \\"download\\": 5, \\"plan\\": 5, \\"prepare\\": 20, \\"publish\\": 35, \\"readiness\\": 3, \\"setup\\": 5, \\"test\\": 30, \\"upload\\": 10}"
    secrets:
      KOMIZO_DEPLOY_KEY: ${{ secrets.KOMIZO_DEPLOY_KEY }}
      DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
""")
        self.assertNotIn('  push:',files['.github/workflows/build-and-test.yml'])

    def test_single_job_deploy_with_backup_and_preview_environment_passes_backup_secrets(self):
        (self.root/'backup.toml').write_text('x=1\n')
        files=self.single(ci='preview_environment="Preview"\n',backup='backup.toml',upload_backup=True,combined=True,
            publish_timeout=100,step_timeouts={'prepare':12,'capture':20,'deploy':90},notify=False,report=False)
        deploy=files['.github/workflows/deploy.yml']
        self.assertIn('      BACKUP_S3_ACCESS_KEY: ${{ secrets.BACKUP_S3_ACCESS_KEY }}\n      BACKUP_S3_SECRET_KEY: ${{ secrets.BACKUP_S3_SECRET_KEY }}\n',deploy)
        self.assertNotIn('DISCORD_WEBHOOK_URL',deploy)
        self.assertIn('      timeout-minutes: 130\n',deploy)  # 30 build and test + 100 delivery, one combined job
        self.assertIn('      notify: false\n      report: false\n',deploy)
        self.assertNotIn('preview',deploy.lower())
        self.assertIn('"prepare\\": 12',deploy);self.assertIn('"capture\\": 20',deploy);self.assertIn('"deploy\\": 90',deploy)
        self.assertIn("environment: ${{ github.event_name == 'pull_request' && 'Preview' || '' }}",files['.github/workflows/build-and-test.yml'])

    def test_single_job_deploy_job_deadline_is_capped(self):
        deploy=self.single(ci='timeout_minutes=180\n',deploy_timeout=180,publish_timeout=180)['.github/workflows/deploy.yml']
        self.assertIn('      timeout-minutes: 360\n      deploy-timeout-minutes: 360\n',deploy)

    def test_single_job_deploy_budget_covers_backup_publish_and_deploy_unless_combined(self):
        self.assertIn('      deploy-timeout-minutes: 135\n',self.single()['.github/workflows/deploy.yml'])
        combined=self.single(combined=True,publish_timeout=100)['.github/workflows/deploy.yml']
        self.assertIn('      deploy-timeout-minutes: 100\n',combined)

    def test_single_job_deploy_caller_matches_the_reusable_workflow_interface(self):
        deploy=read_yaml(self.single()['.github/workflows/deploy.yml'])
        job=deploy['jobs']['deploy']
        self.assertEqual(set(deploy['jobs']),{'deploy'})
        self.assertTrue(job['uses'].startswith('nicodes/komizo-actions/.github/workflows/deliver.yml@'))
        self.assertLessEqual(set(job['with']),self.DELIVER_INPUTS)
        self.assertLessEqual(self.DELIVER_REQUIRED,set(job['with']))
        self.assertLessEqual(set(job['secrets']),self.DELIVER_SECRETS);self.assertIn('KOMIZO_DEPLOY_KEY',job['secrets'])
        self.assertEqual(set(json.loads(job['with']['step-timeouts'])),self.DELIVER_STEP_TIMEOUTS)
        self.assertEqual(set(job['permissions']),{'contents','actions','pull-requests','packages','attestations','id-token','issues'})

    def test_single_job_deploy_accepts_report_and_notification_tables_keeping_only_step_deadlines(self):
        files=self.single(report={'runner':'custom-runner','timeout_minutes':20,'step_timeouts':{'report':7,'setup':3}},
            notification={'runner':'custom-runner','timeout_minutes':9,'step_timeouts':{'notify':4}})
        job=read_yaml(files['.github/workflows/deploy.yml'])['jobs']['deploy']
        stages=json.loads(job['with']['step-timeouts'])
        self.assertEqual((stages['report'],stages['notify']),(7,4))
        self.assertEqual(set(stages)-self.DELIVER_STEP_TIMEOUTS,{'report','notify'})
        self.assertEqual(job['with']['runner'],'ubuntu-24.04')
        self.assertNotIn('custom-runner',files['.github/workflows/deploy.yml'])
        self.assertNotIn('notify',job['with']);self.assertNotIn('report',job['with'])
        files=self.single(report={'runner':'custom-runner','timeout_minutes':20},notification={'runner':'custom-runner','timeout_minutes':9})
        self.assertEqual(set(json.loads(read_yaml(files['.github/workflows/deploy.yml'])['jobs']['deploy']['with']['step-timeouts'])),self.DELIVER_STEP_TIMEOUTS)

    def test_single_job_deploy_refuses_production_gates_instead_of_dropping_them(self):
        self.single()
        self.data.update(combined=True,publish_timeout=100);self.write()
        with patch.object(delivery,'production_gates',return_value=[{'runner':'ubuntu-24.04','timeout_minutes':10}]), \
                self.assertRaisesRegex(ValueError,'production gates .* not yet supported in single-job mode'):
            generate.workflow_files(self.root,'b'*40)

    def test_without_the_flag_delivery_callers_keep_their_legacy_shape(self):
        self.write();(self.root/'tests').mkdir();(self.root/'tests/unit.py').write_text('assert True\n')
        legacy=lambda extra:('version=1\nrecipe="react-native"\n'+extra+'[delivery]\ndefinition="delivery.toml"\n[[tests.unit]]\nrunner="python"\npath="tests/unit.py"\n')
        (self.root/'cookbook.toml').write_text(legacy(''))
        before=generate.workflow_files(self.root,'b'*40)
        (self.root/'cookbook.toml').write_text(legacy('[ci]\nsingle_job=false\n'))
        self.assertEqual(generate.workflow_files(self.root,'b'*40),before)
        deploy=before['.github/workflows/deploy.yml']
        self.assertIn('deploy.yml@'+'a'*40,deploy);self.assertIn('  publish:\n',deploy);self.assertNotIn('deliver.yml',deploy)
        self.assertIn('  validate:\n',deploy)
