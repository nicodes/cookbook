import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

sys.dont_write_bytecode = True

from source_fixture import SDK_ROOT as ROOT
import dependencies
sys.path.insert(0,str(ROOT/'cookbook'))
import backup_bundle as bundle


ROOT_LAUNCHER = r"""
import hashlib,json,os,re,shutil,signal,stat,subprocess,sys,tempfile,time
from pathlib import Path
sys.dont_write_bytecode=True
value=json.load(sys.stdin)
root=Path(value['root'])
assert root.parent==Path(tempfile.gettempdir()).resolve() and root.name.startswith('backup-bundle-control-')
assert root.resolve()==root and not root.is_symlink() and root.stat().st_mode&0o077==0
request=json.loads(value['request'])
operation=request['operation_id'];assert re.fullmatch('[a-f0-9]{32}',operation)
output=root/operation
assert output.is_dir() and not output.is_symlink()
assert request.get('output_directory',str(output))==str(output)
program=root/value['program'];assert program.parent==root and program.is_file() and not program.is_symlink()
owner=output.stat();before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file() and not p.is_symlink()}
assert len(before)==len(list(output.iterdir()))
os.chown(output,0,0)
if value.get('reconcile_orphan'):
 stage=output/'.export-orphan';stage.mkdir(mode=0o700);(stage/'plaintext').write_bytes(b'owned synthetic dump')
 info=stage.stat();marker={'kind':'komizo.capture-staging','version':1,'application':'app','operation_id':operation,
  'path':str(stage),'device':info.st_dev,'inode':info.st_ino}
 path=output/'.capture-staging.json';path.write_text(json.dumps(marker));path.chmod(0o600)
 directory=root/'journal'/operation/'capture-helpers';directory.mkdir(parents=True,mode=0o700)
child=None;clean=True
try:
 child=subprocess.Popen([str(program),*value['arguments']],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                        env=value['environment'],cwd=root,start_new_session=True)
 if value['interrupt']:
  child.stdin.write(value['request'].encode());child.stdin.close();child.stdin=None
  deadline=time.monotonic()+10
  while not list(output.glob('.export-*/started')):
   assert child.poll() is None,'bundle exited before interrupt control'
   assert time.monotonic()<deadline,'capture did not start'
   time.sleep(.01)
  os.killpg(child.pid,signal.SIGTERM)
  stdout,stderr=child.communicate(timeout=10)
 else:stdout,stderr=child.communicate(input=value['request'].encode(),timeout=35)
 assert len(stdout)<=65536 and len(stderr)<=65536
 code=child.returncode
finally:
 if child is not None and child.poll() is None:
  os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=5)
 # Original evidence is never removed, replaced or made public. Only newly
 # created outputs in this fixture operation directory can be transferred.
 for path in output.iterdir():
  if path.name in before:
   assert path.is_file() and not path.is_symlink() and hashlib.sha256(path.read_bytes()).hexdigest()==before[path.name]
  elif path.name in {'snapshot.cms','receipt.json'} and path.is_file() and not path.is_symlink():
   assert path.stat().st_nlink==1 and path.stat().st_mode&0o777==0o600
   os.chown(path,owner.st_uid,owner.st_gid)
  else:
   clean=False
   information=path.lstat()
   assert information.st_uid==0
   if stat.S_ISDIR(information.st_mode) and not path.is_symlink():shutil.rmtree(path)
   else:path.unlink()
 os.chown(output,owner.st_uid,owner.st_gid)
print(json.dumps({'returncode':code,'stdout':stdout.decode(),'stderr':stderr.decode(),'clean':clean}))
"""


def helper(path,name):
    spec=importlib.util.spec_from_file_location(name,path);value=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value);return value


class BackupBundle(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='backup-bundle-control-');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.operation='a'*32
        self.output=self.root/self.operation;self.output.mkdir(mode=0o700)
        self.tools=dependencies.tools_root()
        providers={key:'a'*40 for key in bundle.PROVIDERS}
        images={name:{'kind':'image','reference':'ghcr.io/example/app-'+name+'@sha256:'+'b'*64,
                      'digest':'sha256:'+'b'*64,'config_digest':'sha256:'+('c' if name=='postgres' else 'd')*64,
                      'platform':'linux/amd64'} for name in ['postgres','api']}
        release={'kind':'komizo.release','version':1,'application':'app',
            'source':{'repository':'example/app','repository_id':'123','revision':'a'*40},'providers':providers,
            'artifacts':images,'workload':{'services':{name:{'artifact':name,'user':'0:0'} for name in images}}}
        self.deployment={'release':release,'release_digest':'sha256:'+hashlib.sha256(bundle.canonical(release)).hexdigest(),'provider':providers}
        self.records={name:{'kind':'komizo.resource','version':1,'application':'app','instance':'production','service':name,
            'release_digest':self.deployment['release_digest'],'container_id':('e' if name=='postgres' else 'f')*64,
            'container_name':'owned_'+name,'image_ref':images[name]['reference'],'image_config_digest':images[name]['config_digest'],
            'volumes':{},'deployment':self.deployment} for name in images}
        self.metadata={'project':'app','revision':'a'*40,'engine':{'reference':images['postgres']['reference'],
            'image_id':images['postgres']['config_digest'],'major':18},'roles':['app_owner','app_runtime'],
            'images':{name:{'reference':value['reference'],'image_id':value['config_digest']} for name,value in images.items()},
            'deployment':self.deployment}
        self.manifest={'kind':'komizo.backup-bundle','version':1,'application':'app',
            'source':{'repository':'example/app','revision':'b'*40},'providers':providers,
            'services':['postgres'],'entrypoint':'product/deploy/capture.py'}
        self.request={'kind':'komizo.backup-request','version':1,'application':'app','instance':'production',
            'release_digest':self.deployment['release_digest'],'operation_id':self.operation,
            'output_directory':str(self.output),'deadline_unix':int(time.time())+300}
        self.environment={**{'KOMIZO_'+key.upper():str(value) for key,value in self.request.items() if key not in {'kind','version'}},
            'PYTHONPATH':'/untrusted','BACKUP_RECOVERY_KEY_FILE':'/must/not/read','CONTAINER_HOST':'ssh://foreign',
            **{key:'synthetic-runner-only-object-store-secret' for key in
               ['BACKUP_S3_ACCESS_KEY','BACKUP_S3_SECRET_KEY','AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY']}}
        self.files={'__main__.py':(ROOT/'cookbook/backup_bundle.py').read_bytes(),'recipient.pem':b'public certificate fixture',
                    **{'helpers/'+name:(self.tools/'helpers'/name).read_bytes() for name in bundle.HELPERS},
                    **{'helpers/'+name:(ROOT/'cookbook'/name).read_bytes() for name in bundle.PROVIDER_HELPERS}}
        self.files['helpers/installed_resource.py']+=('\n_fixture_records='+repr(self.records)+'''\ndef query(application, service):
    return validate(_fixture_records[service],application,service)
''').encode()
        self.files['product/deploy/capture.py']=('''import hashlib,json,os
from pathlib import Path
assert not any(key in os.environ for key in ['BACKUP_S3_ACCESS_KEY','BACKUP_S3_SECRET_KEY','AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY'])
PROJECT='app'
RECOVERY_ROOT=Path('/must/not/import')
def capture(stage):
    assert RECOVERY_ROOT.parent.name=='bundle'
    assert 'BACKUP_RECOVERY_KEY_FILE' not in os.environ and 'CONTAINER_HOST' not in os.environ and 'PYTHONPATH' not in os.environ
    target=stage/'capture';target.mkdir(mode=0o700)
    payload=b'PGDMPsynthetic bounded backup bytes'
    (target/'database.dump').write_bytes(payload)
    report={'snapshot_id':'00000003-0000001A-1','reclamation_fenced':True,'completed_at':'2026-09-09T12:00:00+00:00',
            'database':'app','major':18,'files':{'database.dump':{'size':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}}}
    (target/'capture.json').write_text(json.dumps(report))
    (stage/'images.json').write_text(json.dumps(METADATA))
    return target
METADATA='''+repr(self.metadata)+'\n').encode()

    def write(self,name='backup.pyz'):
        path=self.root/name;bundle.write_bundle(self.files,self.manifest,path);return path

    def run_program(self, program, *, arguments=(), interrupt=False, reconcile=False):
        self.assertEqual(program.parent,self.root)
        launcher=['/usr/bin/python3','-I','-B','-c',ROOT_LAUNCHER]
        if os.geteuid()!=0:launcher=['sudo','-n',*launcher]
        selected=self.request if not reconcile else {'kind':'komizo.capture-reconcile','version':1,'application':'app',
            'instance':'production','operation_id':self.operation,'deadline_unix':self.request['deadline_unix']}
        request={'root':str(self.root),'program':program.name,'request':bundle.canonical(selected).decode(),
                 'environment':self.environment,'arguments':list(arguments),'interrupt':interrupt,'reconcile_orphan':reconcile}
        launched=subprocess.run(launcher,input=json.dumps(request),env={'PATH':'/usr/local/bin:/usr/bin:/bin'},
            capture_output=True,text=True,timeout=50)
        self.assertEqual(launched.returncode,0,launched.stderr)
        value=json.loads(launched.stdout)
        self.assertTrue(value['clean'],'root task left plaintext or unexpected output')
        return subprocess.CompletedProcess([],value['returncode'],value['stdout'].encode(),value['stderr'].encode())

    def certificate(self):
        certificate=self.root/'recipient.pem';key=self.root/'owner.key'
        subprocess.run(['/usr/bin/openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=Backup Control',
                        '-keyout',str(key),'-out',str(certificate)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
        key.chmod(0o600);self.files['recipient.pem']=certificate.read_bytes();return key

    def test_deterministic_program_binds_all_source_provider_recipient_and_helper_bytes(self):
        first=self.write();second=self.write('second.pyz')
        self.assertEqual(first.read_bytes(),second.read_bytes());self.assertEqual(first.stat().st_mode & 0o777,0o755)
        manifest,files=bundle.read_bundle(first)
        self.assertEqual(files,self.files);self.assertEqual(manifest['providers'],self.manifest['providers'])
        with self.assertRaises(ValueError):bundle.write_bundle(self.files,self.manifest,first)
        self.files['recipient.pem']+=b'changed';third=self.write('third.pyz')
        self.assertNotEqual(first.read_bytes(),third.read_bytes())

    def test_duplicate_traversal_extra_member_and_changed_inventory_are_refused(self):
        source=self.write()
        for selected in ['duplicate','path','extra','changed']:
            raw=io.BytesIO(bundle.PREFIX);raw.seek(0,2)
            with zipfile.ZipFile(source) as original,zipfile.ZipFile(raw,'w') as archive:
                for member in original.infolist():
                    body=original.read(member)
                    if selected=='changed' and member.filename=='recipient.pem':body=b'changed'
                    archive.writestr(member,body)
                name={'duplicate':'recipient.pem','path':'../outside','extra':'undeclared','changed':None}[selected]
                if name:
                    member=zipfile.ZipInfo(name);member.external_attr=(stat.S_IFREG|0o400)<<16
                    archive.writestr(member,b'rogue')
            path=self.root/(selected+'.pyz');path.write_bytes(raw.getvalue())
            with self.subTest(selected=selected),self.assertRaises(ValueError):bundle.read_bundle(path)

    def test_request_requires_exact_protected_context_scope_deadline_and_closed_json(self):
        self.assertEqual(bundle.request(bundle.canonical(self.request),self.manifest,self.environment),self.request)
        for field,value in [('application','other'),('instance','pr-1'),('operation_id','b'*32),('release_digest','sha256:'+'f'*64),
                            ('deadline_unix',int(time.time())-1),('output_directory',str(self.root/'other'))]:
            selected=dict(self.request,**{field:value})
            with self.subTest(field=field),self.assertRaises(ValueError):bundle.request(bundle.canonical(selected),self.manifest,self.environment)
        for raw in [bundle.canonical(dict(self.request,extra=True)),b'{"kind":1,"kind":2}',bundle.canonical(self.request)+b'\n',b'x'*16385]:
            with self.assertRaises(ValueError):bundle.request(raw,self.manifest,self.environment)

    def test_real_sealed_export_roundtrips_installed_descriptor_and_provider_with_no_plaintext_export(self):
        key=self.certificate();program=self.write()
        before=set((self.tools/'helpers').glob('__pycache__/*'))
        result=self.run_program(program)
        self.assertEqual(result.returncode,0,result.stderr.decode());self.assertEqual(result.stdout,b'')
        self.assertEqual({path.name for path in self.output.iterdir()},{'snapshot.cms','receipt.json'})
        self.assertTrue(all(path.stat().st_mode & 0o777==0o600 for path in self.output.iterdir()))
        snapshot=helper(self.tools/'helpers/snapshot.py','bundle_test_snapshot')
        target=self.root/'unsealed';target.mkdir(mode=0o700)
        restored=snapshot.unseal(self.output,key,target)
        self.assertEqual((target/'database.dump').read_bytes(),b'PGDMPsynthetic bounded backup bytes')
        self.assertEqual(restored['postgresql']['deployment'],self.deployment)
        self.assertEqual(restored['database_restore'],'not_run')
        rejected=self.run_program(program,arguments=['extra'])
        self.assertEqual(set((self.tools/'helpers').glob('__pycache__/*')),before)
        self.assertNotEqual(rejected.returncode,0)

    def test_database_only_capture_retains_all_artifacts_without_api_container_or_identity(self):
        key=self.certificate()
        self.files['helpers/installed_resource.py']=self.files['helpers/installed_resource.py'].replace(
            repr(self.records).encode(),repr({'postgres':self.records['postgres']}).encode())
        result=self.run_program(self.write())
        self.assertEqual(result.returncode,0,result.stderr.decode())
        snapshot=helper(self.tools/'helpers/snapshot.py','bundle_database_only_snapshot')
        target=self.root/'database-only';target.mkdir(mode=0o700)
        restored=snapshot.unseal(self.output,key,target)
        self.assertEqual(restored['postgresql']['images'],self.metadata['images'])
        self.assertEqual(set(restored['postgresql']['images']),{'postgres','api'})
        self.assertEqual(restored['postgresql']['deployment'],self.deployment)
        self.assertNotIn('container_id',bundle.canonical(restored['postgresql']).decode())
        self.assertEqual((target/'database.dump').read_bytes(),b'PGDMPsynthetic bounded backup bytes')

    def test_actual_root_reconciliation_cleans_owned_orphan_without_importing_product_capture(self):
        self.files['__main__.py']=self.files['__main__.py'].replace(
            b"JOURNAL_ROOT = Path('/var/lib/komizo/journal')",("JOURNAL_ROOT = Path("+repr(str(self.root/'journal'))+")").encode()).replace(
            b"EXPORTS_ROOT = Path('/var/lib/komizo/exports')",("EXPORTS_ROOT = Path("+repr(str(self.root))+")").encode())
        self.files['product/deploy/capture.py']=b'raise AssertionError("capture must never import during owner reconciliation")\n'
        result=self.run_program(self.write(),reconcile=True)
        self.assertEqual(result.returncode,0,result.stderr.decode())
        self.assertEqual(json.loads(result.stdout),{'kind':'komizo.capture-reconciled','version':1,'application':'app',
            'instance':'production','operation_id':self.operation,'helpers':0,'helpers_absent':True,'orphan_plaintext_removed':True})
        self.assertEqual(list(self.output.iterdir()),[])

    def test_backup_bundle_does_not_admit_serving_process_prerequisites(self):
        self.manifest['services']=['postgres','api']
        with self.assertRaisesRegex(ValueError,'closed'):self.write()
        self.assertFalse((self.root/'backup.pyz').exists())

    def test_capture_descriptor_substitution_refuses_and_cleans_all_owned_staging(self):
        self.certificate()
        metadata=copy.deepcopy(self.metadata);metadata['deployment']['release_digest']='sha256:'+'f'*64
        self.files['product/deploy/capture.py']=self.files['product/deploy/capture.py'].replace(repr(self.metadata).encode(),repr(metadata).encode())
        result=self.run_program(self.write())
        self.assertNotEqual(result.returncode,0)
        self.assertIn(b'does not bind',result.stderr)
        self.assertEqual(list(self.output.iterdir()),[])

    def test_source_or_resource_changes_and_encryption_failure_cannot_leave_partial_exports(self):
        for failure in ['resource','certificate']:
            with self.subTest(failure=failure):
                original=dict(self.files)
                if failure=='resource':
                    self.files['helpers/installed_resource.py']+=b'\ndef unchanged(records):raise ValueError("resource changed")\n'
                program=self.write(failure+'.pyz')
                result=self.run_program(program)
                self.assertNotEqual(result.returncode,0)
                self.assertIn(b'resource changed' if failure=='resource' else b'CalledProcessError',result.stderr)
                self.assertEqual(list(self.output.iterdir()),[])
                self.files=original

    def test_sigterm_cleans_private_plaintext_and_does_not_publish_an_export(self):
        self.files['product/deploy/capture.py']=b"import signal\nPROJECT='app'\ndef capture(stage):\n (stage/'started').write_text('synthetic private capture')\n signal.pause()\n"
        program=self.write()
        result=self.run_program(program,interrupt=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn(b'protected backup interrupted',result.stderr)
        self.assertEqual(list(self.output.iterdir()),[])

    def test_builder_requires_tracked_clean_source_and_locked_provider_helpers(self):
        import dependencies
        import deployment
        source=self.root/'source';source.mkdir()
        (source/'capture.py').write_bytes(self.files['product/deploy/capture.py'])
        subprocess.run(['git','init','-q',str(source)],check=True)
        subprocess.run(['git','-C',str(source),'remote','add','origin','https://github.com/example/app.git'],check=True)
        subprocess.run(['git','-C',str(source),'add','capture.py'],check=True)
        subprocess.run(['git','-C',str(source),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
                        '-c','commit.gpgsign=false','commit','-qm','Capture fixture'],check=True)
        certificate=self.root/'recipient.pem';certificate.write_bytes(b'-----BEGIN PRIVATE KEY-----\nnever copy private key\n')
        original_tracked=bundle.tracked
        def controlled(root,revision,name):
            if Path(root)==source:return original_tracked(root,revision,name)
            return self.files['__main__.py' if name=='cookbook/backup_bundle.py' else 'helpers/'+Path(name).name if name.startswith('cookbook/') else name]
        with patch.object(dependencies,'tools_root',return_value=self.tools) as tools,patch.object(deployment,'provider_provenance',return_value=self.manifest['providers']) as provider,patch.object(bundle,'tracked',side_effect=controlled):
            with self.assertRaisesRegex(ValueError,'public certificate'):
                bundle.build(source,'app','capture.py',certificate,self.root/'rejected.pyz')
            self.certificate()
            result=bundle.build(source,'app','capture.py',certificate,self.root/'built.pyz')
            manifest,files=bundle.read_bundle(result['program'])
            self.assertEqual(manifest['source']['repository'],'example/app');self.assertEqual(manifest['providers'],self.manifest['providers'])
            self.assertEqual(files['helpers/snapshot.py'],(self.tools/'helpers/snapshot.py').read_bytes())
            self.assertTrue(tools.called and provider.called)
            (source/'untracked.py').write_text('raise RuntimeError()')
            with self.assertRaises(ValueError):
                bundle.build(source,'app','untracked.py',certificate,self.root/'untracked.pyz')
            (source/'capture.py').write_text('changed source')
            with self.assertRaises(subprocess.CalledProcessError):
                bundle.build(source,'app','capture.py',certificate,self.root/'dirty.pyz')
        self.assertFalse((self.root/'rejected.pyz').exists())
        subprocess.run(['git','-C',str(source),'checkout','--','capture.py'],check=True)
        original=original_tracked(source,subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip(),'capture.py')
        def substitute(root,revision,name):
            raw=controlled(root,revision,name)
            if Path(root)==source:(source/name).write_text('raise RuntimeError("concurrent replacement")')
            return raw
        with patch.object(dependencies,'tools_root',return_value=self.tools),patch.object(deployment,'provider_provenance',return_value=self.manifest['providers']),patch.object(bundle,'tracked',side_effect=substitute):
            with self.assertRaises(subprocess.CalledProcessError):
                bundle.build(source,'app','capture.py',certificate,self.root/'raced.pyz')
        self.assertFalse((self.root/'raced.pyz').exists())
        self.assertIn(b'PROJECT',original)


    def test_output_symlink_or_existing_data_is_refused_before_product_code(self):
        program=self.write();sentinel=self.output/'retained';sentinel.write_text('retained')
        result=self.run_program(program)
        self.assertNotEqual(result.returncode,0)
        self.assertIn(b'fresh private directory',result.stderr)
        self.assertEqual(sentinel.read_text(),'retained');sentinel.unlink()
        self.output.rmdir();self.output.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(ValueError):bundle.execute(program,bundle.canonical(self.request),self.environment)
