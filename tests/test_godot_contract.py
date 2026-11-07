from source_fixture import SDK_ROOT
"""Fixed recipe conventions keep native import authority and authored bytes."""
from contextlib import chdir
from pathlib import Path
import json
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import godot_game as game
from source_fixture import read_source, write_source, dump_source


class GodotContract(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = self.root / 'app'
        self.app.mkdir()
        (self.app / 'project.godot').write_bytes(b'config_version=5\n')
        self.config = {'recipe': 'godot-game', 'components': {'godot': {}}}
        self.sidecar = self.app / 'texture.png.import'
        self.authored = b'[remap]\nuid="authored"\n[params]\ncompress/mode=0\n'
        self.sidecar.write_bytes(self.authored)

    def test_retired_convention_fields_refuse_even_their_former_defaults(self):
        for field, value in [('project', 'app'), ('origin', 'godot'), ('import_stage', 'setup'),
                ('lint_arguments', ['-warn']), ('import_ignored_patterns', []),
                ('preserve_import_defaults', True), ('preserve_import_defaults', False)]:
            with self.subTest(field=field, value=value):
                self.config['components']['godot'] = {field: value}
                with self.assertRaises(ValueError): game.validate_config(self.root, self.config)
        self.config['components']['godot'] = {'exports': [
            {'preset': 'Web', 'output': 'app/build/web/index.html', 'project': 'app'}]}
        with self.assertRaisesRegex(ValueError, 'unsupported native Godot export'):
            game.validate_config(self.root, self.config)

    def import_with_editor(self, write):
        project = game.helper('godot-project')
        original_helper = game.helper
        calls = []
        def editor(argv, *, timeout, environment):
            calls.append((argv, timeout, environment))
            write()
            return 0
        def select(name):
            if name == 'godot-project': return project
            if name == 'godot-game-runner': return SimpleNamespace(main=editor)
            return original_helper(name)
        with patch.object(game, 'helper', side_effect=select), patch.object(project.subprocess,
                'check_output', return_value='4.7.2.stable.fixture'):
            game.import_native(self.root, self.config, {'GODOT_BIN': '/pinned/godot'})
        return calls

    def test_every_import_preserves_only_the_seven_equivalent_additions_and_native_arguments(self):
        additions = (b'compress/uastc_level=0\ncompress/rdo_quality_loss=0.0\n'
            b'process/channel_remap/red=0\nprocess/channel_remap/green=1\n'
            b'process/channel_remap/blue=2\nprocess/channel_remap/alpha=3\nmodulate_color_glyphs=false\n')
        self.config['components']['godot']['import_arguments'] = ['--frame-delay', '1000']
        calls = self.import_with_editor(lambda: self.sidecar.write_bytes(self.authored + additions))
        self.assertEqual(self.sidecar.read_bytes(), self.authored)
        self.assertEqual((self.app / 'project.godot').read_bytes(), b'config_version=5\n')
        self.assertEqual(len(calls), 1)
        argv, timeout, environment = calls[0]
        self.assertEqual(argv, ['/pinned/godot', '--headless', '--editor', '--quit', '--path', str(self.app),
            '--import', '--frame-delay', '1000'])
        self.assertEqual(timeout, 180)
        self.assertEqual(environment, {'GODOT_BIN': '/pinned/godot'})

    def test_editor_authored_setting_changes_are_still_refused(self):
        with self.assertRaisesRegex(ValueError, 'changed authored import settings'):
            self.import_with_editor(lambda: self.sidecar.write_bytes(self.authored.replace(b'mode=0', b'mode=1')))
        self.assertFalse((self.app / '.godot/cookbook-import.json').exists())

    def test_lint_always_runs_pinned_native_warnings_mode_and_preserves_failure(self):
        with patch.object(game, 'pinned_gdlint', return_value='/pinned/gdlint'), patch.object(game, 'run') as run:
            game.lint(self.root, self.config, {})
        self.assertIn((['/pinned/gdlint', '-warn'], self.app, {}), [call.args for call in run.call_args_list])
        def execute(argv, *_args):
            if argv[0] == '/pinned/gdlint': raise RuntimeError('native lint refused')
        with patch.object(game, 'pinned_gdlint', return_value='/pinned/gdlint'), patch.object(game, 'run', side_effect=execute):
            with self.assertRaisesRegex(RuntimeError, 'native lint refused'): game.lint(self.root, self.config, {})

    def test_retired_release_definition_selector_is_refused(self):
        self.config['artifacts'] = {'definition': 'deploy/release.toml'}
        with self.assertRaisesRegex(ValueError, 'unknown Godot build artifact'):
            game.verification_scope(self.root, self.config)

    def test_release_file_alone_does_not_enable_release_input_scope(self):
        path = self.root / 'deploy/release.toml'
        path.parent.mkdir()
        path.write_text('an unrelated, unselected native file')
        self.config['artifacts'] = {'paths': ['app/build/web']}
        self.assertEqual(game.verification_scope(self.root, self.config)['artifacts'], ['app/build/web'])

    def test_image_capability_requires_canonical_closed_release_inputs(self):
        self.config['components']['images'] = [{'name': 'gate'}]
        with self.assertRaises(ValueError): game.verification_scope(self.root, self.config)
        path = self.root / 'deploy/release.toml'
        path.parent.mkdir()
        definition = {'version': 1, 'project': 'fixture', 'image_base': 'ghcr.io/example/fixture',
            'inputs': {'artifacts': ['public'], 'images': ['ghcr.io/example/fixture-gate:{revision}'],
                'environment': ['NATIVE_POLICY'], 'revision_environment': ['SOURCE_COMMIT']}, 'tests': ['native']}
        write_source(path, definition)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'add', 'deploy/release.toml'], check=True)
        with chdir(self.root):
            scope = game.verification_scope(self.root, self.config)
        self.assertEqual(scope['artifacts'], ['public'])
        self.assertEqual(scope['images'], definition['inputs']['images'])
        self.assertEqual(scope['environment'], ['NATIVE_POLICY', 'SOURCE_COMMIT'])
        self.assertEqual(scope['revision_environment'], ['SOURCE_COMMIT'])


if __name__ == '__main__': unittest.main()
