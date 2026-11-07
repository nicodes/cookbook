from source_fixture import SDK_ROOT
"""Node regressions compile only after the declared application's autoloads exist."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import godot_game as game


class DeferredAutoloadControls(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        (self.root/'app/tests').mkdir(parents=True);(self.root/'app/project.godot').write_text('config_version=5\n')
        (self.root/'app/tests/node.gd').write_text('extends Node\n')
        self.config={'components':{'godot':{'forbidden_diagnostics':['native guard']}}}
        self.entry={'runner':'godot-check','path':'app/tests/node.gd','timeout':180,'policy':{'deferred_autoloads':True}}

    def test_real_owned_script_remains_the_deferred_argument_and_preserves_deadline_and_failures(self):
        runner=Mock();runner.run_one.return_value={'exit_code':0}
        environment={'GODOT_BIN':'reviewed-engine'}
        with patch.object(game,'helper',return_value=runner):game.native_godot(self.root,self.config,self.entry,environment)
        argv=runner.run_one.call_args.args[0]
        self.assertEqual(argv,['reviewed-engine','--headless','--path',str(self.root/'app'),'--script',
            str(game.TOOLS/'helpers/godot-test-entry.gd'),'--','res://tests/node.gd'])
        self.assertEqual(runner.run_one.call_args.kwargs['timeout'],180)
        self.assertEqual(runner.run_one.call_args.kwargs['forbidden'],['native guard'])
        runner.run_one.return_value={'exit_code':1}
        with patch.object(game,'helper',return_value=runner),self.assertRaisesRegex(ValueError,'app/tests/node.gd'):
            game.native_godot(self.root,self.config,self.entry,environment)

    def test_invalid_deferred_contract_never_invokes_engine(self):
        runner=Mock()
        outside=self.root/'tests';outside.mkdir();(outside/'outside.gd').write_text('extends Node\n')
        for change in [{'path':'tests/outside.gd'},{'args':['unreviewed']},{'policy':{'deferred_autoloads':'yes'}}]:
            with self.subTest(change=change),patch.object(game,'helper',return_value=runner),self.assertRaises(ValueError):
                game.native_godot(self.root,self.config,self.entry|change,{'GODOT_BIN':'reviewed-engine'})
        runner.run_one.assert_not_called()


if __name__=='__main__':unittest.main()
