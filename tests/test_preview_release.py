"""Preview public origins are frozen into the tested full application graph."""
from source_fixture import SDK_ROOT, write_source
import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import preview_release


class PreviewRelease(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        (self.root/'deploy').mkdir()
        self.policy={'project':'demo','environment':{'APP_ENVIRONMENT':'preview'},
            'origin_variables':{'APP_ALLOWED_ORIGINS':['app'],'CLERK_AUTHORIZED_PARTIES':['app']}}
        write_source(self.root/'deploy/preview.toml',self.policy)
        self.config={'preview':{'definition':'deploy/preview.toml'}}
        self.environment={'COOKBOOK_PREVIEW_NUMBER':'42','COOKBOOK_PREVIEW_APP_ORIGIN':'https://pr-42.example.test',
            'COOKBOOK_PREVIEW_API_ORIGIN':'https://pr-42-api.example.test'}
        self.release={'application':'demo','providers':{'cookbook_revision':'b'*40,'tools_revision':'c'*40,'core_revision':'d'*40},
            'workload':{'services':{'api':{'artifact':'api','environment_files':['runtime'],'needs':{'migrate':'completed'}},
                'postgres':{'artifact':'postgres','environment_files':['postgres-owner'],'volumes':[{'name':'data','target':'/data','read_only':False}]}},
                'jobs':{'migrate':{'artifact':'api','environment_files':['migration'],'needs':{'postgres':'healthy'}}},
                'assets':{'web':{'artifact':'web','serving':{}}},'volumes':{'data':{}},'networks':{'database':{'internal':True}},
                'routes':[{'host':'app.example.test','path':'/api/*','strip_prefix':False,'target':{'kind':'service','name':'api','port':8080}},
                    {'host':'app.example.test','path':'/','target':{'kind':'asset','name':'web'}},
                    {'host':'api.example.test','path':'/','target':{'kind':'service','name':'api','port':8080}}],
                'readiness':[{'url':'https://api.example.test/readyz','status':200},{'url':'https://app.example.test/release.json','status':200,'contains':'{revision}'}]}}

    def project(self):return preview_release.project(self.root,self.config,self.environment,self.release)

    def test_complete_database_migration_graph_and_provider_facts_remain_explicit(self):
        before=copy.deepcopy(self.release);result=self.project();workload=result['workload']
        self.assertEqual(self.release,before)
        self.assertEqual(result['providers'],before['providers'])
        self.assertEqual(workload['services']['postgres'],before['workload']['services']['postgres'])
        self.assertEqual(workload['jobs'],before['workload']['jobs'])
        self.assertEqual(workload['volumes'],{'data':{}})
        self.assertEqual(workload['services']['api']['environment']['APP_ALLOWED_ORIGINS'],'https://pr-42.example.test')
        self.assertEqual(workload['services']['api']['environment']['APP_ENVIRONMENT'],'preview')
        self.assertEqual(workload['services']['api']['environment_files'],['runtime'])
        self.assertFalse(workload['routes'][0]['strip_prefix'])
        self.assertEqual(workload['readiness'][0]['url'],'https://pr-42-api.example.test/readyz')
        self.assertEqual(workload['readiness'][1]['contains'],'{revision}')

    def test_static_application_has_zero_containers_and_only_resolved_asset_path(self):
        workload=self.release['workload'];workload['services']={};workload['jobs']={};workload['networks']={};workload['volumes']={}
        workload['routes']=[workload['routes'][1]];workload['readiness']=workload['readiness'][1:]
        self.environment['COOKBOOK_PREVIEW_APP_ORIGIN']='https://preview.example.test/pr-42'
        result=self.project()['workload']
        self.assertEqual(result['services'],{});self.assertEqual(result['jobs'],{})
        self.assertEqual([item['path'] for item in result['routes']],['/pr-42','/pr-42/*'])
        self.assertTrue(all(item['strip_prefix'] for item in result['routes']))
        self.assertEqual(result['readiness'][0]['url'],'https://preview.example.test/pr-42/release.json')

    def test_unplanned_or_credential_bearing_origins_are_refused(self):
        for value in ['',None,'http://preview.example.test','https://user:password@preview.example.test','https://preview.example.test/?token=value']:
            self.environment['COOKBOOK_PREVIEW_APP_ORIGIN']=value
            with self.subTest(value=value),self.assertRaises(ValueError):self.project()
        self.environment['COOKBOOK_PREVIEW_APP_ORIGIN']='https://pr-42.example.test'
        self.environment['COOKBOOK_PREVIEW_API_ORIGIN']='https://pr-42.example.test'
        with self.assertRaisesRegex(ValueError,'distinct PR subdomains'):self.project()


if __name__=='__main__':unittest.main()
