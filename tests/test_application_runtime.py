"""Preserve accepted resource limits, isolation, admission order and owned cleanup."""
from pathlib import Path
import copy
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import os
SDK_ROOT=Path(os.environ.get('COOKBOOK_TEST_SOURCE',Path(__file__).parents[1]))
sys.path.insert(0,str(SDK_ROOT/'cookbook'))
if os.environ.get('COOKBOOK_TEST_SOURCE'):
    import sdk_identity
    sdk_identity.verify(SDK_ROOT,os.environ['COOKBOOK_TEST_REF'])
import application_runtime as runtime


def release():
    limits={'memory_bytes':128<<20,'memory_swap_bytes':128<<20,'milli_cpus':500,'pids':64}
    api={'artifact':'api','user':'10001:10001','limits':limits,'networks':['public','database'],
        'tmpfs':{'/tmp':{'size_bytes':16<<20,'mode':0o1777}},'needs':{'postgres':'healthy','migrate':'completed'},
        'ulimits':{'nofile':{'soft':4096,'hard':4096}},'entrypoint':['/app/api'],'command':['serve']}
    postgres={'artifact':'postgres','user':'0:0','limits':limits,'networks':['database'],
        'capabilities':['CHOWN','SETUID','SETGID'],
        'environment':{'POSTGRES_DB':'app','POSTGRES_USER':'app_owner'},'health':{'command':['pg_isready','-h','127.0.0.1']}}
    return {'application':'app','artifacts':{'api':{'kind':'image','config_digest':'sha256:'+'a'*64},
        'postgres':{'kind':'image','config_digest':'sha256:'+'b'*64}},
        'workload':{'services':{'postgres':postgres,'api':api},'jobs':{'migrate':copy.deepcopy(api)|{'networks':['database'],'timeout_seconds':60,'needs':{'postgres':'healthy'}}},
            'volumes':{},'networks':{'public':{'internal':False},'database':{'internal':True}}}}


def profile():
    return {'project':'app','runtime_role':'app_runtime','api_service':'api','api_port':8080,'health_path':'/readyz',
        'dsn_transport':'environment','dsn_variable':'RUNTIME_DATABASE_URL','env':{},
        'bootstrap':{'job':'migrate','environment':{},'dsn_variable':'OWNER_DATABASE_URL','runtime_dsn_variable':'RUNTIME_DATABASE_URL'}}


class AcceptedRuntime(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup);self.root=Path(self.directory.name)

    def selected(self,value=None):
        with patch.object(runtime.deployment,'verify',return_value=value or release()):
            value=runtime.Runtime(self.root,{}, {},profile())
        value.directory=self.root;value.networks={'public':'owned-public','database':'owned-database'}
        return value

    def test_packet_and_full_job_order_admit_before_any_container_or_registry_effect(self):
        with patch.object(runtime.deployment,'verify',side_effect=ValueError('rejected packet')),patch.object(runtime.native_runtime,'run') as run:
            with self.assertRaises(ValueError):runtime.Runtime(self.root,{}, {},profile())
            run.assert_not_called()
        value=release();value['workload']['jobs']['unclaimed']=value['workload']['jobs']['migrate']
        with patch.object(runtime.native_runtime,'run') as run,self.assertRaisesRegex(ValueError,'every actual'):
            self.selected(value)
        run.assert_not_called()

    def test_declared_limits_uid_commands_tmpfs_and_loopback_are_exact_and_passwords_stay_in_private_files(self):
        value=self.selected();record=value.release['workload']['services']['api']
        record['stop_timeout_seconds']=660
        record['sysctls']={'net.ipv4.ip_local_port_range':'1024 65535'}
        args=value.arguments('api',record,{'RUNTIME_DATABASE_URL':'postgres://runtime:private-fixture-password@postgres/app'},publish=True)
        self.assertEqual(args[0],'create');self.assertIn('--pull=never',args);self.assertIn('--read-only',args)
        self.assertEqual(args[args.index('--user')+1],'10001:10001')
        self.assertIn('--read-only-tmpfs=false',args);self.assertIn('--image-volume=ignore',args)
        self.assertEqual(args[args.index('--memory')+1],str(128<<20));self.assertEqual(args[args.index('--memory-swap')+1],str(128<<20))
        self.assertEqual(args[args.index('--cpus')+1],'0.5');self.assertEqual(args[args.index('--pids-limit')+1],'64')
        self.assertIn('nofile=4096:4096',args);self.assertIn('127.0.0.1::8080',args)
        self.assertEqual(args[args.index('--stop-timeout')+1],'660')
        self.assertEqual(args[args.index('--sysctl')+1],'net.ipv4.ip_local_port_range=1024 65535')
        self.assertEqual([args[i+1] for i,arg in enumerate(args) if arg=='--network'],['owned-public','owned-database'])
        self.assertNotIn('private-fixture-password',str(args));self.assertIn('noexec',args[args.index('--tmpfs')+1]);self.assertNotIn('uid=',args[args.index('--tmpfs')+1])
        envfile=Path(args[args.index('--env-file')+1]);self.assertEqual(envfile.stat().st_mode&0o777,0o600)
        self.assertIn('private-fixture-password',envfile.read_text());self.assertEqual(args[-2:],['sha256:'+'a'*64,'serve'])

    def test_failed_actual_migration_never_creates_the_api(self):
        value=self.selected();created=[]
        def create(name,*_args,**_kwargs):created.append(name);return ('a' if name=='postgres' else 'b')*64
        with patch.object(value.metrics,'start'),patch.object(value,'create',side_effect=create),patch.object(value,'wait'),\
             patch.object(value,'execute',return_value=subprocess.CompletedProcess([],0,'1\n','')):
            with self.assertRaisesRegex(ValueError,'migration job failed'):value.boot()
        self.assertEqual(created,['postgres','migrate'])

    def test_migration_and_serving_use_one_runtime_credential_epoch(self):
        for transport in ['environment','file']:
            selected=profile()|{'dsn_transport':transport,'dsn_file':'/run/secrets/runtime.url',
                'dsn_variable':'RUNTIME_DATABASE_URL_FILE' if transport=='file' else 'RUNTIME_DATABASE_URL'}
            with patch.object(runtime.deployment,'verify',return_value=release()):
                value=runtime.Runtime(self.root,{}, {},selected)
            value.directory=self.root;parameters={}
            def create(name,_record,environment,**_kwargs):
                parameters[name]=environment
                return ('a' if name=='postgres' else 'b')*64
            with patch.object(value.metrics,'start'),patch.object(value,'create',side_effect=create),patch.object(value,'wait'),\
                 patch.object(value,'execute',return_value=subprocess.CompletedProcess([],0,'0\n','')):
                with self.assertRaisesRegex(ValueError,'loopback'):value.boot()
            runtime_dsn=parameters['migrate']['RUNTIME_DATABASE_URL']
            serving_dsn=(self.root/'runtime.url').read_text().strip() if transport=='file' else parameters['api']['RUNTIME_DATABASE_URL']
            self.assertEqual(runtime_dsn,serving_dsn)
            self.assertNotIn('OWNER_DATABASE_URL',parameters['api'])
            self.assertNotIn('RUNTIME_DATABASE_PASSWORD',parameters['migrate'])

    def test_fresh_system_database_is_distinct_from_the_migrated_application_database(self):
        accepted=release();accepted['workload']['services']['postgres']['environment']['POSTGRES_DB']='postgres'
        with patch.object(runtime.deployment,'verify',return_value=accepted):
            value=runtime.Runtime(self.root,{}, {},profile()|{'database':'app'})
        value.directory=self.root;parameters={}
        def create(name,_record,environment,**_kwargs):
            parameters[name]=environment
            return ('a' if name=='postgres' else 'b')*64
        with patch.object(value.metrics,'start'),patch.object(value,'create',side_effect=create),patch.object(value,'wait'),\
             patch.object(value,'execute',return_value=subprocess.CompletedProcess([],0,'1\n','')):
            with self.assertRaisesRegex(ValueError,'migration job failed'):value.boot()
        self.assertEqual(parameters['postgres']['POSTGRES_DB'],'postgres')
        self.assertIn('/postgres?sslmode=disable',parameters['migrate']['OWNER_DATABASE_URL'])
        self.assertEqual(value.database,'app')

    def test_independent_native_inspection_refuses_weaker_limits_privileges_and_caps(self):
        record=release()['workload']['services']['api']
        info={'Config':{'User':'10001:10001'},'EffectiveCaps':[],
            'HostConfig':{'ReadonlyRootfs':True,'Privileged':False,'Memory':128<<20,'MemorySwap':128<<20,
                'PidsLimit':64,'CpuQuota':50000,'CpuPeriod':100000,'SecurityOpt':['no-new-privileges'],
                'Tmpfs':{'/tmp':'rw,noexec,nosuid,nodev,size=16m,mode=1777'},
                'Ulimits':[{'Name':'RLIMIT_NOFILE','Soft':4096,'Hard':4096}]}}
        runtime.assert_limits(record,info)
        runtime.assert_limits(record,info|{'EffectiveCaps':None})
        omitted=dict(info);omitted.pop('EffectiveCaps')
        with self.assertRaises(ValueError):runtime.assert_limits(record,omitted)
        for mutate in [lambda v:v['HostConfig'].update(Memory=256<<20),lambda v:v['HostConfig'].update(CpuQuota=100000),
                       lambda v:v['HostConfig'].update(ReadonlyRootfs=False),lambda v:v['HostConfig'].update(SecurityOpt=[]),
                       lambda v:v['HostConfig'].update(Tmpfs={'/tmp':'rw','/var/tmp':'rw'}),
                       lambda v:v.update(Mounts=[{'Type':'volume','Destination':'/undeclared'}]),
                       lambda v:v.update(EffectiveCaps=['CAP_SYS_ADMIN'])]:
            changed=copy.deepcopy(info);mutate(changed)
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):runtime.assert_limits(record,changed)

    def test_cleanup_removes_only_proven_owned_resources_and_refuses_foreign_container(self):
        value=self.selected();value.containers=['own','borrowed'];value.networks={};value.volumes={};calls=[]
        def execute(args,**_kwargs):
            calls.append(args)
            if args[:2]==['container','exists']:return subprocess.CompletedProcess(args,0,'','')
            if args[:2]==['container','inspect']:
                owner=value.nonce if args[-1]=='own' else 'another-owner'
                return subprocess.CompletedProcess(args,0,json.dumps([{'Id':('a' if owner==value.nonce else 'b')*64,'Config':{'Labels':{runtime.LABEL:owner}}}]),'')
            return subprocess.CompletedProcess(args,0,'','')
        with patch.object(value,'execute',side_effect=execute),self.assertRaisesRegex(ValueError,'ownership changed'):value.cleanup()
        self.assertIn(['rm','--force','--volumes','a'*64],calls)
        self.assertFalse(any('b'*64 in args for args in calls));self.assertFalse(any('prune' in args for args in calls))

    def test_cleanup_requires_each_native_resource_name_and_label_schema(self):
        for kind in ['network','volume']:
            value=self.selected();value.networks={};value.volumes={}
            getattr(value,kind+'s')['private']='owned-'+kind
            name_key,labels_key=('name','labels') if kind=='network' else ('Name','Labels')
            accepted={name_key:'owned-'+kind,labels_key:{runtime.LABEL:value.nonce}}
            cases=[([accepted],True),
                ([accepted|{name_key:'another-name'}],False),
                ([accepted|{labels_key:{runtime.LABEL:'another-owner'}}],False),
                ([accepted|{labels_key:None}],False),([],False),([accepted,accepted],False),
                ([{'Name' if kind=='network' else 'name':'owned-'+kind,
                   'Labels' if kind=='network' else 'labels':{runtime.LABEL:value.nonce}}],False)]
            for inspected,owned in cases:
                calls=[]
                def execute(args,**_kwargs):
                    calls.append(args)
                    return subprocess.CompletedProcess(args,0,json.dumps(inspected) if args[1]=='inspect' else '', '')
                with self.subTest(kind=kind,inspected=inspected),patch.object(value,'execute',side_effect=execute):
                    if owned:value.cleanup()
                    else:
                        with self.assertRaisesRegex(ValueError,'resource ownership changed'):value.cleanup()
                self.assertEqual([call for call in calls if call[1]=='rm'],[[kind,'rm','owned-'+kind]] if owned else [])
                self.assertFalse(any('prune' in call for call in calls))

    def test_actual_native_network_cleanup_removes_owned_and_preserves_foreign(self):
        if sys.platform!='linux':self.skipTest('native network cleanup acceptance requires Linux')
        if shutil.which('podman') is None:
            if os.environ.get('GITHUB_ACTIONS')=='true':
                self.fail('provider CI must prepare Podman for actual network cleanup acceptance')
            self.skipTest('actual network cleanup runs in the prepared native provider CI')
        value=self.selected();value.volumes={};value.networks={}
        owned=value.prefix+'-owned';foreign=value.prefix+'-foreign'
        owners={owned:value.nonce,foreign:secrets.token_hex(16)}
        created={}
        try:
            for name,owner in owners.items():
                self.assertEqual(value.execute(['network','exists',name],required=False).returncode,1)
                created[name]=owner
                value.execute(['network','create','--label',runtime.LABEL+'='+owner,name])
                info=json.loads(value.execute(['network','inspect',name]).stdout)
                self.assertEqual(len(info),1)
                self.assertEqual(info[0]['name'],name)
                self.assertEqual(info[0]['labels'][runtime.LABEL],owner)
                self.assertRegex(info[0]['id'],r'^[a-f0-9]{64}$')
            foreign_before=json.loads(value.execute(['network','inspect',foreign]).stdout)
            value.networks={'owned':owned,'foreign':foreign}
            with self.assertRaisesRegex(ValueError,'resource ownership changed'):value.cleanup()
            self.assertEqual(value.execute(['network','exists',owned],required=False).returncode,1)
            self.assertEqual(json.loads(value.execute(['network','inspect',foreign]).stdout),foreign_before)
            value.networks={'owned':owned};value.cleanup()
        finally:
            for name,owner in created.items():
                exists=value.execute(['network','exists',name],required=False)
                if exists.returncode==1:continue
                self.assertEqual(exists.returncode,0)
                info=json.loads(value.execute(['network','inspect',name]).stdout)
                self.assertEqual(len(info),1)
                self.assertEqual(info[0]['name'],name)
                self.assertEqual(info[0]['labels'][runtime.LABEL],owner)
                value.execute(['network','rm',info[0]['id']])
                self.assertEqual(value.execute(['network','exists',name],required=False).returncode,1)

    def test_native_memory_and_caddy_rss_are_observed_only_for_this_fixture(self):
        value=self.selected();value.active={'a'*64:'api'};value.services={'api':'a'*64}
        import os
        value.metrics.caddy_started(os.getpid())
        with patch.object(value,'execute',return_value=subprocess.CompletedProcess([],0,'a'*64+' 1.5MiB / 128MiB\n','')):
            value.metrics.sample()
        sample=value.metrics.report()['latest'];self.assertIsNotNone(sample)
        self.assertEqual(sample['components']['api'],1572864);self.assertGreater(sample['components']['caddy'],0)
        with patch.object(value,'execute',return_value=subprocess.CompletedProcess([],0,'b'*64+' 1MiB / 128MiB\n','')):
            with self.assertRaisesRegex(ValueError,'unowned'):value.metrics.sample()

    def test_actual_dependency_graph_preserves_private_renderer_without_api_credentials(self):
        value=release();renderer=copy.deepcopy(value['workload']['services']['api'])
        renderer['needs']={};renderer['environment']={'RENDERER_ADDRESS':':8088'}
        value['workload']['services']['renderer']=renderer
        value['workload']['services']['api']['needs']['renderer']='started'
        selected=self.selected(value)
        self.assertLess(selected.order.index('postgres'),selected.order.index('migrate'))
        self.assertLess(selected.order.index('migrate'),selected.order.index('api'))
        self.assertLess(selected.order.index('renderer'),selected.order.index('api'))
        broken=copy.deepcopy(value['workload']);broken['services']['renderer']['needs']={'api':'started'}
        with self.assertRaisesRegex(ValueError,'cycle'):runtime.ordered(broken)

    def test_measurement_parsing_refuses_missing_or_unbounded_units(self):
        self.assertEqual(runtime.memory_bytes('1.5 MiB'),1572864)
        for value in ['','--','NaNMiB','1EB','-1MB']:
            with self.subTest(value=value),self.assertRaises(ValueError):runtime.memory_bytes(value)
