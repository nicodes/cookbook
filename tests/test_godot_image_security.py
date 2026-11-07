from source_fixture import SDK_ROOT
"""Retain scanner evidence and prevent missing SDK or findings from passing."""
import json
import copy
import datetime
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
        (self.root/'mise.toml').write_text('[env]\nTRIVY_IMAGE="aquasec/trivy@sha256:'+'a'*64+'"\n')
        self.config={'components':{'image_security':{'reports':'.artifacts/reports/security'}}}
        self.definition={'project':'fixture-game','image_base':'ghcr.io/fixture/game','inputs':{'images':['ghcr.io/fixture/game-gate:{revision}']}}
        self.commands=[];self.inspections=[]
        self.source='b'*40;self.reference='ghcr.io/fixture/game-gate:'+self.source
        self.archive=self.root/'fixture.tar'
        self.image,self.configuration,self.layer_size=fixture.archive(self.archive,reference=self.reference,source=self.source)
        self.actual_size=len(b'value: harmless\n')
        self.report,self.sbom,_=fixture.evidence(self.image,self.configuration,self.layer_size,reference=self.reference,source=self.source)
        self.report['Results']=[{'Target':self.reference,'Class':'os-pkgs','Type':'linux'}]
        self.inspector=patch.object(security.subprocess,'check_output',side_effect=self.inspect)
        self.inspector.start();self.addCleanup(self.inspector.stop)

    def inspect(self,argv,**kwargs):
        self.inspections.append(argv)
        self.assertEqual(argv[:3],['docker','image','inspect'])
        value={'Id':'sha256:'+'a'*64,'Config':{'Labels':{'org.opencontainers.image.version':'0.70.0'}}} if argv[-1].startswith('aquasec/trivy@') else {**self.image,'Size':self.actual_size}
        return json.dumps([value]).encode()

    def execute(self, argv, root, env, **kwargs):
        self.commands.append((argv,kwargs))
        if '--output' in argv:
            filename=Path(argv[argv.index('--output')+1]).name
            if filename=='data-image.tar':
                shutil.copyfile(self.archive,argv[argv.index('--output')+1]);return
            output=self.root/'.artifacts/reports/security/gate'/filename
            output.parent.mkdir(parents=True,exist_ok=True)
            value=copy.deepcopy(self.report if filename=='security.json' else self.sbom)
            now=datetime.datetime.now(datetime.timezone.utc).isoformat()
            if filename=='security.json':
                value['CreatedAt']=now;value['ArtifactName']=argv[-1];value['Metadata']['Reference']=argv[-1]
            else:
                value['metadata']['timestamp']=now;value['metadata']['component']['name']=argv[-1]
                for item in value['metadata']['component']['properties']:
                    if item['name']=='aquasecurity:trivy:Reference':item['value']=argv[-1]
            output.write_text(json.dumps(value))

    def test_full_security_and_sbom_evidence_is_retained_without_product_build(self):
        with patch.object(security.images,'definition',return_value=self.definition):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':'b'*40},self.execute)
            self.assertEqual(len(security.evidence_paths(self.root,self.config)),2)
        self.assertEqual(self.inspections[0][:3],['docker','image','inspect'])
        self.assertEqual(len(self.commands),2)
        self.assertIn('vuln,secret',self.commands[0][0])
        self.assertIn('cyclonedx',self.commands[1][0])
        self.assertIn('docker',self.commands[0][0][self.commands[0][0].index('--image-src')+1:])
        self.assertFalse(any('build' in argv or 'pull' in argv for argv,_ in self.commands))

    def test_any_secret_high_or_critical_blocks_after_retaining_complete_report(self):
        for results in [{'Secrets':[{'Severity':'LOW'}]}, {'Vulnerabilities':[{'Severity':'HIGH'}]}, {'Vulnerabilities':[{'Severity':'CRITICAL'}]}]:
            self.report['Results']=[{'Target':'fixture','Class':'secret' if 'Secrets' in results else 'os-pkgs',**results}]
            with patch.object(security.images,'definition',return_value=self.definition),self.assertRaises(ValueError):
                security.scan(self.root,self.config,{'SOURCE_COMMIT':'b'*40},self.execute)
            path=self.root/'.artifacts/reports/security/gate/security.json'
            self.assertEqual(json.loads(path.read_text())['Results'],self.report['Results'])

    def test_setup_pulls_only_pinned_scanner_and_tests_refuse_missing_sdk(self):
        security.setup(self.root,self.config,{},self.execute)
        self.assertEqual(len(self.commands),1)
        self.assertEqual(self.commands[0][0][:2],['docker','pull'])
        self.assertEqual(self.commands[0][1],{'timeout':300})
        def missing(argv,*args,**kwargs):
            self.assertEqual(argv[:3],['docker','image','inspect'])
            raise subprocess.CalledProcessError(1,argv)
        with patch.object(security.images,'definition',return_value=self.definition),patch.object(security.subprocess,'check_output',side_effect=missing),self.assertRaises(subprocess.CalledProcessError):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':'b'*40},self.execute)

    def test_historical_ref_is_scanned_without_retagging_or_current_source_proof(self):
        reference='ghcr.io/fixture/game-gate@sha256:'+'c'*64
        with patch.object(security.images,'definition',return_value=self.definition):
            security.scan_references(self.root,self.config,{'SOURCE_COMMIT':'b'*40},self.execute,[('gate',reference)])
        self.assertEqual([argv[-1] for argv,_ in self.commands],[reference,reference])
        self.assertFalse(any(any(value in argv for value in ['tag','build','push','pull']) for argv,_ in self.commands))
        self.assertFalse((self.root/'.artifacts/verification').exists())

    def test_historical_scan_inventory_cannot_escape_declared_image_family(self):
        invalid=[[],[('gate','ghcr.io/other/game-gate@sha256:'+'c'*64)],
            [('gate','ghcr.io/fixture/game-gate:historical')],[('config','ghcr.io/fixture/game-config@sha256:'+'c'*64)],
            [('gate','ghcr.io/fixture/game-gate@sha256:'+'C'*64)],
            [('gate','ghcr.io/fixture/game-gate@sha256:'+'c'*64)]*2]
        with patch.object(security.images,'definition',return_value=self.definition):
            for records in invalid:
                with self.subTest(records=records),self.assertRaises(ValueError):
                    security.scan_references(self.root,self.config,{'SOURCE_COMMIT':'b'*40},self.execute,records)
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
            security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source},self.execute)
        self.assertEqual(self.commands[-1][0][:3],['docker','image','save'])
        self.assertEqual(self.commands[-1][0][-1],self.reference)
        self.assertEqual(self.commands[-1][1],{'timeout':60})
        context=json.loads((self.root/'.artifacts/reports/security/gate/scan-context.json').read_text())
        self.assertEqual(context['context']['before'],context['context']['after'])
        self.assertTrue(context['data_archive_sha256'])
        self.assertEqual(context['data_export_sizes'],{'docker_inspect_bytes':self.actual_size,'trivy_layer_stream_bytes':self.layer_size})
        self.assertNotEqual(self.actual_size,self.layer_size)
        self.assertEqual([argv[-1] for argv in self.inspections if argv[-1]==self.reference],[self.reference]*3)

    def test_small_report_cannot_export_large_or_invalid_actual_image(self):
        self.report.pop('Results')
        for actual in [(16<<20)+1,0,-1,None,True,'10240']:
            self.actual_size=actual;self.commands.clear()
            with self.subTest(actual=actual),patch.object(security.images,'definition',return_value=self.definition),self.assertRaisesRegex(ValueError,'actual inspected image sizes'):
                security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source},self.execute)
            self.assertFalse(any(argv[:3]==['docker','image','save'] for argv,_ in self.commands))

    def test_report_size_must_match_its_scanned_layer_domain_before_export(self):
        self.report.pop('Results');self.report['Metadata']['Size']=1
        with patch.object(security.images,'definition',return_value=self.definition),self.assertRaisesRegex(ValueError,'scanned layer streams'):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source},self.execute)
        self.assertFalse(any(argv[:3]==['docker','image','save'] for argv,_ in self.commands))

    def test_actual_tag_source_and_size_are_rechecked_immediately_before_export(self):
        self.report.pop('Results')
        for field in ['Id','source','Size','after_size']:
            count=0;self.commands.clear()
            def changed(argv,**kwargs):
                nonlocal count
                body=json.loads(self.inspect(argv,**kwargs))
                if argv[-1]==self.reference:
                    count+=1
                    if field=='after_size' and count==2:body[0]['Size']+=1
                    if field!='after_size' and count==3:
                        if field=='source':body[0]['Config']['Labels']['org.opencontainers.image.revision']='c'*40
                        elif field=='Size':body[0]['Size']+=1
                        else:body[0]['Id']='sha256:'+'f'*64
                return json.dumps(body).encode()
            error='actual inspected image sizes' if field=='after_size' else 'changed before export'
            with self.subTest(field=field),patch.object(security.images,'definition',return_value=self.definition),patch.object(security.subprocess,'check_output',side_effect=changed),self.assertRaisesRegex(ValueError,error):
                security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source},self.execute)
            self.assertEqual(count,2 if field=='after_size' else 3)
            self.assertFalse(any(argv[:3]==['docker','image','save'] for argv,_ in self.commands))

    def test_stale_missing_linked_or_failed_scan_outputs_cannot_be_reused(self):
        output=self.root/'.artifacts/reports/security/gate';output.mkdir(parents=True)
        for name in ['security.json','sbom.json']:(output/name).write_text(json.dumps(self.report if name=='security.json' else self.sbom))
        with patch.object(security.images,'definition',return_value=self.definition),self.assertRaises(FileNotFoundError):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source},lambda *args,**kwargs:None)
        self.assertFalse((output/'security.json').exists());self.assertFalse((output/'sbom.json').exists())
        outside=self.root/'outside.json';outside.write_text('original')
        (output/'security.json').symlink_to(outside)
        with patch.object(security.images,'definition',return_value=self.definition),self.assertRaises(ValueError):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source},self.execute)
        self.assertEqual(outside.read_text(),'original')
        (output/'security.json').unlink()
        def failed(*args,**kwargs):raise subprocess.CalledProcessError(1,args[0])
        with patch.object(security.images,'definition',return_value=self.definition),self.assertRaises(subprocess.CalledProcessError):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source},failed)

    def test_actual_image_must_match_imported_release_id_map_when_present(self):
        path=self.root/'.artifacts/release/release.json';path.parent.mkdir(parents=True)
        release={'project':'fixture-game','revision':self.source,'images':{self.reference:self.image['Id']}}
        path.write_text(json.dumps(release))
        with patch.object(security.images,'definition',return_value=self.definition):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source},self.execute)
        self.assertTrue(json.loads((self.root/'.artifacts/reports/security/gate/scan-context.json').read_text())['accepted_release_sha256'])
        self.commands.clear();release['images'][self.reference]='sha256:'+'f'*64;path.write_text(json.dumps(release))
        with patch.object(security.images,'definition',return_value=self.definition),self.assertRaisesRegex(ValueError,'accepted source build identity'):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':self.source},self.execute)
        self.assertEqual(self.commands,[])


if __name__=='__main__':unittest.main()
