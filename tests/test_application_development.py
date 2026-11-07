from source_fixture import SDK_ROOT, native_entry
"""Keep isolated native lifecycle dispatch valid for every application recipe."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import application_development as development
import godot_game
import recipe
import sdk_environment


class ApplicationDevelopmentControls(unittest.TestCase):
    def test_neutral_database_driver_executes_the_actual_owned_program_for_both_recipes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);program=root/'api/development/database.py';program.parent.mkdir(parents=True)
            program.write_text('import os,sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(os.environ["FIXTURE_IDENTITY"])\n')
            for name in ['react-native','godot-game']:
                output=root/(name+'.txt');calls=[]
                api=SimpleNamespace()
                api.run=lambda argv,cwd,env,timeout: subprocess.run(argv,cwd=cwd,env=env,timeout=timeout,check=True)
                api.native=lambda root,entries,env: development.native(root,entries,env,api=api)
                api.check_development_sdks=lambda *args: calls.append(args)
                config={'recipe':name,'database':{'status':{'runner':'python','path':'api/development/database.py','args':[str(output)]}}}
                with patch.object(development,'load',return_value=config),patch.object(recipe,'adapter',return_value=api),\
                     patch.object(recipe,'configure_environment',return_value={'FIXTURE_IDENTITY':'local-only'}),\
                     patch.object(Path,'cwd',return_value=root),patch.object(sys,'argv',['driver','_database','--suite','status']):
                    development.main()
                self.assertEqual(output.read_text(),'local-only')
                self.assertEqual(calls[0][-2:],('_database','status'))

    def test_godot_native_database_reset_keeps_local_ghenv_and_owned_cleanup_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'ghenv.toml').write_text('version=1\n')
            calls=[];config={'dev':{'kind':'native','project_prefix':'komizo'},'database':{'reset':{'runner':'python','path':'api/development/database.py'}}}
            with patch.object(godot_game,'pinned_ghenv',return_value='/pinned/ghenv'),patch.object(godot_game,'run',side_effect=lambda *args:calls.append(args)):
                godot_game.database(root,config,{},'db:reset')
            command=native_entry(calls[0][0])
            self.assertEqual(command[2:4],['clean','--state'])
            self.assertIn('/pinned/ghenv',command);self.assertIn('local',command)
            self.assertEqual(command[-3:],['_database','--suite','reset'])
            self.assertIn(str(Path(development.__file__).resolve()),command)
            self.assertNotIn('react_native.py',' '.join(command))

    def test_native_supervisor_preparation_and_cleanup_use_same_namespace_and_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'ghenv.toml').write_text('version=1\n')
            subprocess.run(['git','init','--quiet',str(root)],check=True)
            (root/'.gitignore').write_text('.artifacts/\n')
            subprocess.run(['git','-C',str(root),'add','.'],check=True)
            calls=[];config={'dev':{'kind':'native','project_prefix':'komizo'}}
            with patch.object(godot_game,'pinned_ghenv',return_value='/pinned/ghenv'),patch.object(godot_game,'run',side_effect=lambda *args:calls.append(args)):
                godot_game.development(root,config,{'DEV_MODE':'local'},'dev')
            command=native_entry(calls[0][0])
            prepare=json.loads(command[command.index('--prepare')+1]);cleanup=json.loads(command[command.index('--cleanup')+1])
            self.assertEqual(prepare[:3],['/pinned/ghenv','local','--'])
            self.assertEqual(prepare[-7:],sdk_environment.python_entry(SDK_ROOT/'cookbook/dispatch.py','setup'))
            self.assertEqual(cleanup[:3],['/pinned/ghenv','local','--'])
            self.assertEqual(cleanup[-1],'_stop-services')
            namespace='DEV_PROJECT='+godot_game.development_project(root,config)
            self.assertIn(namespace,cleanup);self.assertIn(namespace,command)
            self.assertEqual(command[command.index('--mode')+1],'local')
            self.assertEqual(command[-1],'_dev')
            self.assertEqual(command.count('--policy-input'),3)

    def test_generic_native_lifecycle_keeps_long_running_sessions_and_bounded_database_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);program=root/'api/development/runtime.py';program.parent.mkdir(parents=True);program.touch()
            entry={'runner':'python','path':'api/development/runtime.py'};calls=[]
            with patch.object(godot_game,'run',side_effect=lambda *args:calls.append(args)):
                godot_game.native(root,[entry],{},long_running=True)
                godot_game.native(root,[entry],{})
            self.assertIsNone(calls[0][-1]);self.assertEqual(calls[1][-1],300)
            with self.assertRaises(ValueError):godot_game.native(root,[{**entry,'path':'../foreign.py'}],{})

    def test_native_entries_cannot_relabel_source_sdk_request_or_preview_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);program=root/'api/development/runtime.py';program.parent.mkdir(parents=True);program.touch()
            entry={'runner':'python','path':'api/development/runtime.py'}
            for key in ['COOKBOOK_PREVIEW_NUMBER','REQUEST_HEAD','GITHUB_RUN_ID','MISE_DATA_DIR','APP_VERSION','SOURCE_COMMIT']:
                with patch.object(godot_game,'run') as run,self.assertRaises(ValueError):
                    godot_game.native(root,[{**entry,'environment':{key:'same'}}],{key:'same','APP_VERSION':'same'})
                run.assert_not_called()


if __name__=='__main__':unittest.main()
