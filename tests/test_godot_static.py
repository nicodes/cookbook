from source_fixture import SDK_ROOT
"""Config packaging preserves the accepted source except one authenticated gate digest."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import godot_static as static
from source_fixture import read_source, write_source, dump_source

SOURCE='a'*40
GATE='ghcr.io/example/game-gate@sha256:'+'b'*64
COMPOSE='services:\n  game-gate:\n    image: ghcr.io/example/game-gate:${APP_VERSION:-latest}\n    user: "1000:1000"\n'


def stack(reference):return {'name':'godot-static','services':{'game-gate':{'image':reference,'user':'1000:1000','read_only':True,'cap_drop':['ALL'],'security_opt':['no-new-privileges:true']}}}


class StaticBlueprint(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        for name,body in {'deploy/komizo.toml':'compose = "compose.yml"\nhostnames = ["game.example.test"]\n','deploy/compose.yml':COMPOSE,
            'public/release-manifest.json':json.dumps({'source_commit':SOURCE,'release_id':'c'*64}),
            'mise.toml':'[tools]\npython="3.13.11"\n'}.items():
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(body)
        selected={'config':'deploy/komizo.toml','compose':'deploy/compose.yml','release_manifest':'public/release-manifest.json','record':True,'recovery':False,'production':False,'recovery_test':'shipping'}
        write_source(self.root/'deploy/delivery.toml', {'mode':'godot-static','komizo_ref':'d'*40,'project':'game','static':selected})
        write_source(self.root/'deploy/release.toml', {'project':'game','image_base':'ghcr.io/example/game'})
        self.config={'recipe':'godot-game','tests':{'integration':[{'name':'shipping','runner':'bash','path':'tests/gate.sh'}]},'delivery':{'definition':'deploy/delivery.toml'},'artifacts':{
            'release_metadata':[static.CONFIG_ARTIFACT,static.MANIFEST_ARTIFACT]}}
        self.environment={'SOURCE_COMMIT':SOURCE}
        subprocess.run(['git','init','-q',str(self.root)],check=True)
        subprocess.run(['git','add','deploy','public','mise.toml'],cwd=self.root,check=True)

    def make_blueprint(self):
        sdk=self.root/'sdk';sdk.mkdir();interpreter=self.root/'python'
        def capture(argv,*_args,**_kwargs):
            self.assertEqual(argv[:2],[str(interpreter),'-I'])
            Path(argv[-1]).write_text('game.example.test -> game-gate\n')
            return str(self.root/'deploy/compose.yml')
        def normalized(root,text,environment):
            reference=text.split('image: ',1)[1].splitlines()[0]
            return stack(reference)
        with patch.object(static,'checked_sdk',return_value=(interpreter,sdk)),patch.object(static,'capture',side_effect=capture),patch.object(static,'normalized',side_effect=normalized):
            static.stage(self.root,self.config,self.environment)
        return static.verify(self.root,self.config,self.environment)

    def test_blueprint_and_public_identity_are_closed_before_publication(self):
        blueprint=self.make_blueprint()
        self.assertEqual(blueprint['compose']['text'],COMPOSE)
        self.assertEqual(blueprint['config']['text'],(self.root/'deploy/komizo.toml').read_text())
        self.assertEqual(blueprint['production_stack'],stack('ghcr.io/example/game-gate@'+static.PLACEHOLDER))
        self.assertEqual(json.loads((self.root/static.MANIFEST_ARTIFACT).read_text())['release_id'],'c'*64)

    def test_offline_revalidation_rejects_source_and_release_metadata_changes_without_sdk_setup(self):
        self.make_blueprint()
        with patch.object(static,'setup',side_effect=AssertionError('verify may not install')),patch.object(static,'checked_sdk',side_effect=AssertionError('host does not need parser SDK')):
            static.verify(self.root,self.config,self.environment)
            (self.root/'deploy/compose.yml').write_text(COMPOSE+'# changed\n')
            with self.assertRaisesRegex(ValueError,'reviewed native source'):static.verify(self.root,self.config,self.environment)
            (self.root/'deploy/compose.yml').write_text(COMPOSE)
            (self.root/static.MANIFEST_ARTIFACT).write_text(json.dumps({'source_commit':SOURCE,'release_id':'f'*64}))
            with self.assertRaisesRegex(ValueError,'actual public export'):static.verify(self.root,self.config,self.environment)

    def test_materialization_changes_only_the_pushed_gate_digest_and_packages_only_two_data_files(self):
        blueprint=self.make_blueprint();output=self.root/'package'
        with patch.object(static,'normalized',return_value=stack(GATE)):
            static.materialize(blueprint,GATE,output,{})
        self.assertEqual({p.name for p in output.iterdir()},{'compose.yml','hostnames'})
        self.assertEqual((output/'compose.yml').read_text(),COMPOSE.replace('ghcr.io/example/game-gate:${APP_VERSION:-latest}',GATE))
        self.assertEqual((output/'hostnames').read_text(),blueprint['hostnames'])
        self.assertNotIn('RUN',static.DOCKERFILE)
        self.assertTrue(static.DOCKERFILE.startswith('FROM scratch\n'))
        with self.assertRaisesRegex(ValueError,'fresh private destination'):static.materialize(blueprint,GATE,output,{})

    def test_publication_refuses_other_images_additional_services_mounts_and_mutated_host_configuration(self):
        blueprint=self.make_blueprint()
        with self.assertRaisesRegex(ValueError,'repository family'):static.materialize(blueprint,GATE.replace('/game-gate@','/other-gate@'),self.root/'wrong',{})
        for change in ['mount','service','privilege','route']:
            value=copy.deepcopy(stack(GATE))
            if change=='mount':value['services']['game-gate']['volumes']=['/:/host']
            if change=='service':value['services']['other']={}
            if change=='privilege':value['services']['game-gate']['privileged']=True
            if change=='route':value['name']='another-project'
            with self.subTest(change=change),patch.object(static,'normalized',return_value=value),self.assertRaises(ValueError):
                static.materialize(blueprint,GATE,self.root/change,{})
        with self.assertRaisesRegex(ValueError,'exactly one'):static.bind_compose(COMPOSE+'  other:\n    image: foreign:tag\n',GATE)

    def test_metadata_is_recipe_owned_and_lifecycle_cannot_invent_recovery(self):
        config=copy.deepcopy(self.config);config['artifacts']['release_metadata']=[]
        self.assertEqual(static.declaration(self.root,config),static.declaration(self.root,self.config))
        path=self.root/'deploy/delivery.toml';value=read_source(path);value['static']['record']=False;value['static']['recovery']=True;write_source(path, value)
        with self.assertRaisesRegex(ValueError,'real release records'):static.declaration(self.root,self.config)


if __name__=='__main__':unittest.main()
