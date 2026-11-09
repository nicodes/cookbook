from source_fixture import SDK_ROOT
"""Native products and static configuration join the same closed build proof."""
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import godot_game as game


class GodotBuildHooks(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        environment=patch.dict(os.environ);environment.start();self.addCleanup(environment.stop)
        (self.root/'app').mkdir();(self.root/'app/project.godot').write_text('config_version=5\n')
        self.config={'components':{'godot':{'exports':[{'preset':'Web','output':'app/build/web/index.html'}]}},
            'artifacts':{'paths':['app/build/web']}}

    def outputs(self):
        directory=self.root/'app/build/web';directory.mkdir(parents=True)
        for suffix in ['.html','.wasm','.pck','.js']:(directory/('index'+suffix)).write_bytes(b'accepted export')

    def test_staged_native_variants_and_static_blueprints_are_automatically_scoped(self):
        self.outputs();(self.root/'.artifacts/release').mkdir(parents=True)
        self.config['components']['rust']=[{'manifest':'Cargo.toml','binaries':['server'],'output_directory':'.artifacts/native/release'},
            {'manifest':'Cargo.toml','binaries':['server'],'output_directory':'.artifacts/native/fixture'}]
        self.config['artifacts']['release_metadata']=['.artifacts/release/static-config.json','.artifacts/release/godot-release-manifest.json']
        scope=game.verification_scope(self.root,self.config)
        self.assertEqual(set(scope['artifacts']),{'app/build/web','app/build/.gdignore','.artifacts/native/release/server',
            '.artifacts/native/fixture/server','.artifacts/release/static-config.json','.artifacts/release/godot-release-manifest.json'})
        for name in scope['artifacts'][1:]:
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'accepted native fixture')
        verification=game.helper('verification')
        for args in [['init','-q'],['add','.'],['-c','user.name=Controls','-c','user.email=controls@example.invalid','commit','-qm','fixture']]:
            subprocess.run(['git','-C',str(self.root),*args],check=True)
        previous=Path.cwd();os.chdir(self.root);self.addCleanup(os.chdir,previous)
        before=verification.artifacts(scope)
        (self.root/'.artifacts/native/fixture/server').write_bytes(b'wrong feature variant')
        self.assertNotEqual(before,verification.artifacts(scope))

    def test_packet_is_frozen_before_static_variants_and_build_acceptance(self):
        self.config['components']['assets']=[{'name':'web','source':'public'}]
        events=[]
        with patch.object(game.godot_sdk,'activate',return_value={}),patch.object(game,'build_rust'),\
             patch.object(game.godot_package,'extensions'),patch.object(game.godot_package,'author'),\
             patch.object(game.godot_package,'runtime'),patch.object(game.godot_package,'packs'),\
             patch.object(game.godot_package,'package'),patch.object(game,'helper'),patch.object(game,'validate_outputs'),\
             patch.object(game.images,'validate_application',side_effect=lambda *a:events.append('application')),\
             patch.object(game.images,'build',side_effect=lambda *a,**kw:events.append('images')),\
             patch.object(game.image_fixtures,'package'),\
             patch.object(game.asset_fixtures,'package',side_effect=lambda *a,**kw:events.append('static-variants')),\
             patch.object(game.images,'package',side_effect=lambda *a:events.append('packet')):
            game.compile_outputs(self.root,self.config,{'GODOT_BIN':'native-fixture'})
        self.assertEqual(events,['application','images','packet','static-variants'])

    def test_imported_products_refuse_changed_packet_without_preparing_sdk_or_recompiling(self):
        self.outputs();self.config['components']['assets']=[{'name':'web','source':'public'}]
        with patch.object(game.images,'validate_release',side_effect=ValueError('changed accepted packet')),\
             patch.object(game.workload_fixture,'setup') as setup,patch.object(game.images,'package') as package,\
             patch.object(game,'build_rust') as compile:
            with self.assertRaisesRegex(ValueError,'changed accepted packet'):
                game.validate_outputs(self.root,self.config,{},compiler=False)
        setup.assert_not_called();package.assert_not_called();compile.assert_not_called()

    def test_rust_environment_cannot_replace_source_or_workflow_authority(self):
        for name in ['SOURCE_COMMIT','APP_VERSION','COOKBOOK_PREVIEW_NUMBER','MISE_DATA_DIR','GITHUB_SHA','REQUEST_HEAD']:
            self.config['components']['rust']=[{'manifest':'Cargo.toml','environment':{name:'a'*40}}]
            with self.subTest(name=name),self.assertRaisesRegex(ValueError,'authority'):
                game.cargo_records(self.config,{'APP_VERSION':'a'*40})
        self.config['components']['rust']=[{'manifest':'Cargo.toml','output_directory':'.artifacts/native',
            'environment':{'SOURCE_REVISION':'{revision}'}}]
        self.assertEqual(game.cargo_records(self.config,{'APP_VERSION':'b'*40}),
            [{'manifest':'Cargo.toml','environment':{'SOURCE_REVISION':'b'*40}}])


if __name__=='__main__':unittest.main()
