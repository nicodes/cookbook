"""Static-only applications scan accepted public bytes without an app image."""
from source_fixture import SDK_ROOT
import copy
import datetime
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import artifact_packet
import deployment
import godot_security as security


class StaticSecurity(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        (self.root/'mise.toml').write_text('[env]\nTRIVY_IMAGE="docker.io/aquasec/trivy@sha256:'+'a'*64+'"\n')
        public=self.root/'public';public.mkdir();(public/'index.html').write_bytes(b'<html>accepted static bytes</html>')
        packet=self.root/'.artifacts/release';(packet/'static').mkdir(parents=True)
        info=artifact_packet.pack_static(public,packet/'static/web.tar.gz',max_bytes=4096,max_file_bytes=1024)
        digest='sha256:'+info['sha256']
        artifact={'kind':'archive','digest':digest,'size_bytes':info['size'],'media_type':deployment.STATIC_MEDIA,
            'reference':'ghcr.io/example/demo-web@sha256:'+hashlib.sha256(deployment.static_manifest(digest,info['size'])).hexdigest()}
        self.release={'source':{'revision':'b'*40},'artifacts':{'web':artifact}}
        (packet/'release.json').write_bytes(deployment.canonical(self.release))
        self.config={'components':{'assets':[{'name':'web','source':'public','max_bytes':4096,'max_file_bytes':1024}],
            'image_security':{'reports':'.artifacts/reports/security'}}}
        self.commands=[];self.results=[];self.mutate=False
        for owner,name,kwargs in [(deployment,'verify',{'return_value':self.release}),
            (security.images,'definition',{'return_value':{'image_base':'ghcr.io/example/demo','inputs':{'images':[]}}}),
            (security.native_runtime,'inspect',{'return_value':[{'Id':'sha256:'+'c'*64,'Config':{'Labels':{'org.opencontainers.image.version':'0.70.0'}}}]})]:
            patched=patch.object(owner,name,**kwargs);patched.start();self.addCleanup(patched.stop)

    def execute(self,arguments,root,environment,**options):
        self.commands.append(arguments)
        self.assertEqual(arguments[:3],['podman','--remote=false','run'])
        self.assertIn('fs',arguments);self.assertEqual(arguments[-1],'/inputs/static')
        self.assertNotIn('--input',arguments)
        output=Path(next(value[:-9] for value in arguments if value.endswith(':/reports')))
        target=Path(arguments[arguments.index('--output')+1]).name
        now=datetime.datetime.now(datetime.timezone.utc).isoformat()
        if target=='security.json':
            report={'SchemaVersion':2,'Trivy':{'Version':'0.70.0'},'ReportID':'12345678-1234-4234-9234-123456789abc',
                'CreatedAt':now,'ArtifactType':'filesystem','ArtifactName':'/inputs/static'}
            if self.results:report['Results']=self.results
        else:
            report={'bomFormat':'CycloneDX','specVersion':'1.6','version':1,'components':[],'vulnerabilities':[],
                'dependencies':[{'ref':'root-filesystem','dependsOn':[]}],
                'metadata':{'timestamp':now,'tools':{'components':[{'group':'aquasecurity','name':'trivy','version':'0.70.0'}]},
                    'component':{'type':'application','name':'/inputs/static','bom-ref':'root-filesystem',
                        'properties':[{'name':'aquasecurity:trivy:SchemaVersion','value':'2'}]}}}
        (output/target).write_text(json.dumps(report))
        if self.mutate:
            source=Path(next(value[:-18] for value in arguments if value.endswith(':/inputs/static:ro')))
            (source/'index.html').write_bytes(b'changed while scanner ran')

    def test_zero_images_have_full_secret_and_sbom_evidence_bound_to_actual_archive(self):
        security.scan(self.root,self.config,{'SOURCE_COMMIT':'b'*40},self.execute)
        self.assertEqual(len(self.commands),2)
        self.assertIn('vuln,secret',self.commands[0]);self.assertIn('cyclonedx',self.commands[1])
        paths=security.evidence_paths(self.root,self.config)
        self.assertEqual(len(paths),3);self.assertTrue(all('/assets/web/' in path for path in paths))
        evidence=json.loads((self.root/paths[-1]).read_text())
        self.assertEqual(evidence['context']['before'],evidence['context']['after'])
        self.assertEqual(evidence['context']['artifact'],self.release['artifacts']['web'])
        self.assertNotIn('ImageID',str(evidence));self.assertNotIn('Reference',str(evidence))

    def test_secret_finding_retains_the_actual_full_report_and_blocks(self):
        self.results=[{'Target':'index.html','Class':'secret','Secrets':[{'RuleID':'synthetic','Severity':'LOW'}]}]
        with self.assertRaisesRegex(ValueError,'detected secrets'):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':'b'*40},self.execute)
        report=json.loads((self.root/'.artifacts/reports/security/assets/web/security.json').read_text())
        self.assertEqual(report['Results'],self.results)
        self.assertTrue((self.root/'.artifacts/reports/security/assets/web/scan-context.json').exists())

    def test_changed_scanned_file_cannot_gain_a_completed_archive_proof(self):
        self.mutate=True
        with self.assertRaisesRegex(ValueError,'scanned filesystem differs'):
            security.scan(self.root,self.config,{'SOURCE_COMMIT':'b'*40},self.execute)
        self.assertFalse((self.root/'.artifacts/reports/security/assets/web/scan-context.json').exists())


if __name__=='__main__':unittest.main()
