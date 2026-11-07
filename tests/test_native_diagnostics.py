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
