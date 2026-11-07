from source_fixture import SDK_ROOT
"""Workspace members select their actual owning dependency inventories explicitly."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import images
import godot_game
class GodotRustInventory(unittest.TestCase):
    def test_cargo_audit_does_not_implicitly_enable_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'Cargo.toml').write_text('[package]\nname="fixture"\nversion="0.1.0"\n')
            (root/'Cargo.lock').write_text('version=4\n')
            pins=['python','actionlint','github:aviorstudio/gdlint','pipx:gdtoolkit','rust','cargo:cargo-audit']
            (root/'mise.toml').write_text('[tools]\n'+'\n'.join(json.dumps(name)+'="1.0.0"' for name in pins)+'\n')
            config={'components':{'rust':[{'manifest':'Cargo.toml'}]}}
            for stage in ['full','local','security']:
                with self.subTest(stage=stage):
                    self.assertNotIn('trivy',godot_game.required_tools(root,config,stage))
            with patch.object(godot_game,'run') as native_run,patch.object(godot_game.images,'rust_inventory') as inventory:
                godot_game.security(root,config,{})
            inventory.assert_not_called()
            audits=[call.args[0] for call in native_run.call_args_list if call.args[0][:2]==['cargo','audit']]
            self.assertEqual(audits,[['cargo','audit','--file','Cargo.lock','--deny','warnings']])
            self.assertEqual(godot_game.evidence_paths(root,config),[])
            explicit=dict(config,security={'rust_inventory_manifests':['Cargo.toml'],'rust_inventory_sbom':True})
            for stage in ['full','local','security']:
                with self.subTest(stage=stage),self.assertRaisesRegex(ValueError,'(trivy|not pinned)'):
                    godot_game.required_tools(root,explicit,stage)
            (root/'mise.toml').write_text((root/'mise.toml').read_text()+'trivy="1.0.0"\n')
            for stage in ['full','local','security']:
                with self.subTest(stage=stage):
                    self.assertIn('trivy',godot_game.required_tools(root,explicit,stage))
            self.assertEqual(godot_game.evidence_paths(root,explicit),[
                '.artifacts/security/rust-source.json','.artifacts/security/rust-source-sbom.json'])

    def test_workspace_and_standalone_owning_locks_are_used_without_member_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ['Cargo.toml','Cargo.lock','api/Cargo.toml','internal/tool/Cargo.toml','internal/tool/Cargo.lock']:
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('version=4\n')
            config={'components':{'rust':[{'manifest':'Cargo.toml'},{'manifest':'api/Cargo.toml'},{'manifest':'internal/tool/Cargo.toml'}]},
                'security':{'rust_inventory_manifests':['Cargo.toml','internal/tool/Cargo.toml'],'rust_inventory_sbom':True}}
            self.assertEqual(images.rust_inventory_manifests(root,config),['Cargo.toml','internal/tool/Cargo.toml'])
            self.assertEqual(images.rust_inventory_manifests(root,{}),[])
            commands=[]
            with patch.object(godot_game,'run',side_effect=lambda argv,*args,**kwargs:commands.append(argv)) as native_run,patch.object(godot_game.images,'rust_inventory') as inventory:
                godot_game.security(root,config,{})
            inventory.assert_called_once_with(root,['Cargo.toml','internal/tool/Cargo.toml'],{},native_run,sbom=True)
            audits=[argv for argv in commands if argv[:2]==['cargo','audit']]
            self.assertEqual([argv[3] for argv in audits],['Cargo.lock','internal/tool/Cargo.lock'])
            self.assertFalse((root/'api/Cargo.lock').exists())
            for values in [[],['Cargo.toml','Cargo.toml'],['api/Cargo.toml'],['../Cargo.toml']]:
                with self.subTest(values=values),self.assertRaises(ValueError):
                    images.rust_inventory_manifests(root,dict(config,security={'rust_inventory_manifests':values}))
