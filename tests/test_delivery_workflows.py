from source_fixture import SDK_ROOT
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import delivery_workflows as delivery
import generate
from source_fixture import read_source, write_source, dump_source


class DeliveryWorkflows(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = {'delivery': {'definition': 'delivery.toml'}}
        self.data = dict(version=1, project='demo', komizo_ref='a' * 40,
                         publish_runner='ubuntu-24.04', publish_timeout=35,
                         deploy_runner='ubuntu-24.04', deploy_timeout=100,
                         health_urls=['https://example.test/readyz'], notification_url='https://example.test/',
                         secrets={'KOMIZO_SECRET_DATABASE_URL': 'RUNTIME_DATABASE_URL'},
                         variables={'KOMIZO_SECRET_PUBLIC_KEY': 'PUBLIC_KEY'})

    def write(self):
        write_source(self.root / 'delivery.toml', self.data)

    def test_same_run_publication_and_serialized_host_after_validation(self):
        self.write()
        result = delivery.jobs(self.root, self.config, 'b' * 40)
        self.assertIn('needs: validate', result)
        self.assertIn('needs: publish', result)
        self.assertIn('group: deploy-production', result)
        self.assertIn('cancel-in-progress: false', result)
        self.assertIn('inputs.source == github.sha', result)
        self.assertIn('KOMIZO_SECRET_DATABASE_URL: ${{ secrets.RUNTIME_DATABASE_URL }}', result)
        self.assertIn('KOMIZO_SECRET_PUBLIC_KEY: ${{ vars.PUBLIC_KEY }}', result)
        self.assertNotIn('secrets: inherit', result)
        self.assertNotIn('mise run', result)
        self.assertNotIn('make build', result)

    def test_rejects_unpinned_or_interpolated_authority(self):
        for key, value in [('komizo_ref', 'main'), ('deploy_runner', '${{ vars.RUNNER }}'),
                           ('deploy_timeout', 181), ('health_urls', ['http://example.test/']),
                           ('secrets', {'KOMIZO_SECRET_DB': '${{ secrets.DB }}'}),
                           ('variables', {'DATABASE_URL': 'DB'})]:
            original = self.data[key]
            self.data[key] = value
            self.write()
            with self.subTest(key=key), self.assertRaises(ValueError):
                delivery.declaration(self.root, self.config)
            self.data[key] = original

    def test_backup_credentials_only_enter_explicit_delivery(self):
        self.write()
        self.assertNotIn('BACKUP_S3_ACCESS_KEY', delivery.jobs(self.root, self.config, 'b' * 40))
        self.data['upload_backup'] = True
        self.write()
        result = delivery.jobs(self.root, self.config, 'b' * 40)
        self.assertIn('BACKUP_S3_ACCESS_KEY: ${{ secrets.BACKUP_S3_ACCESS_KEY }}', result)
        before_deploy = result.split('  deploy:')[0]
        self.assertNotIn('secrets.', before_deploy)
        preparation,upload=result.split('      - uses: nicodes/komizo-actions/application-backup-upload@',1)
        self.assertNotIn('BACKUP_S3_ACCESS_KEY',preparation)
        self.assertIn('BACKUP_S3_ACCESS_KEY',upload)

    def test_production_retains_host_cd_identity_without_duplicate_main_builds(self):
        self.write()
        (self.root / 'tests').mkdir()
        (self.root / 'tests/unit.py').write_text('assert 1 + 1 == 2\n')
        (self.root / 'cookbook.toml').write_text('version=1\nrecipe="react-native"\n[delivery]\ndefinition="delivery.toml"\n[[tests.unit]]\nrunner="python"\npath="tests/unit.py"\n')
        files = generate.workflow_files(self.root, 'b' * 40)
        self.assertNotIn('  push:', files['.github/workflows/ci.yml'])
        self.assertNotIn('  pull_request:', files['.github/workflows/cd.yml'])
        self.assertIn('  push:', files['.github/workflows/cd.yml'])
        self.assertIn('merge-gate: false', files['.github/workflows/cd.yml'])
        self.assertNotIn('application-delivery', files['.github/workflows/ci.yml'])
        self.assertIn('application-delivery', files['.github/workflows/cd.yml'])
        self.assertIn('group: deploy-source-${{ github.sha }}-', files['.github/workflows/cd.yml'])
        self.assertIn('group: ci-${{ github.ref }}-', files['.github/workflows/ci.yml'])

    def test_combined_delivery_keeps_one_existing_job_deadline(self):
        self.data.update(combined=True, publish_timeout=15, deploy_timeout=15)
        self.write()
        result = delivery.jobs(self.root, self.config, 'b' * 40)
        self.assertNotIn('  publish:\n', result)
        self.assertIn('needs: validate', result)
        self.assertIn('timeout-minutes: 15', result)
        self.assertIn('application-publication@', result)
        self.assertIn('application-delivery@', result)
        self.assertLess(result.index('application-publication@'), result.index('application-delivery@'))
        self.data['publish_timeout'] = 35
        self.write()
        with self.assertRaises(ValueError):
            delivery.declaration(self.root, self.config)

    def test_source_bound_release_health_assertion_is_preserved(self):
        self.data['health_urls'].append(dict(url='https://example.test/release.json', contains='{revision}'))
        self.write()
        self.assertEqual(delivery.declaration(self.root, self.config)['health_urls'][1]['contains'], '{revision}')

    def test_native_environment_deadlines_and_absent_notification_are_preserved(self):
        self.data.update(environment='Production', notify=False,
            step_timeouts={'deploy':25,'readiness':5,'diagnostics':3},
            report={'runner':'ubuntu-24.04','timeout_minutes':5,
                    'step_timeouts':{'checkout':3,'setup':5,'report':3}})
        self.write()
        text=delivery.jobs(self.root,self.config,'b'*40)
        self.assertIn('    environment: Production',text)
        self.assertIn('plan-only: true',text)
        self.assertIn('sdk-prepared: true',text)
        self.assertIn('        timeout-minutes: 25',text)
        self.assertIn('notify: false',text)
        self.assertNotIn('DISCORD_WEBHOOK_URL',text)
        self.assertIn('report-runner: ubuntu-24.04',text)
        for changes in [{'deploy':101},{'command':5},{'readiness':True}]:
            self.data['step_timeouts']=changes
            self.write()
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                delivery.declaration(self.root,self.config)

    def test_readiness_and_operational_readiness_cannot_select_two_policies(self):
        self.data['readiness_operation']=True
        self.write()
        with self.assertRaisesRegex(ValueError,'one native production policy'):
            delivery.declaration(self.root,self.config)
        self.config['operations']={'definition':'deploy/operations.toml'}
        self.assertTrue(delivery.declaration(self.root,self.config)['readiness_operation'])

    def test_scoped_profile_keeps_generation_and_never_transports_profile_values(self):
        self.data.update(service_env_profile='serving-postgres-v2',expected_generation='c'*32,secrets={},variables={})
        self.write()
        text=delivery.jobs(self.root,self.config,'b'*40)
        self.assertIn('service-env-profile: serving-postgres-v2',text)
        self.assertIn('expected-generation: '+'c'*32,text)
        self.assertNotIn('KOMIZO_SECRET_',text)
        import copy
        original=copy.deepcopy(self.data)
        for changes in [dict(expected_generation='wrong'),dict(service_env_profile='../other'),
                        dict(secrets={'KOMIZO_SECRET_DB':'DB'}),dict(optional_secrets={'KOMIZO_SECRET_TOKEN':'TOKEN'}),
                        dict(service_env_profile='')]:
            self.data=original|changes;self.write()
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                delivery.declaration(self.root,self.config)

    def test_delivery_verification_and_notification_keep_distinct_native_policy(self):
        self.data.update(validation_timeout_minutes=60,notification=dict(runner='ubuntu-slim',timeout_minutes=2,
            step_timeouts={'notify':1},environment='Production'))
        self.write()
        (self.root/'tests').mkdir()
        (self.root/'tests/unit.py').write_text('assert 2+2==4\n')
        (self.root/'cookbook.toml').write_text('version=1\nrecipe="react-native"\n[ci]\ntimeout_minutes=45\n[delivery]\ndefinition="delivery.toml"\n[[tests.unit]]\nrunner="python"\npath="tests/unit.py"\n')
        files=generate.workflow_files(self.root,'b'*40)
        self.assertIn('      timeout-minutes: 45',files['.github/workflows/ci.yml'])
        self.assertIn('      timeout-minutes: 60',files['.github/workflows/cd.yml'])
        self.assertIn('      environment: Production',files['.github/workflows/cd.yml'])

    def test_historical_dispatch_skips_new_builds_and_keeps_scoped_host_and_notification_policy(self):
        self.data.update(historical_deploy=True,service_env_profile='serving-postgres-v2',expected_generation='c'*32,
                         secrets={},variables={},report=False)
        self.write()
        (self.root/'tests').mkdir()
        (self.root/'tests/unit.py').write_text('assert 2+2==4\n')
        (self.root/'cookbook.toml').write_text('version=1\nrecipe="react-native"\n[delivery]\ndefinition="delivery.toml"\n[[tests.unit]]\nrunner="python"\npath="tests/unit.py"\n')
        files=generate.workflow_files(self.root,'b'*40)
        text=files['.github/workflows/cd.yml']
        self.assertIn('      version:',text)
        self.assertNotIn('      version:',files['.github/workflows/ci.yml'])
        self.assertIn("    if: inputs.version == '' || inputs.version == github.sha",text)
        normal,rollback=text.split('\n  rollback:',1)
        self.assertIn("&& (inputs.version == '' || inputs.version == github.sha)",normal)
        self.assertIn("inputs.version != '' && inputs.version != github.sha",rollback)
        self.assertIn('cookbook-historical-deploy.yml@',rollback)
        self.assertIn('cookbook-notify.yml@',text)
        self.assertNotIn('issues: write',text)
        self.assertIn('needs.rollback.outputs.version || github.sha',text)
        self.assertEqual(set(delivery.historical_deadlines(self.data)),
                         {'checkout','setup','plan','registry','inspect','deploy','diagnostics'})
        self.data.pop('service_env_profile');self.data.pop('expected_generation');self.write()
        with self.assertRaisesRegex(ValueError,'fixed scoped profile'):
            delivery.declaration(self.root,self.config)

    def test_setup_connection_capture_and_download_have_actual_workflow_deadlines(self):
        (self.root/'deploy').mkdir()
        (self.root/'deploy/backup.toml').write_text('')
        self.data.update(backup='deploy/backup.toml',upload_backup=True)
        self.write()
        text=delivery.jobs(self.root,self.config,'b'*40)
        for action,minutes in [('nicodes/tools/setup@',5),('nicodes/komizo-actions/connect@',3),
                               ('nicodes/komizo-actions/application-backup-export@',15),
                               ('nicodes/komizo-actions/application-backup-upload@',10),
                               ('actions/download-artifact@',5)]:
            block=text.split('      - uses: '+action,1)[1].split('      - uses:',1)[0]
            with self.subTest(action=action):self.assertIn('        timeout-minutes: '+str(minutes)+'\n',block)
        admission=text.split('      - uses: nicodes/komizo-actions/application-delivery@',1)[1].split('      - uses:',1)[0]
        self.assertNotIn('secrets.',admission)
        capture=text.split('      - uses: nicodes/komizo-actions/application-backup-export@',1)[1].split('      - uses:',1)[0]
        self.assertNotIn('BACKUP_S3_',capture)


if __name__ == '__main__':
    unittest.main()
