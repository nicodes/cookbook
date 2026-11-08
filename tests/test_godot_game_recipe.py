from source_fixture import SDK_ROOT, write_entrypoint, run_task, run_unit_adapter
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = SDK_ROOT
sys.path.insert(0, str(TOOLS / 'cookbook'))
spec = importlib.util.spec_from_file_location('game_generation', TOOLS / 'cookbook/generate.py')
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


class NativeGameContract(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='game recipe ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH']})
        environment.start()
        self.addCleanup(environment.stop)
        (self.root / 'app/tests').mkdir(parents=True)
        (self.root / 'app/project.godot').write_text('[application]\nconfig/name="fixture"\n')
        (self.root / 'app/tests/behavior.gd').write_text('native assertion fixture\n')
        (self.root / '.gitignore').write_text('/.artifacts/\n/app/build/\n')
        (self.root / 'cookbook.toml').write_text('''version=1
recipe="godot-game"
[ci.godot]
unit_shards=1
[components.godot]
exports=[{preset="Web",output="app/build/web/index.html"}]
[artifacts]
paths=["app/build/web"]
[[tests.unit]]
runner="godot-check"
path="app/tests/behavior.gd"
cwd="app"
timeout=2
[[tests.integration]]
runner="godot-check"
path="app/tests/behavior.gd"
cwd="app"
timeout=2
''')
        revision = subprocess.check_output(['git', '-C', str(TOOLS), 'rev-parse', 'HEAD'], text=True).strip()
        (self.root / 'mise.toml').write_text('[tools]\npython="3.13.11"\nactionlint="1.7.12"\n"github:aviorstudio/gdlint"="v0.0.3"\n"pipx:gdtoolkit"="4.5.0"\n')
        write_entrypoint(self.root)
        for args in [['init', '-q'], ['add', '.'], ['-c', 'user.name=Recipe Controls', '-c',
                     'user.email=controls@example.invalid', 'commit', '-qm', 'native fixture']]:
            subprocess.run(['git', '-C', str(self.root), *args], check=True)
        binary = self.root / '.artifacts/godot/bin/godot'
        binary.parent.mkdir(parents=True)
        binary.write_text('''#!/usr/bin/env python3
import json,sys
from pathlib import Path
root=Path(__file__).resolve().parents[3]
with (root/'.artifacts/engine-calls.jsonl').open('a') as output:output.write(json.dumps(sys.argv[1:])+'\\n')
if '--export-release' in sys.argv:
    destination=Path(sys.argv[-1]);destination.parent.mkdir(parents=True,exist_ok=True)
    for suffix in ['.html','.wasm','.pck','.js']:destination.with_suffix(suffix).write_bytes(b'native export fixture')
print('native fixture reached')
''')
        binary.chmod(0o755)

    def make(self, operation):
        if operation in {'help', 'dev'}:
            return run_task(self.root, operation)
        return run_unit_adapter(self.root, operation, environment={**os.environ,
            'MISE_TRUSTED_CONFIG_PATHS': str(self.root), 'MISE_CEILING_PATHS': str(self.root.parent),
            'MISE_OFFLINE': 'true'})

    def succeeded(self, operation):
        result = self.make(operation)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_native_units_never_export_or_implicitly_prepare_a_product_build(self):
        self.succeeded('test:unit')
        commands = [json.loads(line) for line in (self.root / '.artifacts/engine-calls.jsonl').read_text().splitlines()]
        self.assertEqual(len(commands), 1)
        self.assertIn('--script', commands[0])
        self.assertNotIn('--export-release', commands[0])
        self.assertFalse((self.root / 'app/build').exists())

    def test_integration_requires_existing_unchanged_outputs_and_never_repairs_them(self):
        self.assertNotEqual(self.make('test:integration').returncode, 0)
        self.assertFalse((self.root / 'app/build').exists())
        self.succeeded('build')
        self.succeeded('test:integration')
        output = self.root / 'app/build/web/index.wasm'
        output.write_bytes(b'mutated artifact')
        self.assertNotEqual(self.make('test:integration').returncode, 0)
        self.assertEqual(output.read_bytes(), b'mutated artifact')

    def test_missing_development_capability_is_absent_from_help(self):
        self.succeeded('help')
        self.assertNotIn('mise run dev ', self.make('help').stdout)
        self.assertNotEqual(self.make('dev').returncode, 0)

    def test_emscripten_node_is_confined_to_compiler_execution(self):
        from unittest.mock import patch
        import os
        from configuration import load
        import godot_game
        config = load(self.root)
        config['components']['rust'] = [{'manifest': 'core/Cargo.toml'}]
        (self.root / 'core').mkdir()
        (self.root / 'core/Cargo.toml').write_text('[package]\nname="native-control"\nversion="0.1.0"\n')
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        env = godot_game.environment(self.root, config)
        env['PATH'] = '/caller-pinned-node:' + os.environ['PATH']
        compiler = {**env, 'PATH': '/emscripten-bundled-node', 'LIBCLANG_PATH': '/pinned-libclang'}
        seen = []
        def build(root, components, values, run):
            seen.append(('compiler', values['PATH']))
        def validate(root, components, values, run):
            seen.append(('validation', values['PATH']))
            self.assertEqual(values['LIBCLANG_PATH'], '/pinned-libclang')
        with patch.dict(os.environ), patch.object(godot_game.godot_sdk, 'activate', return_value=compiler), \
                patch.object(godot_game.rust_component, 'build', side_effect=build), \
                patch.object(godot_game.rust_component, 'validate_build', side_effect=validate):
            godot_game.compile_outputs(self.root, config, env)
            self.assertEqual(os.environ['PATH'], env['PATH'])
        self.assertEqual(seen, [('compiler', '/emscripten-bundled-node'), ('validation', env['PATH'])])

    def test_image_application_still_validates_its_native_model(self):
        import godot_game
        from configuration import load
        config = load(self.root)
        environment = godot_game.environment(self.root, config)
        config['components'].update(images=[{'name':'api'}])
        with patch.dict(os.environ), \
                patch.object(godot_game.godot_sdk, 'activate', return_value=environment), \
                patch.object(godot_game.images, 'validate_application') as validate, \
                patch.object(godot_game.images, 'build'), patch.object(godot_game.images, 'package'), \
                patch.object(godot_game.asset_fixtures, 'package'), \
                patch.object(godot_game.image_fixtures, 'package'), patch.object(godot_game, 'validate_outputs'):
            godot_game.compile_outputs(self.root, config, environment)
        validate.assert_called_once_with(self.root, config, environment, godot_game.run)


if __name__ == '__main__':
    unittest.main()
