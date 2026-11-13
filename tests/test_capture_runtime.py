"""Capture ownership and cleanup controls; real cgroup execution is separately gated."""
import sys
sys.dont_write_bytecode=True
from source_fixture import SDK_ROOT
from pathlib import Path
import copy
import contextlib
import hashlib
import json
import os
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import capture_runtime as runtime


class CaptureRuntime(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='capture-runtime-control-');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.stage=self.root/'.export-fixture';self.stage.mkdir(mode=0o700)
        self.group=self.root/'cgroup/owned';self.group.mkdir(parents=True)
        for name,value in [('memory.max','268435456'),('memory.swap.max','0'),('pids.max','128'),('cpu.max','50000 100000'),('cgroup.procs','')]:
            (self.group/name).write_text(value)
        self.patches=[patch.object(runtime,'CGROUP_ROOT',self.root/'cgroup'),patch.dict(os.environ,{},clear=True)]
        for selected in self.patches:selected.start();self.addCleanup(selected.stop)
        self.selector={'parent':'/owned','path':str(self.group),'leases':str(self.root/'leases')}
        self.volume={'Name':'owned-files','CreatedAt':'2026-11-09T00:00:00Z','Driver':'local','Mountpoint':'/owned/files','Options':None}
        fingerprint='sha256:'+hashlib.sha256(runtime.canonical(self.volume)).hexdigest()
        self.record={'application':'app','service':'postgres','container_id':'e'*64,'release_digest':'sha256:'+'a'*64,
            'volumes':{'files':{'name':'owned-files','id':fingerprint}},'deployment':{'release':{'workload':{'volumes':{'files':{}}},
                'artifacts':{'postgres':{'kind':'image','config_digest':'sha256:'+'b'*64},'api':{'kind':'image','config_digest':'sha256:'+'c'*64}}}}}
        self.resources=types.SimpleNamespace(validate=lambda value,app,service:value,environment=lambda:{'PATH':'/usr/bin:/bin'})
        self.actual=None;self.calls=[];self.fail_create=False
        self.run_patch=patch.object(runtime,'run',side_effect=self.engine);self.run_patch.start();self.addCleanup(self.run_patch.stop)
        selected=patch.object(runtime,'exists',side_effect=lambda *args:self.actual is not None);selected.start();self.addCleanup(selected.stop)

    def lease(self):return json.loads(next((self.root/'leases').glob('*.json')).read_bytes())

    def engine(self,resources,arguments,**options):
        self.calls.append(arguments)
        if arguments[:2]==['volume','inspect']:return json.dumps([self.volume]).encode()
        if arguments[0]=='create':
            lease=self.lease();self.assertEqual(lease['phase'],'intent');self.assertEqual(lease['container_id'],'')
            self.assertIn('--pull=never',arguments);self.assertIn('--read-only-tmpfs=false',arguments)
            self.assertEqual(arguments[arguments.index('--cgroup-parent')+1],'/owned')
            self.assertIn('--timeout',arguments);self.assertNotIn('api',arguments)
            self.actual={'Id':'d'*64,'Name':lease['name'],'Image':lease['config_digest'],
                'Config':{'User':lease['user'],'Labels':{'io.komizo.application':'app',
                    'io.komizo.capture.ownership':lease['ownership'],'io.komizo.capture.operation':lease['operation_id']}},
                'HostConfig':{'ReadonlyRootfs':True,'Privileged':False,'Memory':runtime.LIMITS['memory_bytes'],
                    'MemorySwap':runtime.LIMITS['memory_bytes'],'PidsLimit':64,'CpuPeriod':100000,'CpuQuota':25000,
                    'SecurityOpt':['no-new-privileges'],'NetworkMode':lease['network']},'EffectiveCaps':[],
                'State':{'Running':False,'Status':'created'},'Mounts':[
                    {'Type':item['type'],'Name':item['source'],'Source':item['source'],'Destination':item['target'],'RW':not item['read_only']}
                    for item in lease['mounts']]}
            if self.fail_create:raise ValueError('interrupted creation acknowledgement')
            return b'd'*64
        if arguments[:2]==['container','inspect']:return json.dumps([self.actual]).encode()
        if arguments[0]=='rm':self.assertEqual(arguments[-1],'d'*64);self.actual=None;return b''
        if arguments[0]=='start':
            self.assertEqual(self.lease()['container_id'],'d'*64);return b''
        raise AssertionError(arguments)

    def context(self,**options):
        return runtime.container(self.resources,self.record,self.stage,user='65534:65534',
            control_cgroup=self.selector,**options)

    @contextlib.contextmanager
    def helper_inventory(self):
        containers={}
        def engine(resources,arguments,**options):
            if arguments[0]=='create':
                ownership=arguments[arguments.index('--name')+1].removeprefix('kmz-capture-')
                lease=json.loads((self.root/'leases'/(ownership+'.json')).read_bytes())
                with patch.object(self,'lease',return_value=lease):self.engine(resources,arguments,**options)
                identity=ownership*2;self.actual['Id']=identity;containers[identity]=self.actual
                return identity.encode()
            if arguments[:2]==['container','inspect']:
                self.actual=containers[arguments[-1]]
                return self.engine(resources,arguments,**options)
            if arguments[0]=='rm':
                self.calls.append(arguments);del containers[arguments[-1]];return b''
            if arguments[0]=='start':self.calls.append(arguments);return b''
            return self.engine(resources,arguments,**options)
        def exists(resources,identity):
            return identity in containers or any(value['Name']==identity for value in containers.values())
        with patch.object(runtime,'run',side_effect=engine),patch.object(runtime,'exists',side_effect=exists),\
                patch.object(runtime.secrets,'token_hex',side_effect=['a'*32,'c'*32,'b'*32,'c'*32]):
            yield containers

    def test_nested_media_exit_preserves_outer_client_lease_and_private_environment(self):
        for failure in [False,True]:
            with self.subTest(media_failure=failure),self.helper_inventory() as containers:
                with self.context(entrypoint='/bin/sleep',command=['900'],network='postgres') as client:
                    outer=self.root/'leases'/('a'*32+'.json');before=outer.read_bytes()
                    try:
                        with self.context(entrypoint='/bin/sleep',command=['210'],mounts={'files':'/media'}) as media:
                            self.assertEqual(set(containers),{client,media})
                            if failure:raise ValueError('controlled media failure')
                    except ValueError as error:
                        self.assertTrue(failure);self.assertEqual(str(error),'controlled media failure')
                    self.assertEqual(set(containers),{client})
                    self.assertEqual(outer.read_bytes(),before)
                    self.assertEqual(json.loads((outer.parent/('b'*32+'.json')).read_bytes())['phase'],'cleaned')
                    self.assertEqual([path.name for path in self.stage.glob('.helper-*.env')],['.helper-'+'a'*32+'.env'])
                self.assertEqual(containers,{})
                self.assertFalse(list(self.stage.glob('.helper-*.env')))
            self.remove_cleaned_test_leases()

    def test_explicit_reconciliation_still_cleans_all_operation_helpers(self):
        with self.helper_inventory() as containers:
            with self.context(network='postgres') as client,self.context(mounts={'files':'/media'}) as media:
                observations=runtime.cleanup_leases(self.resources,self.root/'leases',inspect_only=True)
                self.assertEqual({value['container_id'] for value in observations},{client,media})
                self.assertEqual(set(containers),{client,media})
                runtime.cleanup_leases(self.resources,self.root/'leases')
                self.assertEqual(containers,{})
                for path in (self.root/'leases').glob('*.json'):
                    self.assertEqual(json.loads(path.read_bytes())['phase'],'cleaned')

    def test_context_cleanup_selector_refuses_nonownership_paths_before_native_effects(self):
        for ownership in ['',True,'a'*31,'a'*32+'.json','../'+'a'*32]:
            with self.subTest(ownership=ownership),self.assertRaisesRegex(ValueError,'ownership token'):
                runtime.cleanup_leases(self.resources,self.root/'leases',ownership=ownership)
        self.assertEqual(self.calls,[])

    @contextlib.contextmanager
    def completion_race(self,code=0,*,armed=True,change=None,vanish=('cgroup',1),terminal_status='exited'):
        process=self.root/'proc/123';process.mkdir(parents=True,exist_ok=True)
        group=self.group/'helper';group.mkdir(exist_ok=True)
        for name,value in [('memory.max',str(runtime.LIMITS['memory_bytes'])),('memory.swap.max','0'),
                ('pids.max','64'),('cpu.max','25000 100000')]:
            (group/name).write_text(value)
        (process/'cgroup').write_text('0::/owned/helper\n')
        (process/'status').write_text('Uid:\t65534 65534 65534 65534\nGid:\t65534 65534 65534 65534\nNoNewPrivs:\t1\n'+
            ''.join(name+':\t0000000000000000\n' for name in ['CapInh','CapPrm','CapEff','CapBnd','CapAmb']))
        target=(process if vanish[0] in {'cgroup','status'} else group)/vanish[0]
        events={'armed':armed,'reads':0,'target_reads':0,'commands':[],
            'error':FileNotFoundError(2,'controlled owned process exit',str(target))}
        read=Path.read_text
        def engine(resources,arguments,**options):
            events['commands'].append((list(arguments),dict(options)))
            result=self.engine(resources,arguments,**options)
            if arguments[0]=='start':self.actual['State']={'Running':True,'Status':'running','Pid':123,'ExitCode':0}
            if events['reads'] and events.get('on_command'):events['on_command'](arguments,options)
            return result
        def read_process(path,*args,**kwargs):
            if path==target and events['armed']:events['target_reads']+=1
            if path==target and events['armed'] and events['target_reads']==vanish[1]:
                events['reads']+=1
                self.actual['State']={'Running':False,'Status':terminal_status,'Pid':0,'ExitCode':code}
                if change:change(self.actual,events)
                if process.exists():
                    for owned in process.iterdir():owned.unlink()
                    process.rmdir()
                if group.exists():
                    for owned in group.iterdir():owned.unlink()
                    group.rmdir()
                events['at_read']=len(events['commands'])
                raise events['error']
            return read(path,*args,**kwargs)
        with patch.object(runtime,'PROC_ROOT',self.root/'proc'),patch.object(runtime,'run',side_effect=engine),\
                patch.object(Path,'read_text',read_process):
            yield events

    def remove_cleaned_test_leases(self):
        for path in (self.root/'leases').glob('*.json'):
            self.assertEqual(json.loads(path.read_bytes())['phase'],'cleaned');path.unlink()

    def test_completion_between_inspect_and_proc_keeps_actual_exit_and_caller_failure_then_cleans(self):
        for status,code in [(status,code) for status in ['stopped','exited'] for code in [0,37]]:
            def exercise():
                with self.completion_race(code,terminal_status=status) as events:
                    with self.context(entrypoint='/operator',command=['finite'],mounts={'files':'/blobs'}) as identity:
                        self.assertEqual(identity,'d'*64);self.assertEqual(self.actual['State']['ExitCode'],code)
                        self.assertEqual(self.actual['State']['Status'],status)
                        self.assertEqual(self.lease()['phase'],'running')
                        fresh=events['commands'][events['at_read']:]
                        self.assertEqual([args for args,_ in fresh],[['container','inspect',identity],['volume','inspect','owned-files']])
                        self.assertTrue(all(0<options['timeout']<=30 for _,options in fresh))
                        if code:raise ValueError('caller rejects the actual nonzero exit')
            if code:
                with self.assertRaisesRegex(ValueError,'caller rejects'):exercise()
            else:exercise()
            self.assertIsNone(self.actual);self.assertEqual(self.lease()['phase'],'cleaned')
            self.assertFalse(list(self.stage.glob('.helper-*.env')))
            self.remove_cleaned_test_leases()

    def test_cleanup_handles_its_own_inspect_proc_race_without_restart_or_lease_extension(self):
        with self.completion_race(armed=False) as events:
            with self.context(entrypoint='/operator',command=['finite'],mounts={'files':'/blobs'}):
                before=self.lease();events['armed']=True
            after=self.lease()
            self.assertEqual(events['reads'],1);self.assertIsNone(self.actual)
            self.assertEqual(after['phase'],'cleaned')
            self.assertEqual({k:v for k,v in after.items() if k!='phase'},{k:v for k,v in before.items() if k!='phase'})
            self.assertEqual(sum(args[0]=='create' for args in self.calls),1)
            self.assertEqual(sum(args[0]=='start' for args in self.calls),1)
            self.assertEqual(sum(args[0]=='rm' for args in self.calls),1)
            self.assertFalse(list(self.stage.glob('.helper-*.env')))

    def test_natural_completion_at_every_authored_live_evidence_read_is_reconciled_once(self):
        reads=[('cgroup',1),('status',1),*[(name,occurrence) for name in
            ['memory.max','pids.max','memory.swap.max','cpu.max'] for occurrence in [1,2]]]
        for vanish in reads:
            with self.subTest(vanish=vanish),self.completion_race(vanish=vanish) as events:
                with self.context(entrypoint='/operator',command=['finite'],mounts={'files':'/blobs'}):
                    self.assertEqual(self.actual['State']['ExitCode'],0)
                    self.assertEqual(events['reads'],1)
                    self.assertEqual([args for args,_ in events['commands'][events['at_read']:]],
                        [['container','inspect','d'*64],['volume','inspect','owned-files']])
                self.assertIsNone(self.actual);self.assertFalse(list(self.stage.glob('.helper-*.env')))
                self.remove_cleaned_test_leases()

    def test_foreign_cgroup_and_nonmissing_live_validation_failures_do_not_reinspect(self):
        for case in ['foreign','traversal','kernel-limit','process-user']:
            with self.subTest(case=case),self.completion_race(armed=False) as events:
                with self.context(entrypoint='/operator',command=['finite']):
                    process=self.root/'proc/123';group=self.group/'helper'
                    if case=='foreign':(process/'cgroup').write_text('0::/foreign/helper\n')
                    elif case=='traversal':(process/'cgroup').write_text('0::/owned/../foreign/helper\n')
                    elif case=='kernel-limit':(group/'memory.max').write_text('max')
                    else:(process/'status').write_text((process/'status').read_text().replace('65534','0'))
                    before=len(events['commands'])
                    with self.assertRaises(ValueError):runtime.inspect(self.resources,self.lease())
                    self.assertEqual(sum(args[:2]==['container','inspect'] for args,_ in events['commands'][before:]),1)
                    self.actual['State']={'Running':False,'Status':'exited','Pid':0,'ExitCode':0}
                self.remove_cleaned_test_leases()

    def test_fresh_terminal_result_rechecks_identity_mount_volume_privilege_and_resource_guards(self):
        changes=[lambda v,e:v.update(Id='f'*64),lambda v,e:v.update(Image='sha256:'+'f'*64),
            lambda v,e:v['Config']['Labels'].update({'io.komizo.capture.ownership':'f'*32}),
            lambda v,e:v['Config'].update(User='0:0'),lambda v,e:v.update(Mounts=[]),
            lambda v,e:v['HostConfig'].update(NetworkMode='host'),lambda v,e:v['HostConfig'].update(Privileged=True),
            lambda v,e:v['HostConfig'].update(Memory=1),lambda v,e:self.volume.update(CreatedAt='replacement'),
            lambda v,e:v['State'].update(Running=True,Status='running',Pid=123),
            lambda v,e:v['State'].update(Pid=123),lambda v,e:v['State'].update(ExitCode=-1),
            lambda v,e:v['State'].update(ExitCode=True),lambda v,e:v['State'].update(ExitCode=256),
            lambda v,e:v['State'].update(ExitCode='0'),lambda v,e:v['State'].pop('ExitCode'),
            lambda v,e:v['State'].update(Status='created'),lambda v,e:v['State'].update(Status=[]),
            lambda v,e:v['State'].update(Status={})]
        for status,change in [(status,change) for status in ['stopped','exited'] for change in changes]:
            with self.subTest(status=status,change=change),self.completion_race(armed=False,change=change,terminal_status=status) as events:
                with self.context(entrypoint='/operator',command=['finite'],mounts={'files':'/blobs'}):
                    before=copy.deepcopy(self.actual);volume=copy.deepcopy(self.volume);events['armed']=True
                    with self.assertRaises((OSError,ValueError)):runtime.inspect(self.resources,self.lease())
                    fresh=events['commands'][events['at_read']:]
                    self.assertEqual(sum(args[:2]==['container','inspect'] for args,_ in fresh),1)
                    self.actual=before;self.actual['State']={'Running':False,'Status':'exited','Pid':0,'ExitCode':0};self.volume=volume
                self.assertIsNone(self.actual);self.remove_cleaned_test_leases()

    def test_stale_running_result_preserves_uncertain_helper_lease_and_retires_environment(self):
        def stale(value,_events):value['State'].update(Running=True,Status='running',Pid=123)
        with self.completion_race(-1,change=stale) as events,self.assertRaises(FileNotFoundError):
            with self.context(entrypoint='/operator',command=['finite']):self.fail('unknown exit was yielded')
        self.assertIsNotNone(self.actual);self.assertEqual(self.lease()['phase'],'running')
        self.assertFalse(any(args[0]=='rm' for args in self.calls))
        self.assertFalse(list(self.stage.glob('.helper-*.env')))
        self.assertLessEqual(events['reads'],2)

    def test_unrelated_missing_process_file_or_invalid_lease_cannot_trigger_fresh_inspection(self):
        for case in ['filename','phase','deadline-type']:
            with self.subTest(case=case),self.completion_race(armed=False) as events:
                with self.context(entrypoint='/operator',command=['finite']):
                    lease=self.lease();events['armed']=True
                    if case=='filename':events['error'].filename=str(self.root/'proc/123/status')
                    elif case=='phase':lease['phase']='created'
                    else:lease['deadline_unix']=True
                    with self.assertRaises((FileNotFoundError,ValueError)):runtime.inspect(self.resources,lease)
                    self.assertEqual(events['commands'][events['at_read']:],[])
                self.remove_cleaned_test_leases()

    def test_reinspection_shares_remaining_lease_and_outer_deadline_across_all_native_calls(self):
        clock=[1000.0]
        def tighten(_value,_events):os.environ['KOMIZO_DEADLINE_UNIX']='1004'
        with patch.object(runtime.time,'time',side_effect=lambda:clock[0]),self.completion_race(change=tighten) as events:
            events['on_command']=lambda _args,_options:clock.__setitem__(0,clock[0]+1)
            with self.context(entrypoint='/operator',command=['finite'],mounts={'files':'/blobs'}):
                fresh=events['commands'][events['at_read']:]
                self.assertEqual([options['timeout'] for _,options in fresh],[4.0,3.0])
                self.assertEqual(self.lease()['deadline_unix'],1180)
            self.assertIsNone(self.actual);self.assertFalse(list(self.stage.glob('.helper-*.env')))

    def test_expired_or_exhausted_completion_budget_never_yields_a_helper(self):
        for status,case in [(status,case) for status in ['stopped','exited']
                            for case in ['expired','during-inspection','after-volume']]:
            clock=[1000.0]
            def change(_value,_events):
                if case=='expired':clock[0]=1005.0
            with self.subTest(status=status,case=case),patch.object(runtime.time,'time',side_effect=lambda:clock[0]),self.completion_race(change=change,terminal_status=status) as events:
                def consume(arguments,_options):
                    if case=='during-inspection' and arguments[:2]==['container','inspect']:clock[0]=1005.0
                    if case=='after-volume' and arguments[:2]==['volume','inspect']:clock[0]=1005.0
                events['on_command']=consume
                with self.assertRaises((FileNotFoundError,TimeoutError)):
                    with self.context(entrypoint='/operator',command=['finite'],mounts={'files':'/blobs'},timeout=5):
                        self.fail('expired completion evidence was yielded')
                fresh=events['commands'][events['at_read']:]
                # Cleanup still runs, but the additional admission inspection is never retried.
                self.assertLessEqual(sum(args[:2]==['container','inspect'] for args,_ in fresh),2)
                self.assertIsNone(self.actual);self.assertFalse(list(self.stage.glob('.helper-*.env')))
                self.remove_cleaned_test_leases()

    def test_stopped_helper_has_durable_intent_before_create_and_never_starts_api(self):
        with self.context(artifact='api',command=None,mounts={'files':'/blobs'}) as identity:
            self.assertEqual(identity,'d'*64);self.assertEqual(self.lease()['phase'],'created')
            self.assertEqual(self.lease()['config_digest'],'sha256:'+'c'*64)
        self.assertFalse(any(call[0]=='start' for call in self.calls));self.assertIsNone(self.actual)
        self.assertEqual(self.lease()['phase'],'cleaned');self.assertTrue((self.root/'leases').is_dir())

    def test_interrupted_creation_can_clean_only_exact_pre_effect_token_without_start_or_replay(self):
        self.fail_create=True
        with self.assertRaisesRegex(ValueError,'interrupted'):
            with self.context():self.fail('uncertain creation was yielded')
        self.assertIsNone(self.actual);self.assertEqual(self.lease()['phase'],'cleaned')
        self.assertFalse(any(call[0]=='start' for call in self.calls))

    def test_foreign_image_token_mount_or_network_refuses_cleanup_and_preserves_lease(self):
        for mutate in [lambda value:value.update(Image='sha256:'+'f'*64),
            lambda value:value['Config']['Labels'].update({'io.komizo.capture.ownership':'f'*32}),
            lambda value:value.update(Mounts=[]),lambda value:value['HostConfig'].update(NetworkMode='host')]:
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):
                with self.context(mounts={'files':'/blobs'}):mutate(self.actual)
            self.assertIsNotNone(self.actual);self.assertEqual(self.lease()['phase'],'created')
            self.actual=None
            for path in (self.root/'leases').iterdir():path.unlink()

    def test_inspection_mode_never_removes_helper_or_changes_raw_lease_bytes(self):
        with self.context() as identity:
            path=next((self.root/'leases').glob('*.json'));before=path.read_bytes();calls=len(self.calls)
            status=runtime.cleanup_leases(self.resources,self.root/'leases',inspect_only=True)
            self.assertEqual(status[0]['container_id'],identity);self.assertTrue(status[0]['present'])
            self.assertEqual(before,path.read_bytes());self.assertFalse(any(call[0]=='rm' for call in self.calls[calls:]))
        missing=self.root/'absent'
        with self.assertRaises(OSError):runtime.cleanup_leases(self.resources,missing,inspect_only=True)
        self.assertFalse(missing.exists())

    def test_volume_fingerprint_and_declared_volume_membership_refuse_before_create(self):
        self.volume['CreatedAt']='replacement'
        with self.assertRaisesRegex(ValueError,'fingerprint'):
            with self.context(mounts={'files':'/blobs'}):pass
        self.assertFalse(any(call[0]=='create' for call in self.calls))
        with self.assertRaises(ValueError):runtime.retained_volume(self.record,'foreign')

    def test_missing_or_unbounded_fixture_control_is_not_silently_omitted(self):
        with self.assertRaises(ValueError):runtime.control(None,self.stage)
        for name in ['memory.max','memory.swap.max','pids.max','cpu.max']:
            original=(self.group/name).read_text();(self.group/name).write_text('max' if name!='cpu.max' else 'max 100000')
            with self.subTest(name=name),self.assertRaises(ValueError):runtime.control(self.selector,self.stage)
            (self.group/name).write_text(original)
        self.assertFalse(self.calls)

    def test_production_context_rejects_any_caller_control_override(self):
        with patch.dict(os.environ,{'KOMIZO_OPERATION_ID':'a'*32}):
            with self.assertRaisesRegex(ValueError,'overridden'):runtime.control(self.selector,self.stage)

    def production(self):
        task='b'*32
        path=self.root/'cgroup/komizo.slice/komizo-control.slice'/('komizo-reconcile-'+task+'.service')
        current=path/'runner';current.mkdir(parents=True)
        for name,value in [('memory.max','268435456'),('memory.swap.max','0'),('pids.max','128'),
                ('cpu.max','50000 100000'),('cgroup.procs',''),('cgroup.controllers','cpu memory pids'),('cgroup.subtree_control','')]:
            (path/name).write_text(value)
        (current/'cgroup.procs').write_text(str(os.getpid()))
        process=self.root/'proc/self';process.mkdir(parents=True)
        (process/'cgroup').write_text('0::/'+current.relative_to(self.root/'cgroup').as_posix()+'\n')
        return task,path,current

    def test_production_parent_is_actual_new_task_but_leases_retain_interrupted_old_operation(self):
        task,path,current=self.production();journal=self.root/'journal'
        with patch.dict(os.environ,{'KOMIZO_OPERATION_ID':'a'*32,'KOMIZO_TASK_OPERATION_ID':task,'KOMIZO_CGROUP_PARENT':'/forged'}),\
                patch.object(runtime.os,'geteuid',return_value=0),patch.object(runtime,'PROC_ROOT',self.root/'proc'),\
                patch.object(runtime,'PRODUCTION_JOURNAL',journal),patch.object(runtime,'protected_owner') as owner,\
                patch.object(runtime,'private_directory',side_effect=lambda value:Path(value)):
            parent,actual,leases=runtime.control(None,self.stage)
            self.assertEqual(parent,'/'+path.relative_to(self.root/'cgroup').as_posix())
            self.assertEqual(actual,str(path));self.assertEqual(leases,journal/('a'*32)/'capture-helpers')
            self.assertEqual(owner.call_args_list,[unittest.mock.call(path),unittest.mock.call(current)])
            for value in [None,'a'*32,'invalid']:
                with patch.dict(os.environ,{'KOMIZO_TASK_OPERATION_ID':value or ''}),self.assertRaises(ValueError):
                    runtime.control(None,self.stage)
            self.assertFalse(self.calls)

    def test_wrong_service_runner_membership_controllers_or_internal_processes_refuse_before_lease_effects(self):
        task,path,current=self.production()
        with patch.dict(os.environ,{'KOMIZO_OPERATION_ID':'a'*32,'KOMIZO_TASK_OPERATION_ID':task}),\
                patch.object(runtime.os,'geteuid',return_value=0),patch.object(runtime,'PROC_ROOT',self.root/'proc'),\
                patch.object(runtime,'protected_owner'),patch.object(runtime,'private_directory') as mutate:
            group=self.root/'proc/self/cgroup';original=group.read_text()
            for wrong in [original.replace('komizo-reconcile-','unrelated-'),original.replace('/runner','/other'),
                    original.replace('/runner','/runner/child'),'0::/other\n0::/ambiguous\n']:
                group.write_text(wrong)
                with self.assertRaises(ValueError):runtime.control(None,self.stage)
            group.write_text(original)
            for file,value in [(path/'cgroup.procs',str(os.getpid())),(current/'cgroup.procs','99999999'),
                    (path/'cgroup.controllers','cpu memory'),(path/'memory.swap.max','max')]:
                before=file.read_text();file.write_text(value)
                with self.subTest(path=file),self.assertRaises(ValueError):runtime.control(None,self.stage)
                file.write_text(before)
            mutate.assert_not_called();self.assertFalse(self.calls)

    def test_task_control_requires_root_owned_nonwritable_cgroup_directories(self):
        for owner,mode in [(1001,0o755),(0,0o777),(0,0o775)]:
            information=types.SimpleNamespace(st_uid=owner,st_mode=0o040000|mode)
            with patch.object(Path,'stat',return_value=information),self.assertRaises(ValueError):
                runtime.protected_owner(self.group)

    def test_old_parent_must_be_empty_or_absent_with_no_cached_live_pid_and_exact_metadata_parent(self):
        task,current,_runner=self.production()
        old=current.with_name('komizo-backup-'+('c'*32)+'.service');old.mkdir()
        (old/'cgroup.events').write_text('populated 0\nfrozen 0\n')
        parent='/'+old.relative_to(self.root/'cgroup').as_posix()
        lease={'operation_id':'a'*32,'control_path':str(old),'control_parent':parent}
        value={'HostConfig':{'CgroupParent':parent},'State':{'Running':True,'Pid':123,'ConmonPid':124}}
        with patch.dict(os.environ,{'KOMIZO_OPERATION_ID':'a'*32,'KOMIZO_TASK_OPERATION_ID':task}),\
                patch.object(runtime.os,'geteuid',return_value=0),patch.object(runtime,'PROC_ROOT',self.root/'proc'),\
                patch.object(runtime,'production_control',return_value=current),patch.object(runtime,'protected_owner'):
            self.assertTrue(runtime._old_task_quiesced(lease,value))
            (old/'cgroup.events').write_text('populated 1\nfrozen 0\n')
            self.assertFalse(runtime._old_task_quiesced(lease,value))
            (old/'cgroup.events').write_text('populated 0\nfrozen 0\n')
            for pid in [123,124]:
                process=self.root/'proc'/str(pid);process.mkdir()
                self.assertFalse(runtime._old_task_quiesced(lease,value));process.rmdir()
            wrong=copy.deepcopy(value);wrong['HostConfig']['CgroupParent']='/foreign'
            self.assertFalse(runtime._old_task_quiesced(lease,wrong))
            (old/'cgroup.events').unlink();old.rmdir()
            self.assertTrue(runtime._old_task_quiesced(lease,value))
            with patch.dict(os.environ,{'KOMIZO_OPERATION_ID':'d'*32}):
                self.assertFalse(runtime._old_task_quiesced(lease,value))

    def test_known_dead_conmon_transition_allows_one_nonforce_exact_cid_removal_without_replay(self):
        original=self.engine
        def engine(resources,arguments,**options):
            if arguments[:2]==['rm','--force']:
                self.calls.append(arguments)
                self.actual['State']={'Status':'exited','Running':False,'Pid':0,'ExitCode':-1,'Error':runtime.CONMON_UNKNOWN_EXIT}
                raise ValueError('bounded native capture command failed')
            return original(resources,arguments,**options)
        with patch.object(runtime,'run',side_effect=engine),patch.object(runtime,'_old_task_quiesced',return_value=True):
            with self.context():pass
        removals=[call for call in self.calls if call[0]=='rm']
        self.assertEqual(removals,[['rm','--force','--volumes','d'*64],['rm','--volumes','d'*64]])
        self.assertIsNone(self.actual);self.assertEqual(self.lease()['phase'],'cleaned')
        self.assertFalse(any(call[0]=='start' for call in self.calls))

    def test_unknown_exit_followup_refuses_wrong_state_or_changed_container_identity(self):
        for mutate in [lambda value:value['State'].update(ExitCode=0),lambda value:value['State'].update(Pid=123),
                lambda value:value['State'].update(ConmonPid=124),lambda value:value['State'].update(ConmonPid=None),
                lambda value:value['State'].update(Status='created'),lambda value:value['State'].update(Error='unrelated failure'),
                lambda value:value.update(Image='sha256:'+'f'*64),lambda value:value['Config']['Labels'].update({'io.komizo.capture.ownership':'f'*32})]:
            original=self.engine
            def engine(resources,arguments,**options):
                if arguments[:2]==['rm','--force']:
                    self.calls.append(arguments)
                    self.actual['State']={'Status':'exited','Running':False,'Pid':0,'ExitCode':-1,'Error':runtime.CONMON_UNKNOWN_EXIT}
                    mutate(self.actual);raise ValueError('bounded native capture command failed')
                return original(resources,arguments,**options)
            with patch.object(runtime,'run',side_effect=engine),patch.object(runtime,'_old_task_quiesced',return_value=True),self.assertRaises(ValueError):
                with self.context():pass
            self.assertFalse(any(call[:2]==['rm','--volumes'] for call in self.calls))
            self.assertIsNotNone(self.actual);self.assertEqual(self.lease()['phase'],'created')
            self.actual=None;self.calls=[]
            for path in (self.root/'leases').iterdir():path.unlink()

    def test_known_conmon_error_cannot_remove_again_while_old_scope_is_live(self):
        original=self.engine
        def engine(resources,arguments,**options):
            if arguments[:2]==['rm','--force']:
                self.calls.append(arguments)
                self.actual['State']={'Status':'exited','Running':False,'Pid':0,'ExitCode':-1,'Error':runtime.CONMON_UNKNOWN_EXIT}
                raise ValueError('bounded native capture command failed')
            return original(resources,arguments,**options)
        with patch.object(runtime,'run',side_effect=engine),patch.object(runtime,'_old_task_quiesced',return_value=False),self.assertRaises(ValueError):
            with self.context():pass
        self.assertEqual([call for call in self.calls if call[0]=='rm'],[['rm','--force','--volumes','d'*64]])
        self.assertIsNotNone(self.actual);self.assertEqual(self.lease()['phase'],'created')

    def test_actual_process_identity_and_all_kernel_capability_sets_are_required(self):
        process=self.root/'proc/123';process.mkdir(parents=True)
        fields={'Uid':'65534 65534 65534 65534','Gid':'65534 65534 65534 65534','NoNewPrivs':'1',
            **{name:'0000000000000000' for name in ['CapInh','CapPrm','CapEff','CapBnd','CapAmb']}}
        def write(values):(process/'status').write_text(''.join(key+':\t'+value+'\n' for key,value in values.items()))
        with patch.object(runtime,'PROC_ROOT',self.root/'proc'):
            write(fields);runtime.process_identity(123,'65534:65534')
            for key,value in [('Uid','0 0 0 0'),('Gid','0 0 0 0'),('NoNewPrivs','0'),
                    *[(name,'0000000000000001') for name in ['CapInh','CapPrm','CapEff','CapBnd','CapAmb']]]:
                write(fields|{key:value})
                with self.subTest(key=key),self.assertRaises(ValueError):runtime.process_identity(123,'65534:65534')

    def test_private_staging_binds_refuse_links_traversal_and_writable_retained_data(self):
        outside=self.root/'outside';outside.write_bytes(b'preserve')
        linked=self.stage/'linked';linked.symlink_to(outside)
        for path in [outside,linked]:
            with self.assertRaises(ValueError):
                with self.context(binds=[{'source':str(path),'target':'/run/source','read_only':True}]):pass
        with self.assertRaises(ValueError):
            with self.context(mounts={'files':'/x,readwrite'}):pass
        owned=self.stage/'ordinary';owned.write_bytes(b'owned')
        for targets in [('/run/source','/run/source'),('/run/source','/run/source/child')]:
            with self.assertRaisesRegex(ValueError,'overlap'):
                with self.context(binds=[{'source':str(owned),'target':target,'read_only':True} for target in targets]):pass
        self.assertEqual(outside.read_bytes(),b'preserve');self.assertFalse(any(call[0]=='create' for call in self.calls))

    def test_started_helper_persists_id_before_start_and_uses_only_private_env_file(self):
        with self.context(entrypoint='/operator',command=['storage-backup'],network='postgres',
                environment={'BACKUP_DATABASE_URL_FILE':'/run/backup.url'}) as identity:
            self.assertEqual(self.lease()['phase'],'running');self.assertEqual(identity,'d'*64)
            create=next(call for call in self.calls if call[0]=='create')
            self.assertIn('container:'+'e'*64,create)
            self.assertNotIn('BACKUP_DATABASE_URL_FILE',' '.join(create));self.assertIn('--env-file',create)
        self.assertIsNone(self.actual)

    def test_private_lease_links_modes_duplicates_and_changed_identity_are_refused(self):
        with self.context():pass
        path=next((self.root/'leases').glob('*.json'));body=path.read_bytes();path.chmod(0o644)
        with self.assertRaises(ValueError):runtime.cleanup_leases(self.resources,path.parent)
        path.chmod(0o600);path.write_bytes(b'{"kind":1,"kind":2}')
        with self.assertRaises(ValueError):runtime.cleanup_leases(self.resources,path.parent)
        path.write_bytes(body);bad=json.loads(body);bad['ownership']='b'*32;path.write_bytes(runtime.canonical(bad))
        with self.assertRaises(ValueError):runtime.cleanup_leases(self.resources,path.parent)
        path.write_bytes(body);other=self.root/'saved';path.rename(other);path.symlink_to(other)
        with self.assertRaises(ValueError):runtime.cleanup_leases(self.resources,path.parent)

    def test_only_exact_private_interrupted_atomic_files_are_skipped_without_mutation(self):
        with self.context():
            directory=self.root/'leases';pending=directory/'.lease-ab12_cd3'
            pending.write_bytes(b'{partial intent');pending.chmod(0o600);before=pending.read_bytes()
            status=runtime.cleanup_leases(self.resources,directory,inspect_only=True)
            self.assertEqual(len(status),1);self.assertEqual(pending.read_bytes(),before)
            self.assertIsNotNone(self.actual)
            pending.chmod(0o644)
            with self.assertRaises(ValueError):runtime.cleanup_leases(self.resources,directory)
            self.assertIsNotNone(self.actual);pending.chmod(0o600)
            linked=directory/'.lease-zzzzzzzz';linked.symlink_to(pending)
            with self.assertRaises(ValueError):runtime.cleanup_leases(self.resources,directory)
            linked.unlink();unknown=directory/'.lease-malformed';unknown.write_bytes(b'preserve');unknown.chmod(0o600)
            with self.assertRaises(ValueError):runtime.cleanup_leases(self.resources,directory)
            self.assertEqual(unknown.read_bytes(),b'preserve');unknown.unlink()
        self.assertEqual(pending.read_bytes(),before);self.assertIsNone(self.actual)

if __name__=='__main__':unittest.main()
