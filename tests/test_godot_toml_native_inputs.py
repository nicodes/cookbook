from source_fixture import TOOLS_ROOT
from source_fixture import SDK_ROOT
"""Authored TOML reaches unchanged native compiler and lint interfaces."""
from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = SDK_ROOT
sys.path.insert(0, str(TOOLS / 'cookbook'))
import godot_game
from configuration import runtime_configuration


class NativeTomlInputs(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='native-toml-inputs-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = self.root / 'app'
        self.app.mkdir()
        (self.app / 'project.godot').write_text('config_version=5\n')
        (self.root / '.gitignore').write_text('.artifacts/\n/app/gdlint.json\n')
        self.source = self.app / 'gdlint.toml'
        self.runtime = runtime_configuration()
        self.data = {'errors': {'print_statements': False}, 'settings': {'ignore_patterns': []}}
        self.runtime.source_config.write(self.source, self.data)
        subprocess.run(['git', 'init', '--quiet', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)

    def lint(self, fail=False):
        commands = []
        def execute(command, cwd, environment):
            if command[0] != '/pinned/gdlint':
                return
            commands.append(command)
            self.assertEqual(cwd, self.app)
            target = self.app / 'gdlint.json'
            self.assertEqual(json.loads(target.read_bytes()), self.data)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            if fail:
                raise subprocess.CalledProcessError(9, command)
        with patch.object(godot_game, 'pinned_gdlint', return_value='/pinned/gdlint'), patch.object(godot_game, 'run', side_effect=execute):
            godot_game.lint(self.root, {'components': {'godot': {}}}, {})
        return commands

    def test_lint_uses_original_argv_and_authenticated_adjacent_configuration(self):
        self.assertEqual(self.lint(), [['/pinned/gdlint', '-warn']])
        self.assertFalse((self.app / 'gdlint.json').exists())
        receipt = self.runtime.declaration(self.root, 'app/gdlint.toml', 'gdlint')[3]
        self.assertFalse(receipt.exists())
        self.assertEqual(self.runtime.source_config.read(self.source), self.data)

    def test_native_failure_status_and_cleanup_are_preserved(self):
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            self.lint(fail=True)
        self.assertEqual(failure.exception.returncode, 9)
        self.assertFalse((self.app / 'gdlint.json').exists())
        self.assertFalse(self.runtime.declaration(self.root, 'app/gdlint.toml', 'gdlint')[3].exists())

    def test_unignored_foreign_and_tracked_lint_outputs_refuse(self):
        target = self.app / 'gdlint.json'
        (self.root / '.gitignore').write_text('.artifacts/\n')
        with self.assertRaisesRegex(ValueError, 'ignored'):
            self.lint()
        (self.root / '.gitignore').write_text('.artifacts/\n/app/gdlint.json\n')
        target.write_text('{"foreign":true}')
        with self.assertRaisesRegex(ValueError, 'absent'):
            self.lint()
        self.assertEqual(target.read_text(), '{"foreign":true}')
        subprocess.run(['git', '-C', str(self.root), 'add', '--force', 'app/gdlint.json'], check=True)
        with self.assertRaisesRegex(ValueError, 'tracked'):
            self.lint()
        self.assertEqual(target.read_text(), '{"foreign":true}')

    def compile_art(self, text, *, program='export const sprite = {color: "green"};\n'):
        bun = shutil.which('bun')
        self.assertIsNotNone(bun, 'Prepare the pinned Bun SDK before native art compiler controls')
        (self.app / 'sprite.ts').write_text(program)
        declaration = self.app / 'project.toml'
        declaration.write_text(text)
        return subprocess.run([bun, '--no-macros', str(TOOLS_ROOT / 'helpers/godot-art-sources.mjs'), str(declaration)],
                              cwd=self.root, capture_output=True, text=True, timeout=30)

    def test_actual_bun_compiles_declared_toml_sources_without_rendering(self):
        result = self.compile_art('schema = 1\njobs = [{sources = ["sprite.ts"]}]\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'Compiled 1 declared art source entrypoints')
        self.assertEqual(sorted(path.name for path in self.app.iterdir()),
                         ['gdlint.toml', 'project.godot', 'project.toml', 'sprite.ts'])

    def test_actual_bun_refuses_invalid_toml_compiler_errors_and_escaping_paths(self):
        for text, program, expected in [
            ('{"jobs":[{"sources":["sprite.ts"]}]}', 'export const x = 1;', 'source'),
            ('jobs = [{sources = ["sprite.ts"]}]\nwhen = 2026-11-07\n', 'export const x = 1;', 'source'),
            ('jobs = [{sources = ["sprite.ts"]}]\n', 'export const = ;', 'error'),
            ('jobs = [{sources = ["../outside.ts"]}]\n', 'export const x = 1;', 'owned source'),
        ]:
            (self.root / 'outside.ts').write_text('export const outside = true;\n')
            with self.subTest(declaration=text):
                result = self.compile_art(text, program=program)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr.lower())


if __name__ == '__main__':
    unittest.main()
