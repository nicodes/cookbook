from source_fixture import TOOLS_ROOT
from source_fixture import SDK_ROOT
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = SDK_ROOT
spec = importlib.util.spec_from_file_location('godot_variant_export', TOOLS_ROOT/'helpers/godot-export.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
sys.path.insert(0,str(TOOLS/'cookbook'))
import godot_game as game


class ExportVariants(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root/'app'
        self.project.mkdir()
        (self.project/'project.godot').write_text('config_version=5\n')
        for name in ['test_support','local_auth']:
            (self.project/name).mkdir()
            (self.project/name/'owned.gd').write_text('# native source\n')
        self.presets = 'exclude_filter="test_support/*, local_auth/*, editor/*, tests/*"\n'
        (self.project/'export_presets.cfg').write_text(self.presets)
        (self.project/'client-config.json').write_text('{"mode":"authored"}\n')
        (self.root/'.artifacts/config').mkdir(parents=True)
        self.config = self.root/'.artifacts/config/debug.json'
        self.config.write_text('{"mode":"debug"}\n')

    def test_debug_configuration_and_privileged_filters_change_only_the_copy(self):
        observed = []
        def run(command):
            copy = Path(command[command.index('--path')+1])
            observed.append((json.loads((copy/'client-config.json').read_text()),(copy/'export_presets.cfg').read_text()))
        helper.export(self.project,[('Web',self.root/'output/index.html','debug')],run=run,
            privileged_directories=['test_support','local_auth'],source_files={'client-config.json':str(self.config)},source_root=self.root)
        self.assertTrue(observed)
        self.assertTrue(all(value=={'mode':'debug'} and filters=='exclude_filter="editor/*, tests/*"\n' for value,filters in observed))
        self.assertEqual((self.project/'export_presets.cfg').read_text(),self.presets)
        self.assertEqual(json.loads((self.project/'client-config.json').read_text()),{'mode':'authored'})

    def test_a_subsequent_release_retains_original_filters_after_debug_or_failure(self):
        def fail(command):
            raise RuntimeError('failed native export')
        with self.assertRaises(RuntimeError):
            helper.export(self.project,[('Web',self.root/'debug/index.html','debug')],run=fail,
                privileged_directories=['test_support','local_auth'])
        filters=[]
        def observe(command):
            copy=Path(command[command.index('--path')+1])
            filters.append((copy/'export_presets.cfg').read_text())
        helper.export(self.project,[('Web',self.root/'release/index.html','release')],run=observe)
        self.assertEqual(filters,[self.presets,self.presets])

    def test_privileged_release_mixed_modes_missing_exclusions_and_linked_sources_refuse(self):
        for presets in [[('Web',self.root/'out','release')],
                        [('Web',self.root/'debug','debug'),('Linux',self.root/'release','release')]]:
            calls=[]
            with self.subTest(presets=presets),self.assertRaises(ValueError):
                helper.export(self.project,presets,run=calls.append,privileged_directories=['test_support'])
            self.assertEqual(calls,[])
        (self.project/'export_presets.cfg').write_text('exclude_filter="editor/*"\n')
        with self.assertRaisesRegex(ValueError,'explicitly exclude'):
            helper.export(self.project,[('Web',self.root/'debug','debug')],run=lambda _:None,privileged_directories=['test_support'])
        (self.project/'test_support'/'linked').symlink_to(self.config)
        with self.assertRaisesRegex(ValueError,'real owned'):
            helper.export(self.project,[('Web',self.root/'debug','debug')],run=lambda _:None,privileged_directories=['test_support'])

    def test_only_bounded_regular_owned_json_can_be_an_export_configuration(self):
        for source in ['../outside.json','tests/policy.json','app/code.gd']:
            with self.subTest(source=source),self.assertRaises(ValueError):
                helper.variant_options(self.project,['release'],source_files={'client-config.json':source},source_root=self.root)
        for body in ['not JSON','{"value":NaN}', '"'+'x'*65536+'"']:
            self.config.write_text(body)
            with self.subTest(size=len(body)),self.assertRaises(ValueError):
                helper.variant_options(self.project,['release'],source_files={'client-config.json':self.config},source_root=self.root)
        self.config.unlink()
        self.config.symlink_to(self.project/'client-config.json')
        with self.assertRaisesRegex(ValueError,'regular owned'):
            helper.variant_options(self.project,['release'],source_files={'client-config.json':self.config},source_root=self.root)

    def test_configuration_cannot_replace_native_code_or_traverse_links(self):
        for destination in ['project.godot','export_presets.cfg','scripts/commands.json','../outside.json','.git/config.json']:
            with self.subTest(destination=destination),self.assertRaises(ValueError):
                helper.variant_options(self.project,['debug'],source_files={destination:self.config},source_root=self.root)
        (self.project/'config').symlink_to(self.root/'.artifacts/config',target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'destinations'):
            helper.variant_options(self.project,['debug'],source_files={'config/runtime.json':self.config},source_root=self.root)

    def test_recipe_separates_exports_with_different_privilege_and_configuration(self):
        config={'components':{'godot':{'exports':[
            {'preset':'Web','output':'app/build/release/index.html','kind':'release'},
            {'preset':'Web','output':'app/build/debug/index.html','kind':'debug',
                'privileged_directories':['test_support','local_auth'],
                'source_files':{'client-config.json':'.artifacts/config/debug.json'}}]}}}
        exports=[]
        wrapper=type('Export',(),{'export':lambda _self,*args,**kwargs:exports.append((args,kwargs))})()
        with patch.object(game.godot_sdk,'activate',return_value={}),patch.object(game,'build_rust'), \
                patch.object(game.go_component,'build'),patch.object(game.godot_package,'extensions'), \
                patch.object(game.godot_package,'author'),patch.object(game.godot_package,'runtime'), \
                patch.object(game.godot_package,'packs'),patch.object(game.godot_package,'package'), \
                patch.object(game,'helper',return_value=wrapper),patch.object(game,'validate_outputs'):
            game.compile_outputs(self.root,config,{'GODOT_BIN':'fixture'})
        self.assertEqual(len(exports),2)
        self.assertEqual(exports[0][1]['privileged_directories'],())
        self.assertEqual(exports[0][1]['source_files'],{})
        self.assertEqual(exports[1][1]['privileged_directories'],('test_support','local_auth'))
        self.assertEqual(exports[1][0][1][0][2],'debug')

    def test_native_operational_evidence_is_not_a_build_input(self):
        config={'artifacts':{'paths':['app/build/release'], 'evidence_paths':['production-evidence/']}}
        self.assertEqual(game.verification_scope(self.root,config)['artifacts'],['app/build/release'])


if __name__=='__main__':
    unittest.main()
