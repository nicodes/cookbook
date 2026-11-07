from source_fixture import SDK_ROOT
"""Native imports wait for their declared build inputs without caller phase wiring."""
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

TOOLS = SDK_ROOT
sys.path.insert(0, str(TOOLS / 'cookbook'))
import godot_game as game


class NativeImportStage(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'app/export').mkdir(parents=True)
        (self.root / 'app/project.godot').write_text('config_version=5\n')
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        self.binary = b'\x7fELF\x02' + b'\0' * 13 + b'\x3e\0'

    def config(self, *, extensions=False, prepare=False, **settings):
        values = {'exports': [{'preset': 'Web', 'output': 'app/build/web/index.html'}], **settings}
        if extensions:
            values['extensions'] = [{'source': '.artifacts/native/libfixture.so',
                'destination': 'app/extensions/libfixture.so', 'format': 'elf'}]
        if prepare:
            program = self.root / 'app/export/prepare.py'
            program.write_text('from pathlib import Path\nroot=Path(__file__).resolve().parents[2]\n'
                + ('assert (root/"app/extensions/libfixture.so").read_bytes()[:5] == b"\\x7fELF\\x02"\n' if extensions else '')
                + '(root/"app/prepared.json").write_text("prepared")\n')
            values['prepare'] = [{'runner': 'python', 'path': 'app/export/prepare.py', 'timeout': 37}]
        return {'recipe': 'godot-game', 'components': {'godot': values}}

    def exercise(self, config):
        events = []
        environment = {'GODOT_BIN': '/prepared/godot', 'PYTHONDONTWRITEBYTECODE': '1'}
        def imported(*_args):
            events.append(('import', (self.root / 'app/extensions/libfixture.so').exists(),
                (self.root / 'app/prepared.json').exists()))
        pins = {name: 'fixture' for name in ['GODOT_VERSION', 'GODOT_EXPECTED_VERSION',
            'GODOT_BINARY_CHECKSUM', 'GODOT_TEMPLATES_CHECKSUM']}
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'COOKBOOK_STAGE': 'full'}))
            stack.enter_context(patch.object(game, 'required_tools', return_value=['python']))
            stack.enter_context(patch.object(game, 'run'))
            stack.enter_context(patch.object(game, 'godot_pins', return_value=pins))
            stack.enter_context(patch.object(game.godot_sdk, 'inputs', return_value=None))
            stack.enter_context(patch.object(game.node_component, 'install'))
            stack.enter_context(patch.object(game.playwright_sdk, 'setup'))
            stack.enter_context(patch.object(game.komizo_sdk, 'setup'))
            stack.enter_context(patch.object(game.go_component, 'setup'))
            stack.enter_context(patch.object(game.godot_static, 'setup'))
            stack.enter_context(patch.object(game, 'import_native', side_effect=imported))
            game.setup(self.root, config, environment)
        setup_events = list(events)
        def compiled(*_args):
            destination = self.root / '.artifacts/native/libfixture.so'
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(self.binary)
            events.append(('compile',))
        execute = game.run
        def preparation(command, directory, env, timeout=None):
            events.append(('prepare', timeout))
            execute(command, directory, env, timeout)
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {}, clear=False))
            stack.enter_context(patch.object(game.godot_sdk, 'activate', return_value=environment))
            stack.enter_context(patch.object(game.node_component, 'build'))
            stack.enter_context(patch.object(game, 'build_rust', side_effect=compiled))
            stack.enter_context(patch.object(game, 'run', side_effect=preparation))
            stack.enter_context(patch.object(game, 'import_native', side_effect=imported))
            stack.enter_context(patch.object(game, 'helper', return_value=SimpleNamespace(
                export=lambda *_args, **_kwargs: events.append(('export',)))))
            stack.enter_context(patch.object(game, 'validate_outputs'))
            game.compile_outputs(self.root, config, environment)
        return setup_events, events

    def test_extensions_and_preparation_exist_before_the_inferred_build_import(self):
        setup, events = self.exercise(self.config(extensions=True, prepare=True))
        self.assertEqual(setup, [])
        self.assertEqual(events, [('compile',), ('prepare', 37), ('import', True, True), ('export',)])
        self.assertEqual((self.root / 'app/extensions/libfixture.so').read_bytes(), self.binary)
        self.assertEqual((self.root / 'app/prepared.json').read_text(), 'prepared')

    def test_preparation_alone_defers_import_until_its_outputs_exist(self):
        setup, events = self.exercise(self.config(prepare=True))
        self.assertEqual(setup, [])
        self.assertEqual(events, [('compile',), ('prepare', 37), ('import', False, True), ('export',)])

    def test_extensions_alone_defers_import_until_the_compiled_bytes_are_staged(self):
        setup, events = self.exercise(self.config(extensions=True))
        self.assertEqual(setup, [])
        self.assertEqual(events, [('compile',), ('import', True, False), ('export',)])

    def test_ordinary_projects_keep_their_existing_setup_import(self):
        setup, events = self.exercise(self.config())
        self.assertEqual(setup, [('import', False, False)])
        self.assertEqual(events, [('import', False, False), ('compile',), ('export',)])

    def test_phase_override_is_refused_even_when_it_names_a_formerly_valid_stage(self):
        for phase in ['setup', 'build', None, True, 1, '', 'test', [], {}]:
            with self.subTest(phase=phase):
                with self.assertRaisesRegex(ValueError, 'unsupported Godot component'):
                    game.validate_config(self.root, self.config(import_stage=phase))
                with self.assertRaisesRegex(ValueError, 'derived'):
                    game.native_import_stage({'import_stage': phase})


if __name__ == '__main__':
    unittest.main()
