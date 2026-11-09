from source_fixture import TOOLS_ROOT
from source_fixture import SDK_ROOT
import importlib.util
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS=SDK_ROOT
sys.path.insert(0,str(TOOLS/'cookbook'))
import preview_workflows as preview
import mise_entrypoint
from source_fixture import read_source, write_source, dump_source
spec=importlib.util.spec_from_file_location('preview_release',TOOLS_ROOT/'helpers/release.py')
release=importlib.util.module_from_spec(spec);spec.loader.exec_module(release)


class PreviewRelationship(unittest.TestCase):
    def setUp(self):
        self.head,self.base='a'*40,'b'*40
        self.environment={'COOKBOOK_PREVIEW_NUMBER':'42','REQUEST_HEAD':self.head,'REQUEST_BASE':self.base,
            'GITHUB_REPOSITORY':'owner/demo','GITHUB_EVENT_NAME':'issue_comment','GITHUB_REF':'refs/heads/main',
            'GITHUB_SHA':self.base,'GITHUB_WORKFLOW_SHA':self.base,
            'GITHUB_WORKFLOW_REF':'owner/demo/.github/workflows/pr-preview.yml@refs/heads/main'}
        self.event={'action':'created','issue':{'number':42,'pull_request':{'url':'fixture'}},
            'comment':{'body':' /preview \n','author_association':'OWNER'}}
        self.request={'number':42,'state':'open','draft':False,'author_association':'OWNER','user':{'login':'owner'},
            'head':{'sha':self.head,'repo':{'full_name':'owner/demo'}},
            'base':{'sha':self.base,'ref':'main','repo':{'full_name':'owner/demo'}}}

    def relationship(self):
        return release.preview_relationship(self.head,self.event,self.environment,lambda *_:self.request)

    def test_comments_and_labeled_target_events_bind_exact_head_base_without_claiming_merge_bytes(self):
        value=self.relationship()
        self.assertEqual(value['head'],self.head)
        self.assertEqual(value['controller_commit'],self.base)
        self.environment['GITHUB_EVENT_NAME']='pull_request_target'
        self.event={'action':'synchronize','pull_request':{'number':42,'head':{'sha':self.head},'labels':[{'name':'preview'}]}}
        self.assertEqual(self.relationship(),value)

    def test_untrusted_event_wrong_workflow_candidate_bytes_and_stale_heads_are_refused(self):
        for key,value in [('GITHUB_EVENT_NAME','pull_request'),('GITHUB_REF','refs/pull/42/merge'),
                          ('GITHUB_WORKFLOW_SHA','c'*40),('REQUEST_HEAD','c'*40),
                          ('GITHUB_WORKFLOW_REF','owner/demo/.github/workflows/other.yml@refs/heads/main')]:
            old=self.environment[key];self.environment[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):self.relationship()
            self.environment[key]=old
        for path,value in [('head','c'*40),('base','c'*40)]:
            old=self.request[path]['sha'];self.request[path]['sha']=value
            with self.subTest(path=path),self.assertRaisesRegex(ValueError,'stale'):self.relationship()
            self.request[path]['sha']=old
        self.event['comment']['author_association']='CONTRIBUTOR'
        with self.assertRaises(ValueError):self.relationship()

    def test_fork_closed_draft_and_untrusted_author_cannot_gain_publication(self):
        for key,value in [('state','closed'),('draft',True),('author_association','CONTRIBUTOR')]:
            old=self.request[key];self.request[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):self.relationship()
            self.request[key]=old
        self.request['head']['repo']['full_name']='fork/demo'
        with self.assertRaises(ValueError):self.relationship()

    def test_normal_main_relationship_remains_the_existing_shape(self):
        with patch.dict(os.environ,{'GITHUB_SHA':self.head},clear=True):
            self.assertEqual(release.source_relationship(self.head),dict(source_commit=self.head,
                workflow_commit=self.head,pull_request_head=None,pull_request_base=None,pull_request_merge=None))


class PreviewDeclaration(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='preview source ')
        self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        (self.root/'deploy').mkdir();(self.root/'.github/workflows').mkdir(parents=True)
        self.config={'version':1,'recipe':'react-native','ci':{'runner':'ubuntu-24.04','timeout_minutes':90,'preview_environment':'Preview'},
            'components':{'app':{'preview_environment':{'EXPO_PUBLIC_API_URL':'/api'},
                'preview_variables':{'EXPO_PUBLIC_CLERK_PUBLISHABLE_KEY':'COOKBOOK_PREVIEW_CLERK_KEY'}}},
            'artifacts':{},'preview':{'definition':'deploy/preview.toml'}}
        self.config['tests']={'unit':[{'runner':'python','path':'tests/check.py'}]}
        (self.root/'tests').mkdir()
        (self.root/'tests/check.py').write_text('raise RuntimeError("source inspection must not execute")\n')
        self.data={'version':1,'project':'demo','komizo_ref':'c'*40,'ttl_seconds':259200,
            'runner':'ubuntu-24.04','up_timeout_minutes':35,'down_timeout_minutes':10,'publish_timeout_minutes':10,
            'origin_variables':{'ALLOWED_ORIGINS':['app','api'],'CLERK_AUTHORIZED_PARTIES':['app']},
            'health_paths':{'app':'/','api':'/readyz'},'secrets':['CLERK_SECRET_KEY']}
        self.definition={'project':'demo','image_base':'ghcr.io/owner/demo','tests':['lint','unit','security','integration','e2e'],
            'inputs':{'images':['ghcr.io/owner/demo-'+name+':{revision}' for name in ['api','postgres']]}}
        self.backend=True
        self.write()

    def write(self):
        write_source(self.root/'deploy/preview.toml', self.data)
        workload={'services':{'api':{'artifact':'api'}} if self.backend else {},'jobs':{},'assets':{'web':{'artifact':'web','serving':{}}},
            'networks':{},'volumes':{},'readiness':[{'url':'https://app.example.test/','status':200}],
            'routes':[{'host':'app.example.test','path':'/','target':{'kind':'asset','name':'web'}}]}
        if self.backend:
            workload['routes'].insert(0,{'host':'app.example.test','path':'/api/*','strip_prefix':True,
                'target':{'kind':'service','name':'api','port':8080}})
            workload['routes'].append({'host':'api.example.test','path':'/','target':{'kind':'service','name':'api','port':8080}})
        write_source(self.root/'deploy/application.toml',{'kind':'komizo.application','version':1,'application':'demo','workload':workload})

        raw=dump_source(self.config).encode()
        (self.root/'cookbook.toml').write_bytes(raw)
        (self.root/'mise.toml').write_text(mise_entrypoint.render('[tools]\npython="3.13.11"\n',
            'd'*40,self.config['recipe'],raw,['lint','unit','security'],[]))

    def declaration(self):
        with patch.object(preview.images,'definition',return_value=self.definition):
            return preview.declaration(self.root,self.config)

    def test_frontend_api_base_requires_its_authored_route_without_inventing_strip_semantics(self):
        path=self.root/'deploy/application.toml';original=read_source(path)
        for mutate in [lambda routes:routes.pop(0),lambda routes:routes[0].update(host='elsewhere.example.test'),
                lambda routes:routes[0].update(path='/other/*'),
                lambda routes:routes[0].update(target={'kind':'asset','name':'web'})]:
            changed=copy.deepcopy(original);mutate(changed['workload']['routes']);write_source(path,changed)
            with self.subTest(mutate=mutate),self.assertRaisesRegex(ValueError,'explicit app-host /api'):
                self.declaration()
        original['workload']['routes'][0]['strip_prefix']=False;write_source(path,original)
        self.assertTrue(self.declaration()['backend'])

    def test_native_effect_data_has_no_commands_unbounded_credentials_or_production_origin_override(self):
        self.assertEqual(self.declaration()['shipping_components'],['api','postgres'])
        for key,value in [('components',['other']),('secrets',['CLERK_SECRET_KEY','DATABASE_PASSWORD']),
                          ('environment',{'ALLOWED_ORIGINS':'https://fixed.invalid'}),
                          ('environment',{'GH_TOKEN':'override'}),('up_timeout_minutes',36),
                          ('commands',['true']),('health_paths',{'app':'/','api':'/readyz?token=leak'})]:
            old=self.data.get(key);self.data[key]=value;self.write()
            with self.subTest(key=key),self.assertRaises(ValueError):self.declaration()
            if old is None:self.data.pop(key)
            else:self.data[key]=old
            self.write()

    def test_static_godot_preview_has_readonly_full_source_proof_and_no_tenant_settings(self):
        self.config['recipe']='godot-game'
        self.config['ci'].pop('preview_environment')
        self.backend=False;self.definition['inputs']['images']=[]
        self.data.update(origin_variables={},health_paths={'app':'/'},secrets=[])
        self.write()
        value=self.declaration()
        self.assertFalse(value['backend'])
        self.assertEqual(value['origin_layout'],'resolved')
        self.assertEqual(value['secrets'],[])
        with patch.object(preview.images,'definition',return_value=self.definition):
            body=preview.workflow_files(self.root,'d'*40,self.config)['.github/workflows/pr-preview.yml']
        self.assertIn('      recipe: godot-game',body)
        self.assertIn('preview-number:',body)
        self.assertNotIn('secrets.CLERK_SECRET_KEY',body)
        self.assertNotIn('preview-public-key:',body)

    def test_backend_godot_keeps_readonly_preview_configuration_and_original_host_deadlines(self):
        self.config['recipe']='godot-game'
        self.config['ci'].pop('preview_environment')
        self.config['ci']['timeout_minutes']=45
        self.data.update(up_timeout_minutes=25,down_timeout_minutes=25,
            clerk_urls={'CLERK_ISSUER':'issuer','CLERK_JWKS_URL':'jwks'})
        self.write()
        self.assertEqual(self.declaration()['clerk_urls'],self.data['clerk_urls'])
        with patch.object(preview.images,'definition',return_value=self.definition):
            body=preview.workflow_files(self.root,'d'*40,self.config)['.github/workflows/pr-preview.yml']
        full=body.split('  full:\n',1)[1].split('  publish:\n',1)[0]
        self.assertIn('      preview-public-key: true\n',full)
        self.assertIn('      timeout-minutes: 45\n',full)
        self.assertNotIn('secrets:',full)
        for action in ['up','down']:
            self.assertIn('      timeout-minutes: 25\n',body.split('  '+action+':\n',1)[1])
        self.data['down_timeout_minutes']=36;self.write()
        with self.assertRaises(ValueError):self.declaration()

    def test_derived_provider_urls_are_closed_development_carriers(self):
        for value in [{'CLERK_ISSUER':'command'}, {'OTHER':'issuer'}, {'CLERK_ISSUER':[]}, []]:
            self.data['clerk_urls']=value;self.write()
            with self.subTest(value=value),self.assertRaises(ValueError):self.declaration()
        self.data['clerk_urls']={'CLERK_ISSUER':'issuer'}
        self.data['environment']={'CLERK_ISSUER':'https://fixed.invalid'};self.write()
        with self.assertRaises(ValueError):self.declaration()
        self.backend=False;self.definition['inputs']['images']=[]
        self.data.update(secrets=[],origin_variables={},health_paths={'app':'/'},environment={})
        self.write()
        with self.assertRaises(ValueError):self.declaration()

    def test_godot_preview_public_key_is_opt_in_and_waits_for_the_trusted_controller_plan(self):
        worker=(TOOLS/'.github/workflows/cookbook-godot.yml').read_text()
        build=worker.split('  build:\n',1)[1].split('  units:\n',1)[0]
        self.assertIn('    needs: plan\n',build)
        self.assertIn("inputs.preview-public-key && inputs.preview-number != '' && inputs.preview-environment == 'Preview'",build)
        self.assertIn('vars.CLERK_PUBLISHABLE_KEY',build)
        self.assertNotIn('secrets.',build)
        self.assertIn('"$GITHUB_WORKFLOW_REF" == "$GITHUB_REPOSITORY/.github/workflows/pr-preview.yml@refs/heads/main"',worker)
        self.assertIn('if [[ "$PREVIEW_PUBLIC_KEY" == true ]]; then [[ -n "$COOKBOOK_PREVIEW_NUMBER" ]]; fi',worker)
        for name in ['cookbook-ci.yml','cookbook-godot.yml']:
            self.assertIn('      preview-public-key:\n        type: boolean\n        default: false\n',
                (TOOLS/'.github/workflows'/name).read_text())

    def test_generated_controller_keeps_authority_split_and_down_independent(self):
        with patch.object(preview.images,'definition',return_value=self.definition):
            body=preview.workflow_files(self.root,'d'*40,self.config)['.github/workflows/pr-preview.yml']
            (self.root/'.github/workflows/pr-preview.yml').write_text(body)
            self.assertTrue(preview.auth_contract(self.root,self.config)['present'])
            body=body.replace('      merge-gate: false','      merge-gate: true')
            (self.root/'.github/workflows/pr-preview.yml').write_text(body)
            with self.assertRaisesRegex(ValueError,'reviewed'):preview.auth_contract(self.root,self.config)
        self.assertIn('  pull_request_target:',body)
        self.assertNotIn('  pull_request:',body)
        full=body.split('  full:\n',1)[1].split('  publish:\n',1)[0]
        self.assertNotIn('secrets:',full)
        self.assertNotIn('packages: write',full)
        self.assertIn('preview-number:',full)
        self.assertIn('  down:\n    needs: request',body)
        self.assertNotIn('secrets.CLERK_SECRET_KEY',body)
        self.assertNotIn('secrets.OPENROUTER_API_KEY',body)
        self.assertIn('preview-noop-',body)

    def test_publication_source_is_never_substituted_with_a_synthetic_merge(self):
        source=TOOLS_ROOT/'helpers/release.py'
        environment=PreviewRelationship();environment.setUp()
        with tempfile.TemporaryDirectory() as directory:
            event=Path(directory)/'event.json';event.write_text(json.dumps(environment.event))
            values=environment.environment|{'GITHUB_EVENT_PATH':str(event)}
            with patch.dict(os.environ,values,clear=True),patch.object(release,'preview_pr',return_value=environment.request):
                relationship=release.source_relationship(environment.head)
                self.assertEqual(relationship['source_commit'],environment.head)
                self.assertEqual(relationship['workflow_commit'],environment.base)
                self.assertIsNone(relationship['pull_request_merge'])


if __name__=='__main__':unittest.main()
