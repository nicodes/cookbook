from source_fixture import SDK_ROOT
"""Generated native exports stay closed across transported editor imports."""
from contextlib import chdir
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import ci
import godot_ci
import godot_game


class BuildImportBoundary(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name);self.root=self.base/'builder';self.root.mkdir()
        (self.root/'app').mkdir()
        (self.root/'app/project.godot').write_text('config_version=5\n[application]\nconfig/name="Native import fixture"\n[rendering]\nrenderer/rendering_method="gl_compatibility"\n')
        (self.root/'app/assertion.gd').write_text('extends SceneTree\n')
        (self.root/'.gitignore').write_text('.artifacts/\n.godot/\napp/build/\n*.uid\n')
        (self.root/'cookbook.toml').write_text('version=1\nrecipe="godot-game"\n')
        for args in [['init','-q'],['add','.'],['-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','Native fixture']]:
            subprocess.run(['git',*args],cwd=self.root,check=True,stdout=subprocess.DEVNULL)
        self.source=subprocess.check_output(['git','rev-parse','HEAD'],cwd=self.root,text=True).strip()
        self.tree=subprocess.check_output(['git','rev-parse','HEAD^{tree}'],cwd=self.root,text=True).strip()
        self.config={'recipe':'godot-game','components':{'godot':{'exports':[
            {'preset':'Web','output':'app/build/web/index.html'}]}},'artifacts':{'paths':['app/build/web']},
            'tests':{'unit':[{'runner':'godot-check','path':'app/assertion.gd'}]},
            'ci':{'runner':'ubuntu-24.04','godot':{'unit_shards':1}}}
        self.environment={'GITHUB_REPOSITORY':'fixture/game','GITHUB_RUN_ID':'12','GITHUB_RUN_ATTEMPT':'1',
            'GITHUB_WORKFLOW_REF':'fixture/game/.github/workflows/ci.yml@refs/pull/1/merge',
            'GITHUB_WORKFLOW_SHA':'a'*40}
        self.verify=ci.verification()

    def build(self):
        godot_game.prepare_export_import_boundaries(self.root,self.config)
        output=self.root/'app/build/web';output.mkdir(parents=True)
        (output/'index.html').write_bytes(b'accepted native html')
        def chunk(kind,data):
            return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
        png=(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',1,1,8,6,0,0,0))
             +chunk(b'IDAT',zlib.compress(b'\0\xff\0\0\xff'))+chunk(b'IEND',b''))
        (output/'index.png').write_bytes(png)
        with chdir(self.root):
            scope=godot_game.verification_scope(self.root,self.config)
            return {'identity':self.verify.identity(scope),'artifacts':self.verify.artifacts(scope),
                'provenance':{'source_commit':self.source,'source_tree':self.tree,'tracked_source_clean':True,
                    'input_identity':self.verify.identity(scope),
                    'execution_context':dict(self.environment,GITHUB_JOB='build')}}

    def test_strict_packet_transports_and_binds_the_generated_boundary(self):
        build=self.build();self.assertIn('app/build/.gdignore',build['artifacts'])
        entry=next(row for row in godot_ci.plan(self.root,self.config)['requirements'] if row['kind']=='build')
        proof={'version':1,'coverage':'godot-partial','complete_host':False,'status':'passed',
            'repository':'fixture/game','source_commit':self.source,'source_tree':self.tree,'cookbook_commit':'b'*40,
            'configuration_sha256':godot_ci.configuration_hash(self.root),'entry':entry,'executed_cases':[],
            'execution_context':dict(self.environment,GITHUB_JOB='build'),'build':build}
        packet=godot_ci.export_packet(self.root,proof)
        worker=self.base/'worker';subprocess.run(['git','clone','--quiet',str(self.root),str(worker)],check=True)
        accepted=godot_ci.packet(worker,packet,self.config,self.source,'b'*40,entry,self.environment)
        with chdir(worker):
            godot_ci.import_build(worker,self.config,packet,accepted)
            scope=godot_game.verification_scope(worker,self.config)
            self.assertEqual(self.verify.artifacts(scope),build['artifacts'])
            godot_game.prepare_export_import_boundaries(worker,self.config,imported=True)
            self.assertEqual(self.verify.artifacts(scope),build['artifacts'])
            (worker/'app/build/web/unexpected.import').write_bytes(b'foreign output')
            with self.assertRaisesRegex(ValueError,'stale'):
                godot_game.current_build(worker,self.config,self.verify)
            (worker/'app/build/web/unexpected.import').unlink()
            (worker/'app/build/.gdignore').write_bytes(b'changed marker')
            with self.assertRaisesRegex(ValueError,'stale'):
                godot_game.current_build(worker,self.config,self.verify)
            for mode in [{'COOKBOOK_GODOT_ENTRY':'unit-1-1'}, {'COOKBOOK_GODOT_HOST':'accepted-host-matrix'}]:
                with self.subTest(mode=mode),self.assertRaisesRegex(ValueError,'accepted artifact bytes'):
                    godot_game.import_native(worker,self.config,mode)
            (worker/'app/build/.gdignore').unlink()
            for mode in [{'COOKBOOK_GODOT_ENTRY':'unit-1-1'}, {'COOKBOOK_GODOT_HOST':'accepted-host-matrix'}]:
                with self.subTest(mode=mode),self.assertRaisesRegex(ValueError,'may not repair'):
                    godot_game.import_native(worker,self.config,mode)
                self.assertFalse((worker/'app/build/.gdignore').exists())
        marker=packet/'payload/app/build/.gdignore';marker.write_bytes(b'substituted marker')
        with self.assertRaisesRegex(ValueError,'accepted output bytes'):
            godot_ci.packet(worker,packet,self.config,self.source,'b'*40,entry,self.environment)

    def test_boundary_refuses_links_and_nonregular_files_without_replacing_source(self):
        target=self.root/'owned-source';target.write_bytes(b'unchanged')
        build=self.root/'app/build';build.mkdir();marker=build/'.gdignore';marker.symlink_to(target)
        with self.assertRaises(ValueError):godot_game.prepare_export_import_boundaries(self.root,self.config)
        self.assertEqual(target.read_bytes(),b'unchanged');marker.unlink();marker.mkdir()
        with self.assertRaisesRegex(ValueError,'regular file'):
            godot_game.prepare_export_import_boundaries(self.root,self.config)

    @unittest.skipUnless(os.environ.get('GODOT_BIN'),'real native import requires explicitly prepared GODOT_BIN')
    def test_real_native_import_preserves_closed_exports_and_markerless_control_drifts(self):
        build=self.build();scope=godot_game.verification_scope(self.root,self.config)
        env=dict(os.environ,GODOT_BIN=os.environ['GODOT_BIN'],COOKBOOK_GODOT_ENTRY='build')
        with chdir(self.root):
            (self.root/'app/build/.gdignore').unlink()
            godot_game.import_native(self.root,self.config,env)
            self.assertEqual(self.verify.artifacts(scope),build['artifacts'])
            (self.root/'app/build/.gdignore').unlink();shutil.rmtree(self.root/'app/.godot')
            legacy=self.verify.scope(artifacts=['app/build/web']);before=self.verify.artifacts(legacy)
            result=subprocess.run([env['GODOT_BIN'],'--headless','--editor','--import','--quit','--path',str(self.root/'app')],
                capture_output=True,text=True,timeout=45)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertNotEqual(self.verify.artifacts(legacy),before)
            self.assertTrue((self.root/'app/build/web/index.png.import').is_file())


if __name__=='__main__':unittest.main()
