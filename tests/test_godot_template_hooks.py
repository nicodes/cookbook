from source_fixture import SDK_ROOT
"""Custom template setup never leaks into tests or imported product validation."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock,patch

sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import godot_game as game
import ci


class TemplateHookControls(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        app=self.root/'app';app.mkdir();(app/'project.godot').write_text('config_version=5\n')
        self.config={'components':{'godot':{'exports':[{'preset':'Web','output':'app/build/web/index.html'}],
            'templates':{'profile':'app/engine/profile.json','directory':'app/.export-templates'}}},'artifacts':{'paths':['app/build/web']}}

    def test_bad_or_absent_custom_sdk_stops_the_real_export_without_network_repair(self):
        template=SimpleNamespace(verify=MagicMock(side_effect=ValueError('offline SDK mismatch')),setup=MagicMock())
        exporter=MagicMock()
        with patch.dict(sys.modules,{'godot_template':template}),patch.object(game.godot_sdk,'activate',return_value={}),\
             patch.object(game.godot_package,'author'),patch.object(game.godot_package,'extensions'),\
             patch.object(game,'helper',return_value=exporter):
            with self.assertRaisesRegex(ValueError,'offline SDK mismatch'):
                game.compile_outputs(self.root,self.config,{})
        template.verify.assert_called_once();template.setup.assert_not_called();exporter.export.assert_not_called()

    def test_imported_host_product_validation_does_not_require_custom_sdk_cache(self):
        output=self.root/'app/build/web';output.mkdir(parents=True)
        for suffix in ['.html','.wasm','.pck','.js']:(output/('index'+suffix)).write_bytes(b'accepted product fixture')
        verification=SimpleNamespace(scope=lambda **values:values,artifacts=lambda scope:{'fixture':'accepted'})
        template=SimpleNamespace(verify=MagicMock(side_effect=AssertionError('must not inspect export SDK')),setup=MagicMock())
        with patch.dict(sys.modules,{'godot_template':template}),patch.object(game,'helper',return_value=verification),\
             patch.object(ci,'verification',return_value=verification):
            game.validate_outputs(self.root,self.config,{},compiler=False)
        template.verify.assert_not_called();template.setup.assert_not_called()

    def test_image_fixture_preparation_runs_only_in_the_build_phase(self):
        from contextlib import ExitStack
        import subprocess
        fixture = self.root / 'tests/fixtures/prepare.py'
        fixture.parent.mkdir(parents=True)
        fixture.write_text('from pathlib import Path\nPath("fixture-ready").write_text("prepared bytes")\n')
        record = {'name': 'gate-fixture', 'dockerfile': 'tests/fixtures/Dockerfile',
                  'pull': False, 'native': [{'runner': 'python', 'path': 'tests/fixtures/prepare.py'}]}
        (fixture.parent / 'Dockerfile').write_text('FROM scratch\n')
        config = {'components': {'godot': {'exports': [
            {'preset': 'Web', 'output': 'app/build/web/index.html'}]}, 'images': [record]}}
        effects = []
        def execute(argv, cwd, environment, *args, **kwargs):
            if argv[0] == sys.executable:
                subprocess.run(argv, cwd=cwd, check=True)
                effects.append('native preparation')
            elif argv[:3] == ['podman', '--remote=false', 'build']:
                self.assertEqual((self.root / 'fixture-ready').read_text(), 'prepared bytes')
                effects.append('image build')
            else:
                self.fail('unexpected build effect: ' + repr(argv))
        with ExitStack() as stack:
            for owner, name in [(game, 'build_rust'), (game, 'validate_outputs'),
                    (game.node_component, 'build'), (game.go_component, 'build'),
                    *[(game.godot_package, name) for name in ['author', 'extensions', 'runtime', 'packs', 'package']],
                    (game.images, 'validate_application'), (game.asset_fixtures, 'package'), (game.image_fixtures, 'package'), (game.images, 'package')]:
                stack.enter_context(patch.object(owner, name))
            stack.enter_context(patch.object(game.godot_sdk, 'activate', return_value={}))
            stack.enter_context(patch.object(game, 'helper', return_value=MagicMock()))
            stack.enter_context(patch.object(game.images, 'definition', return_value={
                'image_base': 'ghcr.io/fixture/app', 'inputs': {'images': ['ghcr.io/fixture/app-gate-fixture:{revision}']}}))
            stack.enter_context(patch.object(game, 'run', side_effect=execute))
            self.assertFalse((self.root / 'fixture-ready').exists())
            game.compile_outputs(self.root, config, {'APP_VERSION': 'a' * 40, 'GODOT_BIN': 'reviewed-engine'})
        self.assertEqual(effects, ['native preparation', 'image build'])


if __name__=='__main__':unittest.main()
