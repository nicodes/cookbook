"""Capture ownership and cleanup controls; real cgroup execution is separately gated."""
import sys
sys.dont_write_bytecode=True
from source_fixture import SDK_ROOT
from pathlib import Path
import copy
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
        for name,value in [('memory.max','268435456'),('pids.max','128'),('cpu.max','50000 100000'),('cgroup.procs','')]:
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
        self.actual=None;self.calls=[];self.fail_create=False;self.origin=None
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
        if arguments[:2]==['container','inspect']:
            return json.dumps([self.origin if self.origin is not None and arguments[2]==self.record['container_id'] else self.actual]).encode()
        if arguments[0]=='rm':self.assertEqual(arguments[-1],'d'*64);self.actual=None;return b''
        if arguments[0]=='start':
            self.assertEqual(self.lease()['container_id'],'d'*64);return b''
        raise AssertionError(arguments)

    def context(self,**options):
        return runtime.container(self.resources,self.record,self.stage,user='65534:65534',
            control_cgroup=self.selector,**options)

    def socket_origin(self):
        proc=self.root/'proc';pid=12345;process=proc/str(pid);process.mkdir(parents=True)
        root=process/'root';(root/'run/postgresql').mkdir(parents=True);(root/'var').mkdir()
        (root/'var/run').symlink_to('/run')
        directory=root/'run/postgresql';(directory/'sentinel').write_bytes(b'pinned original directory')
        def process_stat(number,ticks):return str(number)+' (postgres) S '+'0 '*18+str(ticks)+'\n'
        (process/'stat').write_text(process_stat(pid,100))
        holder=proc/str(os.getpid());holder.mkdir(exist_ok=True);(holder/'stat').write_text(process_stat(os.getpid(),200))
        uid,gid=os.geteuid(),os.getegid()
        (process/'status').write_text('Uid:\t'+' '.join([str(uid)]*4)+'\nGid:\t'+' '.join([str(gid)]*4)+'\n')
        device=directory.stat().st_dev
        (process/'mountinfo').write_text(f'36 25 {os.major(device)}:{os.minor(device)} / /run/postgresql rw,nosuid,nodev,noexec - tmpfs tmpfs rw,size=1024k\n')
        self.record['image_config_digest']='sha256:'+'b'*64
        self.record['deployment']['release']['workload']['services']={'postgres':{'user':'0:0','tmpfs':{
            '/var/run/postgresql':{'size_bytes':1<<20,'mode':0o1777}}}}
        self.origin={'Id':self.record['container_id'],'Image':self.record['image_config_digest'],'Config':{'User':'0:0'},
            'HostConfig':{'Tmpfs':{'/var/run/postgresql':'rw,noexec,nosuid,nodev,size=1m'}},
            'State':{'Pid':pid,'Running':True,'Paused':False,'Restarting':False}}
        selected=patch.object(runtime,'PROC_ROOT',proc);selected.start();self.addCleanup(selected.stop)
        # The ordinary parser/FD model is unprivileged. The separate delegated
        # native control exercises the real protected-root precondition.
        selected=patch.object(runtime,'require_origin_root');selected.start();self.addCleanup(selected.stop)
        return directory,process

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
        for name in ['memory.max','pids.max','cpu.max']:
            original=(self.group/name).read_text();(self.group/name).write_text('max' if name!='cpu.max' else 'max 100000')
            with self.subTest(name=name),self.assertRaises(ValueError):runtime.control(self.selector,self.stage)
            (self.group/name).write_text(original)
        self.assertFalse(self.calls)

    def test_production_context_rejects_any_caller_control_override(self):
        with patch.dict(os.environ,{'KOMIZO_OPERATION_ID':'a'*32}):
            with self.assertRaisesRegex(ValueError,'overridden'):runtime.control(self.selector,self.stage)

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

    def test_socket_directory_fd_pins_only_source_declared_tmpfs_without_ambient_paths(self):
        directory,_=self.socket_origin();source=None
        with self.context(container_paths={'/var/run/postgresql':'/run/capture/postgres'}) as identity:
            lease=self.lease();self.assertEqual(len(lease['origins']),1);origin=lease['origins'][0];source=Path(origin['source'])
            self.assertEqual(identity,'d'*64);self.assertEqual(source.stat().st_ino,directory.stat().st_ino)
            self.assertEqual((source/'sentinel').read_bytes(),b'pinned original directory')
            self.assertEqual(origin['container_id'],self.record['container_id']);self.assertEqual(origin['start_ticks'],100)
            self.assertEqual(lease['network'],'none');self.assertEqual(origin['uid'],os.geteuid())
            arguments=next(call for call in self.calls if call[0]=='create')
            self.assertIn('type=bind,source='+str(source)+',target=/run/capture/postgres,readonly,nosuid,nodev,noexec,bind-nonrecursive',arguments)
        self.assertFalse(source.exists());self.assertEqual(self.lease()['phase'],'cleaned')
        for paths in [{'/proc/1/root':'/run/capture/postgres'}, {'/run/not-declared':'/run/capture/postgres'},
                {'/var/run/postgresql':'/etc'}, {'/var/run/postgresql':'/run/capture/../escape'}]:
            before=len(self.calls)
            with self.subTest(paths=paths),self.assertRaises(ValueError):
                with self.context(container_paths=paths):pass
            self.assertFalse(any(call[0]=='create' for call in self.calls[before:]))
        with self.assertRaises(ValueError):
            with self.context(container_paths={'/var/run/postgresql':'/run/capture/postgres'},network='postgres'):pass

    def test_origin_restart_directory_replacement_and_changed_owner_refuse_but_remove_owned_helper(self):
        directory,process=self.socket_origin()
        for change in ['restart','owner','directory','death']:
            before=copy.deepcopy(self.origin);status=(process/'status').read_text();stat_body=(process/'stat').read_text()
            with self.subTest(change=change),self.assertRaises(ValueError):
                with self.context(container_paths={'/var/run/postgresql':'/run/capture/postgres'}):
                    if change=='restart':(process/'stat').write_text(stat_body.replace('100','101'))
                    elif change=='owner':(process/'status').write_text(status.replace('Uid:\t','Uid:\t9 '))
                    elif change=='death':self.origin['State']['Running']=False
                    else:
                        directory.rename(directory.with_name('old'));directory.mkdir()
            self.assertIsNone(self.actual);self.assertEqual(self.lease()['phase'],'cleaned')
            if change=='directory':directory.rmdir();directory.with_name('old').rename(directory)
            self.origin=before;(process/'status').write_text(status);(process/'stat').write_text(stat_body)
            for path in (self.root/'leases').iterdir():path.unlink()
        self.assertEqual((directory/'sentinel').read_bytes(),b'pinned original directory')

    def test_socket_tmpfs_type_missing_owner_and_last_symlink_refuse_before_create(self):
        directory,process=self.socket_origin();mounts=(process/'mountinfo').read_text();status=(process/'status').read_text()
        for field in ['filesystem','owner','link']:
            if field=='filesystem':(process/'mountinfo').write_text(mounts.replace(' - tmpfs ',' - ext4 '))
            elif field=='owner':(process/'status').write_text('Uid:\t9 9 9 9\nGid:\t9 9 9 9\n')
            else:directory.rename(directory.with_name('saved'));directory.symlink_to('saved')
            before=len(self.calls)
            with self.subTest(field=field),self.assertRaises(ValueError):
                with self.context(container_paths={'/var/run/postgresql':'/run/capture/postgres'}):pass
            self.assertFalse(any(call[0]=='create' for call in self.calls[before:]))
            if field=='link':directory.unlink();directory.with_name('saved').rename(directory)
            (process/'mountinfo').write_text(mounts);(process/'status').write_text(status)

    def test_root_requirement_and_closed_origin_lease_refuse_arbitrary_proc_authority(self):
        with patch.object(runtime.os,'geteuid',return_value=1001),self.assertRaisesRegex(ValueError,'protected root'):
            runtime.require_origin_root()
        self.socket_origin()
        with self.context(container_paths={'/var/run/postgresql':'/run/capture/postgres'}):
            path=next((self.root/'leases').glob('*.json'));before=path.read_bytes();lease=json.loads(before)
            for mutate in [lambda x:x['origins'][0].update(source='/proc/1/root/etc'),
                    lambda x:x['origins'][0].update(path='/proc/self/fd'),lambda x:x['origins'][0].update(uid=True),
                    lambda x:x.update(network='container:'+'f'*64),lambda x:x['mounts'][0].update(read_only=False)]:
                changed=copy.deepcopy(lease);mutate(changed);path.write_bytes(runtime.canonical(changed))
                with self.assertRaises(ValueError):runtime.cleanup_leases(self.resources,path.parent)
                self.assertIsNotNone(self.actual)
            path.write_bytes(before)

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
