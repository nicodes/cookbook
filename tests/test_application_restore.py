"""Keep owner snapshot authority separate from source CI and public runtime data."""
from source_fixture import SDK_ROOT, write_source
from pathlib import Path
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import application_restore as restore
import application_synthetic as synthetic
import deployment


class SnapshotApplicationBoundary(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.providers={'cookbook_revision':'b'*40,'tools_revision':'c'*40,'core_revision':'d'*40}
        self.release={'kind':'komizo.release','version':1,'application':'demo',
            'source':{'repository':'example/demo','repository_id':'123','revision':'a'*40},'providers':self.providers,
            'artifacts':{'api':{'reference':'ghcr.io/example/demo-api@sha256:'+'e'*64}},'workload':{}}
        self.manifest={'revision':'a'*40,'deployment':{'release':self.release,
            'release_digest':'sha256:'+hashlib.sha256(deployment.canonical(self.release)).hexdigest(),'provider':self.providers}}

    def admit(self, manifest):
        with patch.object(restore,'provider_binding',return_value=self.providers),patch.object(deployment,'native_validate') as native:
            result=restore.admitted_release(manifest,{'project':'demo','image_base':'ghcr.io/example/demo'},self.root,{})
            native.assert_called_once()
            return result

    def test_snapshot_uses_exact_retained_descriptor_and_three_providers(self):
        actual,digest=self.admit(self.manifest)
        self.assertEqual(actual,self.release)
        self.assertEqual(digest,self.manifest['deployment']['release_digest'])
        self.assertEqual((self.root/'retained-release.json').read_bytes(),deployment.canonical(self.release))

    def test_provider_or_descriptor_rebinding_fails_before_any_process(self):
        changed=[]
        value=copy.deepcopy(self.manifest);value['deployment']['provider']['cookbook_revision']='f'*40;changed.append(value)
        value=copy.deepcopy(self.manifest);value['deployment']['release']['providers']['tools_revision']='f'*40;changed.append(value)
        value=copy.deepcopy(self.manifest);value['deployment']['release']['source']['revision']='f'*40;changed.append(value)
        value=copy.deepcopy(self.manifest);value['deployment']['release_digest']='sha256:'+'f'*64;changed.append(value)
        for value in changed:
            with self.subTest(value=value),patch.object(restore,'provider_binding',return_value=self.providers),patch.object(deployment,'native_validate') as native,self.assertRaises(ValueError):
                restore.admitted_release(value,{'project':'demo','image_base':'ghcr.io/example/demo'},self.root,{})
            native.assert_not_called()

    def test_local_packet_cannot_substitute_another_retained_release(self):
        packet=self.root/'packet';packet.mkdir()
        with patch.object(deployment,'verify',return_value=self.release|{'application':'other'}),self.assertRaisesRegex(ValueError,'another retained release'):
            restore.retained_packet(self.root,self.release,packet,{},False)
        with self.assertRaisesRegex(ValueError,'select immutable registry reads'):
            restore.retained_packet(self.root,self.release,self.root/'absent',{},False)

    def test_runtime_credentials_are_replaced_and_do_not_enter_arguments(self):
        dsn=self.root/'synthetic.url';dsn.write_text('postgres://runtime:synthetic@127.0.0.1:5432/demo\n');dsn.chmod(0o600)
        profile={'dsn_transport':'file','dsn_variable':'RUNTIME_DATABASE_URL_FILE','dsn_file':'/run/secrets/database.url'}
        values,mounts=restore.private_credentials(profile,dsn,self.root,os_uid())
        self.assertEqual(values,{'RUNTIME_DATABASE_URL_FILE':'/run/secrets/database.url'})
        self.assertNotIn('synthetic',' '.join(mounts))
        self.assertIn('readonly',' '.join(mounts))
        self.assertEqual((self.root/'runtime.url').stat().st_mode&0o777,0o400)

    def test_disposable_runtime_cannot_publish_ports_or_write_image_filesystem(self):
        record={'user':'10001:10001','limits':{'memory_bytes':134217728,'memory_swap_bytes':134217728,'milli_cpus':500,'pids':64},
            'tmpfs':{'/tmp':{'size_bytes':16777216,'mode':0o1777}}}
        args=restore.container_arguments(record,'owned-database','owned-api')
        self.assertIn('container:owned-database',args);self.assertIn('--read-only',args)
        self.assertIn('--cap-drop=ALL',args);self.assertNotIn('--publish',args)
        self.assertIn('noexec',args[args.index('--tmpfs')+1])
        self.assertEqual(args[args.index('--memory')+1],'134217728')

    def test_failed_bootstrap_never_starts_api_and_always_removes_its_job(self):
        record={'artifact':'api','user':'10001:10001','entrypoint':['/app','migrate'],'timeout_seconds':60,
            'limits':{'memory_bytes':134217728,'memory_swap_bytes':134217728,'milli_cpus':500,'pids':64}}
        release={'workload':{'jobs':{'migrate':record}},'artifacts':{'api':{'config_digest':'sha256:'+'e'*64}}}
        profile={'runtime_role':'demo_runtime','database':'demo','bootstrap':{'job':'migrate','environment':{},'dsn_variable':'OWNER_DATABASE_URL','runtime_dsn_variable':'RUNTIME_DATABASE_URL'}}
        def command(arguments,*args,**kwargs):
            if arguments[0]=='wait':return b'1\n'
            self.assertEqual(arguments[0],'run');self.assertIn('/app',arguments[arguments.index('--entrypoint')+1])
            return b'owned-job\n'
        with patch.object(synthetic,'execute',side_effect=command),patch.object(synthetic.native_runtime,'run',return_value=subprocess.CompletedProcess([],0)) as cleanup,self.assertRaisesRegex(ValueError,'empty synthetic database'):
            synthetic.bootstrap(release,profile,'owned-db','postgres://fixture:synthetic@127.0.0.1:5432/demo',self.root,{})
        self.assertEqual(cleanup.call_args.args[0][:3],['rm','--force','--volumes'])
        self.assertNotIn('api',cleanup.call_args.args[0][-1])

    def test_malformed_first_boot_uses_the_canonical_runtime_dsn_without_a_second_password(self):
        record={'artifact':'api','user':'10001:10001','timeout_seconds':60,
            'limits':{'memory_bytes':134217728,'memory_swap_bytes':134217728,'milli_cpus':500,'pids':64}}
        release={'workload':{'jobs':{'migrate':record}},'artifacts':{'api':{'config_digest':'sha256:'+'e'*64}}}
        profile={'runtime_role':'demo_runtime','database':'demo','bootstrap':{'job':'migrate','environment':{},
            'dsn_variable':'OWNER_DATABASE_URL','runtime_dsn_variable':'RUNTIME_DATABASE_URL'}}
        def command(arguments,*_args,**_kwargs):
            if arguments[0]=='wait':return b'1\n'
            envfile=Path(arguments[arguments.index('--env-file')+1]);values=dict(line.split('=',1) for line in envfile.read_text().splitlines())
            self.assertEqual(set(values),{'OWNER_DATABASE_URL','RUNTIME_DATABASE_URL'})
            self.assertIn('demo_runtime:malformed%01password@127.0.0.1:5432/demo',values['RUNTIME_DATABASE_URL'])
            self.assertNotIn('%01password',' '.join(arguments))
            return b'owned-job\n'
        with patch.object(synthetic,'execute',side_effect=command),patch.object(synthetic.native_runtime,'run',return_value=subprocess.CompletedProcess([],0)):
            self.assertEqual(synthetic.bootstrap(release,profile,'owned-db','postgres://owner:private@127.0.0.1:5432/demo',self.root,{},malformed=True),'1')

    def test_complete_role_inventory_preserves_explicit_namespace_and_exact_project_reader(self):
        profile={'version':1,'project':'demo','image_base':'ghcr.io/example/demo','runtime_role':'runtime_app',
            'api_service':'api','api_port':8080,'health_path':'/readyz','env':{},
            'roles':['runtime_migrator','runtime_app','demo'],'role_namespace':'runtime'}
        path=self.root/'restore.toml';write_source(path,profile)
        self.assertEqual(restore.load_config(path)['roles'],profile['roles'])
        for change in [{'roles':['runtime_migrator','runtime_app','another_project']},
                {'roles':['runtime_migrator','demo']},{'roles':['runtime_app',{'name':'runtime_migrator'}]},
                {'role_namespace':'different'}]:
            write_source(path,profile|change)
            with self.subTest(change=change),self.assertRaises(ValueError):restore.load_config(path)


def os_uid():
    import os
    return os.getuid()


if __name__=='__main__':unittest.main()
