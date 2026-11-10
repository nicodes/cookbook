from source_fixture import SDK_ROOT
"""Retain scanner evidence and prevent missing SDK or findings from passing."""
import json
import copy
import datetime
import os
import secrets
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import godot_security as security
from source_fixture import tool_fixture
fixture = tool_fixture('image_scan_fixture')


class ImageSecurityControls(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        (self.root/'mise.toml').write_text('[env]\nTRIVY_IMAGE="docker.io/aquasec/trivy@sha256:'+'a'*64+'"\n')
        self.config={'components':{'image_security':{'reports':'.artifacts/reports/security'}}}
        self.definition={'project':'fixture-game','image_base':'ghcr.io/fixture/game','inputs':{'images':['ghcr.io/fixture/game-api:{revision}']}}
        self.commands=[];self.inspections=[]
        self.source='b'*40;self.reference='ghcr.io/fixture/game-api:'+self.source
        self.archive=self.root/'fixture.tar'
        self.image,self.configuration,self.layer_size=fixture.archive(self.archive,reference=self.reference,source=self.source)
        self.packet=self.root/'.artifacts/release';(self.packet/'images').mkdir(parents=True)
        shutil.copyfile(self.archive,self.packet/'images/api.oci.tar')
        self.source_record={'repository':'fixture/game','repository_id':'123','revision':self.source}
        metadata=security.dependencies.helper('oci_archive').inspect_archive(self.archive,self.reference,self.image['Id'])
        self.release={'kind':'komizo.release','version':1,'application':'fixture-game','source':self.source_record,
            'providers':{'cookbook_revision':'1'*40,'tools_revision':'2'*40,'core_revision':'3'*40},
            'artifacts':{'api':{'kind':'image','reference':'ghcr.io/fixture/game-api@'+metadata['manifest_digest'],
                'digest':metadata['manifest_digest'],'config_digest':self.image['Id'],'platform':metadata['platform']}},
            'workload':{'services':{'api':{'artifact':'api','networks':['app']}},'jobs':{},'assets':{},
                'networks':{'app':{'internal':False}},'volumes':{},
                'routes':[{'host':'api.example.test','path':'/','target':{'kind':'service','name':'api','port':8080}}],
                'readiness':[]}}
        (self.packet/'release.json').write_bytes(security.deployment.canonical(self.release))
        security.artifact_packet.write_inventory(self.packet,{'release.json':'release','images/api.oci.tar':'image'})
        source=patch.object(security.deployment,'source',return_value=self.source_record);source.start();self.addCleanup(source.stop)
        self.actual_size=len(b'value: harmless\n')
        self.report,self.sbom,_=fixture.evidence(self.image,self.configuration,self.layer_size,reference=self.reference,source=self.source)
        self.report['Results']=[{'Target':self.reference,'Class':'os-pkgs','Type':'linux'}]
        self.report['ArtifactName']='/inputs/image.oci'
        self.report['Metadata'].pop('Reference',None)
        self.sbom['metadata']['component']['name']='/inputs/image.oci'
        self.sbom['metadata']['component']['properties']=[item for item in self.sbom['metadata']['component']['properties']
            if item['name']!='aquasecurity:trivy:Reference']
        self.inspector=patch.object(security.native_runtime,'inspect',side_effect=self.inspect)
        self.inspector.start();self.addCleanup(self.inspector.stop)
        oci=security.dependencies.helper('oci_archive')
        exporter=patch.object(oci,'save_image',side_effect=lambda reference,path,**kwargs:shutil.copyfile(self.archive,path))
        self.exporter=exporter.start();self.addCleanup(exporter.stop)

    def inspect(self,references,environment=None):
        self.inspections.extend(references)
        return [copy.deepcopy({'Id':'sha256:'+'a'*64,'Config':{'Labels':{'org.opencontainers.image.version':'0.70.0'}}}
            if reference.startswith('docker.io/aquasec/trivy@') else {**self.image,'Size':self.actual_size})
            for reference in references]

    def execute(self, argv, root, env, **kwargs):
        self.commands.append((argv,kwargs))
        if '--output' in argv:
            filename=Path(argv[argv.index('--output')+1]).name
            if filename=='data-image.tar':
                shutil.copyfile(self.archive,argv[argv.index('--output')+1]);return
            output=self.root/'.artifacts/reports/security/api'/filename
            output.parent.mkdir(parents=True,exist_ok=True)
            value=copy.deepcopy(self.report if filename=='security.json' else self.sbom)
            now=datetime.datetime.now(datetime.timezone.utc).isoformat()
            if filename=='security.json':
                value['CreatedAt']=now
            else:
                value['metadata']['timestamp']=now
                for item in value['metadata']['component']['properties']:
                    if item['name']=='aquasecurity:trivy:Reference':item['value']=argv[-1]
            output.write_text(json.dumps(value))

    def test_full_security_and_sbom_evidence_is_retained_without_product_build(self):
        with patch.object(security.images,'definition',return_value=self.definition):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':'b'*40,'APP_VERSION':'b'*40},self.execute)
            self.assertEqual(len(security.evidence_paths(self.root,self.config)),2)
        self.assertTrue(self.inspections[0].startswith('docker.io/aquasec/trivy@'))
        self.assertEqual(len(self.commands),2)
        self.assertIn('vuln,secret',self.commands[0][0])
        self.assertIn('cyclonedx',self.commands[1][0])
        self.assertIn('/inputs/image.oci',self.commands[0][0]);self.assertIn('--input',self.commands[0][0])
        self.assertFalse(any('build' in argv or 'pull' in argv for argv,_ in self.commands))

    def test_any_secret_high_or_critical_blocks_after_retaining_complete_report(self):
        for results in [{'Secrets':[{'Severity':'LOW'}]}, {'Vulnerabilities':[{'Severity':'HIGH'}]}, {'Vulnerabilities':[{'Severity':'CRITICAL'}]}]:
            self.report['Results']=[{'Target':'fixture','Class':'secret' if 'Secrets' in results else 'os-pkgs',**results}]
            with patch.object(security.images,'definition',return_value=self.definition),self.assertRaises(ValueError):
                security.scan(self.root,self.config,{'SOURCE_COMMIT':'b'*40,'APP_VERSION':'b'*40},self.execute)
            path=self.root/'.artifacts/reports/security/api/security.json'
            self.assertEqual(json.loads(path.read_text())['Results'],self.report['Results'])

    def test_setup_pulls_only_pinned_scanner_and_tests_refuse_missing_sdk(self):
        security.setup(self.root,self.config,{},self.execute)
        self.assertEqual(len(self.commands),1)
        self.assertEqual(self.commands[0][0][:3],['podman','--remote=false','pull'])
        self.assertEqual(self.commands[0][1],{'timeout':300})
        with patch.object(security.images,'definition',return_value=self.definition),patch.object(security.native_runtime,'inspect',side_effect=ValueError('missing scanner')),self.assertRaisesRegex(ValueError,'missing scanner'):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source,'APP_VERSION':self.source},self.execute)

    def test_historical_ref_is_scanned_without_retagging_or_current_source_proof(self):
        reference='ghcr.io/fixture/game-api@'+security.dependencies.helper('oci_archive').inspect_archive(self.archive)['manifest_digest']
        with patch.object(security.images,'definition',return_value=self.definition):
            security.scan_references(self.root,self.config,{'SOURCE_COMMIT':'b'*40,'APP_VERSION':'b'*40},self.execute,[('api',reference)])
        self.assertTrue(all('/inputs/image.oci' in argv for argv,_ in self.commands))
        self.assertFalse(any(any(value in argv for value in ['tag','build','push','pull']) for argv,_ in self.commands))
        self.assertFalse((self.root/'.artifacts/verification').exists())

    def test_historical_scan_inventory_cannot_escape_declared_image_family(self):
        invalid=[[],[('api','ghcr.io/other/game-api@sha256:'+'c'*64)],
            [('api','ghcr.io/fixture/game-api:historical')],[('config','ghcr.io/fixture/game-config@sha256:'+'c'*64)],
            [('api','ghcr.io/fixture/game-api@sha256:'+'C'*64)],
            [('api','ghcr.io/fixture/game-api@sha256:'+'c'*64)]*2]
        with patch.object(security.images,'definition',return_value=self.definition):
            for records in invalid:
                with self.subTest(records=records),self.assertRaises(ValueError):
                    security.scan_references(self.root,self.config,{'SOURCE_COMMIT':'b'*40,'APP_VERSION':'b'*40},self.execute,records)
        self.assertEqual(self.commands,[])

    def test_report_and_sdk_pins_cannot_escape_native_authority(self):
        self.config['components']['image_security']['reports']='app'
        with self.assertRaises(ValueError):security.declaration(self.root,self.config)
        self.config['components']['image_security']['reports']='.artifacts/reports/security'
        (self.root/'mise.toml').write_text('[env]\nTRIVY_IMAGE="aquasec/trivy:latest"\n')
        with self.assertRaises(ValueError):security.declaration(self.root,self.config)

    def test_complete_empty_scan_uses_only_exact_saved_data_image_and_retains_context(self):
        self.report.pop('Results')
        with patch.object(security.images,'definition',return_value=self.definition):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source,'APP_VERSION':self.source},self.execute)
        context=json.loads((self.root/'.artifacts/reports/security/api/scan-context.json').read_text())
        self.assertEqual(context['context']['before'],context['context']['after'])
        self.assertTrue(context['data_archive_sha256'])
        self.assertEqual(context['data_export_sizes'],{'podman_inspect_bytes':self.actual_size,'trivy_layer_stream_bytes':self.layer_size})
        self.assertNotEqual(self.actual_size,self.layer_size)
        self.assertEqual([reference for reference in self.inspections if reference==self.image['Id']],[self.image['Id']]*3)

    def test_small_report_cannot_export_large_or_invalid_actual_image(self):
        self.report.pop('Results')
        for actual in [(16<<20)+1,0,-1,None,True,'10240']:
            self.actual_size=actual;self.commands.clear()
            with self.subTest(actual=actual),patch.object(security.images,'definition',return_value=self.definition),self.assertRaisesRegex(ValueError,'actual inspected image sizes'):
                security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source,'APP_VERSION':self.source},self.execute)
            self.assertFalse(any(argv[:3]==['docker','image','save'] for argv,_ in self.commands))

    def test_report_size_must_match_its_scanned_layer_domain_before_export(self):
        self.report.pop('Results');self.report['Metadata']['Size']=1
        with patch.object(security.images,'definition',return_value=self.definition),self.assertRaisesRegex(ValueError,'scanned layer streams'):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source,'APP_VERSION':self.source},self.execute)
        self.assertFalse(any(argv[:3]==['docker','image','save'] for argv,_ in self.commands))

    def test_actual_tag_source_and_size_are_rechecked_immediately_before_export(self):
        self.report.pop('Results')
        for field in ['Id','source','Size','after_size']:
            count=0;self.commands.clear()
            def changed(references,environment=None):
                nonlocal count
                body=self.inspect(references,environment)
                if references==[self.image['Id']]:
                    count+=1
                    if field=='after_size' and count==2:body[0]['Size']+=1
                    if field!='after_size' and count==3:
                        if field=='source':body[0]['Config']['Labels']['org.opencontainers.image.revision']='c'*40
                        elif field=='Size':body[0]['Size']+=1
                        else:body[0]['Id']='sha256:'+'f'*64
                return body
            error='actual inspected image sizes' if field=='after_size' else 'changed before export'
            with self.subTest(field=field),patch.object(security.images,'definition',return_value=self.definition),patch.object(security.native_runtime,'inspect',side_effect=changed),self.assertRaisesRegex(ValueError,error):
                security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source,'APP_VERSION':self.source},self.execute)
            self.assertEqual(count,2 if field=='after_size' else 3)
            self.assertFalse(any(argv[:3]==['docker','image','save'] for argv,_ in self.commands))

    def test_stale_missing_linked_or_failed_scan_outputs_cannot_be_reused(self):
        output=self.root/'.artifacts/reports/security/api';output.mkdir(parents=True)
        for name in ['security.json','sbom.json']:(output/name).write_text(json.dumps(self.report if name=='security.json' else self.sbom))
        with patch.object(security.images,'definition',return_value=self.definition),self.assertRaises(FileNotFoundError):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source,'APP_VERSION':self.source},lambda *args,**kwargs:None)
        self.assertFalse((output/'security.json').exists());self.assertFalse((output/'sbom.json').exists())
        outside=self.root/'outside.json';outside.write_text('original')
        (output/'security.json').symlink_to(outside)
        with patch.object(security.images,'definition',return_value=self.definition),self.assertRaises(ValueError):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source,'APP_VERSION':self.source},self.execute)
        self.assertEqual(outside.read_text(),'original')
        (output/'security.json').unlink()
        def failed(*args,**kwargs):raise subprocess.CalledProcessError(1,args[0])
        with patch.object(security.images,'definition',return_value=self.definition),self.assertRaises(subprocess.CalledProcessError):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source,'APP_VERSION':self.source},failed)

    def test_actual_image_must_match_imported_release_id_map_when_present(self):
        with patch.object(security.images,'definition',return_value=self.definition):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source,'APP_VERSION':self.source},self.execute)
        self.assertTrue(json.loads((self.root/'.artifacts/reports/security/api/scan-context.json').read_text())['accepted_release_sha256'])
        self.assertIn(self.image['Id'],self.inspections)
        self.assertNotIn(self.reference,self.inspections)
        self.commands.clear()
        def changed(references,environment=None):
            values=self.inspect(references,environment)
            if references==[self.image['Id']]:values[0]['Id']='sha256:'+'f'*64
            return values
        with patch.object(security.images,'definition',return_value=self.definition),patch.object(security.native_runtime,'inspect',side_effect=changed),self.assertRaisesRegex(ValueError,'accepted source build identity'):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source,'APP_VERSION':self.source},self.execute)
        self.assertEqual(self.commands,[])

    def test_shipping_scan_reuses_exact_packet_without_export_or_packet_cleanup(self):
        self.report['Results']=[]
        original={path: (self.packet/path).read_bytes() for path in security.artifact_packet.packet_files(self.packet)}
        with patch.object(security.images,'definition',return_value=self.definition):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source,'APP_VERSION':self.source},self.execute)
        self.exporter.assert_not_called()
        copied=self.root/'.artifacts/reports/security/api/data-image.tar'
        self.assertEqual(copied.read_bytes(),original['images/api.oci.tar'])
        self.assertNotEqual(copied.stat().st_ino,(self.packet/'images/api.oci.tar').stat().st_ino)
        self.assertEqual(original,{path:(self.packet/path).read_bytes() for path in security.artifact_packet.packet_files(self.packet)})
        context=json.loads((copied.parent/'scan-context.json').read_bytes())['context']
        security.CONTEXT_READER.authenticate_archive(copied,context,self.image)

    def test_shipping_oci_can_exceed_data_only_limit_in_cleanup_and_postcheck(self):
        output=self.root/'.artifacts/reports/security/api';output.mkdir(parents=True)
        shutil.copyfile(self.archive,output/'data-image.tar')
        data_limit=self.archive.stat().st_size-1
        with patch.object(security.images,'definition',return_value=self.definition),patch.object(security.CONTEXT_READER,'MAX_ARCHIVE',data_limit):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source,'APP_VERSION':self.source},self.execute)
        self.assertEqual(len(self.commands),2);self.exporter.assert_not_called()
        self.assertFalse((output/'data-image.tar').exists())
        self.assertTrue((self.packet/'images/api.oci.tar').exists())

    def test_empty_data_classification_keeps_its_independent_archive_limit(self):
        self.report['Results']=[]
        with patch.object(security.images,'definition',return_value=self.definition),patch.object(security.CONTEXT_READER,'MAX_ARCHIVE',self.archive.stat().st_size-1),self.assertRaisesRegex(ValueError,'bounded regular'):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source,'APP_VERSION':self.source},self.execute)
        self.assertFalse((self.root/'.artifacts/reports/security/api/scan-context.json').exists())

    def test_missing_or_changed_packet_refuses_before_scanning(self):
        archive=self.packet/'images/api.oci.tar';original=archive.read_bytes()
        archive.write_bytes(original+b'changed')
        with patch.object(security.images,'definition',return_value=self.definition),self.assertRaisesRegex(ValueError,'accepted member bytes'):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source,'APP_VERSION':self.source},self.execute)
        self.assertEqual(self.commands,[]);self.exporter.assert_not_called()
        archive.write_bytes(original);(self.packet/'inventory.json').unlink()
        with patch.object(security.images,'definition',return_value=self.definition),self.assertRaises(FileNotFoundError):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source,'APP_VERSION':self.source},self.execute)
        self.assertEqual(self.commands,[])

    def test_verified_descriptor_must_match_the_captured_raw_bytes(self):
        original=security.deployment.verify
        def changed(*args,**kwargs):
            value=original(*args,**kwargs)
            value['workload']['readiness']=[{'url':'https://api.example.test/changed','status':200}]
            return value
        with patch.object(security.images,'definition',return_value=self.definition),patch.object(security.deployment,'verify',side_effect=changed),self.assertRaisesRegex(ValueError,'packet changed during image scan admission'):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source,'APP_VERSION':self.source},self.execute)
        self.assertEqual(self.commands,[]);self.exporter.assert_not_called()

    def test_packet_mutation_during_scan_cannot_emit_successful_context(self):
        original=self.packet/'images/api.oci.tar'
        def changed(*args,**kwargs):
            self.execute(*args,**kwargs)
            if len(self.commands)==1:
                with original.open('ab') as stream:stream.write(b'concurrent change')
        with patch.object(security.images,'definition',return_value=self.definition),self.assertRaisesRegex(ValueError,'accepted image packet changed'):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source,'APP_VERSION':self.source},changed)
        self.assertFalse((self.root/'.artifacts/reports/security/api/scan-context.json').exists())
        self.assertTrue(original.exists())

    def test_evidence_cannot_be_written_inside_accepted_packet(self):
        self.config['components']['image_security']['reports']='.artifacts/release/reports'
        with self.assertRaisesRegex(ValueError,'outside the accepted release packet'):
            security.declaration(self.root,self.config)


class NativeImportedScanArchive(unittest.TestCase):
    def test_native_source_archive_survives_unnamed_import_and_strict_shipping_scan(self):
        if sys.platform!='linux':
            self.skipTest('native OCI build/import acceptance requires Linux')
        if shutil.which('podman') is None:
            if os.environ.get('GITHUB_ACTIONS')=='true':
                self.fail('the provider must prepare native Podman for shipping scan acceptance')
            self.skipTest('native image chain runs in the prepared provider CI')
        runtime=security.dependencies.helper('container_runtime')
        oci=security.dependencies.helper('oci_archive')
        revision=secrets.token_hex(20)
        family='ghcr.io/example/scan-'+secrets.token_hex(12)
        reference=family+'-api:'+revision
        environment=dict(os.environ,APP_VERSION=revision,SOURCE_COMMIT=revision)
        identity=None
        self.assertEqual(runtime.run(['image','exists',reference],environment=environment,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30).returncode,1)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);build=root/'build';build.mkdir()
            (build/'data').mkdir()
            (build/'data/settings.yaml').write_text('value: harmless\n')
            # One-MiB writes keep the fixture bounded; random payload stays large
            # whether the actual engine saves compressed or plain OCI layers.
            with (build/'data/payload.bin').open('xb') as output:
                for _ in range(40):output.write(secrets.token_bytes(1<<20))
            (build/'Containerfile').write_text('FROM scratch\nCOPY data /data/\n'
                +'LABEL org.opencontainers.image.revision="'+revision+'"\n'
                +'USER 10001:10001\nCMD ["/data/settings.yaml"]\n')
            scanner=fixture.SCANNER
            (root/'mise.toml').write_text('[env]\nTRIVY_IMAGE="'+scanner+'"\n')
            config={'components':{'image_security':{'reports':'.artifacts/reports/security'}}}
            definition={'project':'scan-fixture','image_base':family,'inputs':{'images':[family+'-api:{revision}']}}
            packet=root/'.artifacts/release';(packet/'images').mkdir(parents=True)
            source={'repository':'example/scan-fixture','repository_id':'123','revision':revision}
            try:
                runtime.run(['build','--format','oci','--network','none','--tag',reference,
                    '--file',str(build/'Containerfile'),str(build)],environment=environment,check=True,
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60)
                built=runtime.inspect_images([reference],environment=environment)[0];identity=built['Id']
                metadata=oci.save_image(reference,packet/'images/api.oci.tar',environment=environment,timeout=60)
                release={'kind':'komizo.release','version':1,'application':'scan-fixture','source':source,
                    'providers':{'cookbook_revision':'1'*40,'tools_revision':'2'*40,'core_revision':'3'*40},
                    'artifacts':{'api':{'kind':'image','reference':family+'-api@'+metadata['manifest_digest'],
                        'digest':metadata['manifest_digest'],'config_digest':identity,'platform':metadata['platform']}},
                    'workload':{'services':{'api':{'artifact':'api'}},'jobs':{},'assets':{},'networks':{},'volumes':{},
                        'routes':[{'host':'api.example.test','path':'/','target':{'kind':'service','name':'api','port':8080}}],
                        'readiness':[]}}
                (packet/'release.json').write_bytes(security.deployment.canonical(release))
                security.artifact_packet.write_inventory(packet,{'release.json':'release','images/api.oci.tar':'image'})
                self.assertGreater((packet/'images/api.oci.tar').stat().st_size,security.CONTEXT_READER.MAX_ARCHIVE)
                self.assertLess((packet/'images/api.oci.tar').stat().st_size,oci.MAX_ARCHIVE)
                original={name:security.artifact_packet.digest_file(packet/name) for name in security.artifact_packet.packet_files(packet)}
                runtime.run(['untag',identity,reference],environment=environment,check=True,timeout=30)
                runtime.run(['rmi',identity],environment=environment,check=True,timeout=30)
                oci.load_image(packet/'images/api.oci.tar',reference,identity,environment=environment,timeout=60)
                self.assertEqual(runtime.run(['image','exists',reference],environment=environment,
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30).returncode,1)
                imported=runtime.inspect_images([identity],environment=environment)[0]
                self.assertEqual(imported['Id'],identity);self.assertEqual(imported.get('RepoTags') or [],[])
                report,sbom,_=fixture.evidence(imported,metadata['config'],metadata['layers'][0]['size'],
                    reference=reference,source=revision)
                report['Results']=[{'Target':'/inputs/image.oci','Class':'os-pkgs','Type':'linux'}]
                report['ArtifactName']='/inputs/image.oci';report['Metadata'].pop('Reference',None)
                sbom['metadata']['component']['name']='/inputs/image.oci'
                sbom['metadata']['component']['properties']=[item for item in sbom['metadata']['component']['properties']
                    if item['name']!='aquasecurity:trivy:Reference']
                selected=[];commands=[]
                def inspect(references,environment=None):
                    selected.extend(references)
                    if references==[scanner]:
                        return [{'Id':'sha256:'+'a'*64,'Config':{'Labels':{'org.opencontainers.image.version':'0.70.0'}}}]
                    return runtime.inspect_images(references,environment=environment)
                def scan_reports(argv,directory,env,**kwargs):
                    # Scanner reports are synthetic; build/save/load/config selection,
                    # packet admission, materialization and strict Tools auth are real.
                    commands.append(argv)
                    self.assertIn('--input',argv)
                    self.assertEqual(argv[argv.index('--input')+1],'/inputs/image.oci')
                    layout=Path(argv[argv.index('-v')+1].rsplit(':',2)[0])
                    manifest=layout/'blobs/sha256'/metadata['manifest_digest'][7:]
                    self.assertEqual('sha256:'+security.hashlib.sha256(manifest.read_bytes()).hexdigest(),metadata['manifest_digest'])
                    name=Path(argv[argv.index('--output')+1]).name
                    value=copy.deepcopy(report if name=='security.json' else sbom)
                    now=datetime.datetime.now(datetime.timezone.utc).isoformat()
                    if name=='security.json':value['CreatedAt']=now
                    else:value['metadata']['timestamp']=now
                    (root/'.artifacts/reports/security/api'/name).write_text(json.dumps(value))
                with patch.object(security.images,'definition',return_value=definition), \
                        patch.object(security.deployment,'source',return_value=source), \
                        patch.object(security.native_runtime,'inspect',side_effect=inspect), \
                        patch.object(oci,'save_image',wraps=oci.save_image) as save, \
                        patch.object(security.CONTEXT_READER,'authenticate_archive',wraps=security.CONTEXT_READER.authenticate_archive) as authenticate:
                    for repeat in range(2):
                        if repeat:
                            # A prior interrupted scan can leave an owned OCI
                            # input; its cleanup also uses the shipping limit.
                            with (packet/'images/api.oci.tar').open('rb') as source_stream, (root/'.artifacts/reports/security/api/data-image.tar').open('xb') as target:
                                shutil.copyfileobj(source_stream,target,1<<20)
                        security.scan_references(root,config,environment,scan_reports,[('api',reference)])
                    save.assert_not_called();self.assertEqual(authenticate.call_count,2)
                self.assertEqual(len(commands),4)
                self.assertIn(identity,selected);self.assertNotIn(reference,selected)
                context=json.loads((root/'.artifacts/reports/security/api/scan-context.json').read_bytes())
                self.assertEqual(context['context']['archive']['sha256'],metadata['archive_sha256'])
                self.assertEqual(context['context']['before'],context['context']['after'])
                self.assertEqual(original,{name:security.artifact_packet.digest_file(packet/name) for name in security.artifact_packet.packet_files(packet)})
                self.assertFalse((root/'.artifacts/reports/security/api/data-image.tar').exists())
            finally:
                if identity is not None:
                    exists=runtime.run(['image','exists',identity],environment=environment,
                        stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30).returncode
                    self.assertIn(exists,{0,1})
                    if exists==0:
                        owned=runtime.inspect_images([identity],environment=environment)[0]
                        self.assertEqual(owned['Config']['Labels']['org.opencontainers.image.revision'],revision)
                        self.assertTrue(set(owned.get('RepoTags') or [])<={reference})
                        runtime.run(['rmi',identity],environment=environment,check=True,timeout=30)


if __name__=='__main__':unittest.main()
