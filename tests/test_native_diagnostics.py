from source_fixture import SDK_ROOT
"""Only explicit generated diagnostics can be retained independently of success."""
import importlib.util
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
spec=importlib.util.spec_from_file_location('native_diagnostics',SDK_ROOT/'helpers/native-diagnostics.py')
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
class NativeDiagnostics(unittest.TestCase):
    def test_react_native_failure_reports_keep_exact_bytes_and_use_existing_closed_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=root/'cookbook.toml'
            source.write_text('version=1\nrecipe="react-native"\n[artifacts]\nevidence_paths=[".artifacts/reports/security"]\nevidence_retention_days={".artifacts/reports/security"={days=3,when="failure"}}\n')
            marker=root/'source-executed'
            (root/'configuration.py').write_text('from pathlib import Path\nPath('+repr(str(marker))+').write_text("executed")\nraise AssertionError("product code must never execute during diagnostic collection")\n')
            reports=root/'.artifacts/reports/security';reports.mkdir(parents=True)
            report=reports/'image-report.json';raw=b'{"Results":[{"Vulnerabilities":[{"Severity":"HIGH","VulnerabilityID":"fixture-blocking-finding"}]}]}\n'
            report.write_bytes(raw)
            identity=hashlib.sha256(source.read_bytes()).hexdigest()
            selected=[{'days':3,'paths':['.artifacts/reports/security']}]
            self.assertEqual(helper.read(root,expected=identity),selected)
            self.assertEqual(helper.read(root,inspect=True,expected=identity,status='failure'),selected)
            for status in ['success','cancelled']:
                self.assertEqual(helper.read(root,inspect=True,expected=identity,status=status),[])
            output=root/'outputs'
            subprocess.run([sys.executable,'-I','-B',str(SDK_ROOT/'helpers/native-diagnostics.py'),
                '--root',str(root),'--inspect','--expected-sha256',identity,'--status','failure'],
                env=dict(os.environ,GITHUB_OUTPUT=str(output)),check=True,timeout=10)
            body=output.read_text()
            self.assertIn('.artifacts/reports/security\n',body)
            self.assertIn('retention-days-1=3\n',body)
            self.assertIn('declaration-sha256='+identity,body)
            self.assertEqual(report.read_bytes(),raw)
            self.assertFalse(marker.exists())
            leaves=['.artifacts/reports/security/api/'+name for name in ['security.json','sbom.json','scan-context.json']]
            (reports/'api').mkdir()
            for name in leaves:(root/name).write_bytes(raw)
            archive=reports/'data-image.tar';archive_marker=b'OCI fixture excluded by explicit leaf selection'
            with archive.open('wb') as stream:stream.write(archive_marker);stream.truncate(1<<30)
            source.write_text('recipe="react-native"\n[artifacts]\nevidence_paths='+json.dumps(leaves)+'\nevidence_retention_days={'+','.join(json.dumps(name)+'={days=3,when="failure"}' for name in leaves)+'}\n')
            identity=hashlib.sha256(source.read_bytes()).hexdigest();output=root/'leaf-outputs'
            subprocess.run([sys.executable,'-I','-B',str(SDK_ROOT/'helpers/native-diagnostics.py'),
                '--root',str(root),'--inspect','--expected-sha256',identity,'--status','failure'],
                env=dict(os.environ,GITHUB_OUTPUT=str(output)),check=True,timeout=10)
            body=output.read_text()
            for name in leaves:self.assertIn(name+'\n',body);self.assertEqual((root/name).read_bytes(),raw)
            self.assertNotIn('data-image.tar',body)
            self.assertNotIn('\n.artifacts/reports/security\n',body)
            self.assertEqual(archive.stat().st_size,1<<30)
            with archive.open('rb') as stream:self.assertEqual(stream.read(len(archive_marker)),archive_marker)

    def test_react_native_changed_or_malicious_declarations_refuse_before_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'cookbook.toml'
            original='recipe="react-native"\n[artifacts]\nevidence_paths=[".artifacts/reports/security"]\nevidence_retention_days={".artifacts/reports/security"={days=3,when="failure"}}\n'
            source.write_text(original);identity=hashlib.sha256(source.read_bytes()).hexdigest()
            source.write_text(original.replace('days=3','days=90'))
            with self.assertRaisesRegex(ValueError,'declaration changed'):
                helper.read(root,inspect=True,expected=identity,status='failure')
            source.write_text(original)
            for path in ['api/src','cookbook.toml','.git/config','.artifacts/../api','tmp','/tmp/reports']:
                config={'recipe':'react-native','artifacts':{'evidence_paths':[path],
                    'evidence_retention_days':{path:{'days':3,'when':'failure'}}}}
                with self.subTest(path=path),self.assertRaises(ValueError):helper.declaration(root,config,inspect=True,status='failure')
            with self.assertRaisesRegex(ValueError,'actual job status'):helper.read(root,inspect=True,expected=identity,status='accepted')
            reports=root/'.artifacts/reports/security';reports.mkdir(parents=True)
            (reports/'source-link').symlink_to(source)
            with self.assertRaisesRegex(ValueError,'bounded unlinked regular'):helper.read(root,inspect=True,expected=identity,status='failure')
            (reports/'source-link').unlink()
            os.link(source,reports/'source-hardlink')
            with self.assertRaisesRegex(ValueError,'bounded unlinked regular'):helper.read(root,inspect=True,expected=identity,status='failure')
            (reports/'source-hardlink').unlink();reports.rmdir();reports.parent.rmdir();reports.parent.parent.rmdir()
            (root/'.artifacts').symlink_to(root,target_is_directory=True)
            with self.assertRaisesRegex(ValueError,'traverse links'):helper.read(root,inspect=True,expected=identity,status='failure')

    def test_retains_only_selected_native_locations_and_does_not_execute_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'cookbook.toml').write_text('recipe="godot-game"\n[artifacts]\nevidence_paths=["tmp/fixture-proof","public-evidence"]\nevidence_retention_days={"tmp/fixture-proof"=14}\n')
            self.assertEqual(helper.read(root),[{'days':14,'paths':['tmp/fixture-proof']}])
            identity=hashlib.sha256((root/'cookbook.toml').read_bytes()).hexdigest()
            self.assertEqual(helper.read(root,expected=identity),[{'days':14,'paths':['tmp/fixture-proof']}])
            with self.assertRaises(ValueError):helper.read(root,expected='a'*64)
            (root/'tmp/fixture-proof').mkdir(parents=True)
            (root/'tmp/fixture-proof/failure.json').write_text('{"success":false}')
            self.assertEqual(helper.read(root,inspect=True),[{'days':14,'paths':['tmp/fixture-proof']}])
    def test_rejects_undeclared_broad_source_injection_and_retention_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for path,days in [('tmp',14),('app',14),('api/source',14),('tmp/../app',14),('tmp/proof\nother',14),('tmp/proof/**',14),('tmp/proof',True),('tmp/proof',91)]:
                config={'recipe':'godot-game','artifacts':{'evidence_paths':[path],'evidence_retention_days':{path:days}}}
                with self.subTest(path=path,days=days),self.assertRaises(ValueError):helper.declaration(root,config)
            config={'recipe':'godot-game','artifacts':{'evidence_paths':['tmp/proof'],'evidence_retention_days':{'tmp/unregistered':14}}}
            with self.assertRaises(ValueError):helper.declaration(root,config)
    def test_refuses_symlink_parent_or_nested_payload_without_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'tmp/proof').mkdir(parents=True)
            config={'recipe':'godot-game','artifacts':{'evidence_paths':['tmp/proof'],'evidence_retention_days':{'tmp/proof':14}}}
            (root/'tmp/proof/leak').symlink_to(root/'cookbook.toml')
            with self.assertRaises(ValueError):helper.declaration(root,config,inspect=True)
    def test_workers_always_inspect_and_upload_separate_from_success_proof(self):
        tools=SDK_ROOT
        for file,count in [('cookbook-godot.yml',3),('cookbook-validation.yml',1)]:
            text=(tools/'.github/workflows'/file).read_text()
            self.assertEqual(text.count('id: native-evidence'),count)
            self.assertEqual(text.count('uses: ./.artifacts/recipe-sdks/_cookbook/native-diagnostics'),count)
            self.assertEqual(text.count('job-status: ${{ job.status }}'),count)
            self.assertEqual(text.count('declaration-sha256: ${{ steps.native-evidence.outputs.declaration-sha256 }}'),count)

    def test_generated_files_mixed_retention_and_actual_source_conditions(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'tmp').mkdir();(root/'tmp/alert.json').write_text('{"status":"failed"}')
            paths=['.artifacts/native/game.tar.gz','capability-evidence','tmp/alert.json','tmp/restore-proof']
            config={'recipe':'godot-game','artifacts':{'evidence_paths':paths,'evidence_retention_days':{
                paths[0]:{'days':14,'when':'success'},paths[1]:7,paths[2]:{'days':0,'when':'failure'},paths[3]:14}}}
            success=helper.declaration(root,config,inspect=True,status='success')
            self.assertEqual(success,[{'days':7,'paths':['capability-evidence']},{'days':14,'paths':['.artifacts/native/game.tar.gz','tmp/restore-proof']}])
            failure=helper.declaration(root,config,inspect=True,status='failure')
            self.assertEqual(failure,[{'days':0,'paths':['tmp/alert.json']},{'days':7,'paths':['capability-evidence']},{'days':14,'paths':['tmp/restore-proof']}])
            self.assertEqual(helper.declaration(root,config,status='cancelled'),[{'days':7,'paths':['capability-evidence']},{'days':14,'paths':['tmp/restore-proof']}])
            config['artifacts']['evidence_retention_days'][paths[3]]=8
            with self.assertRaisesRegex(ValueError,'three'):helper.declaration(root,config)
            config['artifacts']['evidence_retention_days'][paths[3]]={'days':14,'when':'unknown'}
            with self.assertRaises(ValueError):helper.declaration(root,config)

    def test_real_output_reader_emits_default_retention_and_excludes_success_file_after_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'cookbook.toml').write_text('recipe="godot-game"\n[artifacts]\nevidence_paths=["tmp/alert.json",".artifacts/native/game.tar.gz"]\nevidence_retention_days={"tmp/alert.json"={days=0,when="failure"},".artifacts/native/game.tar.gz"={days=14,when="success"}}\n')
            output=root/'outputs'
            identity=hashlib.sha256((root/'cookbook.toml').read_bytes()).hexdigest()
            subprocess.run([sys.executable,str(SDK_ROOT/'helpers/native-diagnostics.py'),
                '--root',str(root),'--inspect','--expected-sha256',identity,'--status','failure'],
                env=dict(os.environ,GITHUB_OUTPUT=str(output)),check=True,timeout=10)
            body=output.read_text()
            self.assertIn('tmp/alert.json',body)
            self.assertIn('retention-days-1=0',body)
            self.assertNotIn('game.tar.gz',body)
            self.assertIn('declaration-sha256='+identity,body)
