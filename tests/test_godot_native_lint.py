from source_fixture import SDK_ROOT
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = SDK_ROOT
sys.path.insert(0, str(TOOLS / 'cookbook'))
import godot_game


@unittest.skipIf(os.name == 'nt', 'The conflicting POSIX native executables use shell fixtures')
class NativeLintSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='native-godot-lint-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / 'project'
        (self.root / 'app').mkdir(parents=True)
        (self.root / 'app/project.godot').write_text('[application]\nconfig/name="fixture"\n')
        self.mise = shutil.which('mise')
        self.assertIsNotNone(self.mise, 'Prepare the declared Mise SDK before running native selector controls')
        self.data = self.base / 'data'
        self.native = self.data / 'installs/github-aviorstudio-gdlint/v0.0.3/gdlint'
        self.toolkit = self.data / 'installs/pipx-gdtoolkit/4.5.0/bin/gdlint'
        self.marker = self.base / 'selected.json'
        for path in [self.native, self.toolkit]:
            path.parent.mkdir(parents=True)
        self.native.write_text('#!/bin/sh\nprintf \'native:%s:%s\\n\' "$PWD" "$*" > "$LINT_SELECTION"\n')
        self.toolkit.write_text('#!/bin/sh\nprintf wrong-toolkit > "$LINT_SELECTION"\nexit 47\n')
        self.native.chmod(0o755)
        self.toolkit.chmod(0o755)
        self.configure()
        self.env = {key: value for key, value in os.environ.items() if not key.startswith('MISE_')}
        self.env.update(MISE_DATA_DIR=str(self.data), MISE_CACHE_DIR=str(self.base/'cache'),
            MISE_CONFIG_DIR=str(self.base/'config'), MISE_TRUSTED_CONFIG_PATHS=str(self.root),
            MISE_AUTO_INSTALL='false', MISE_EXEC_AUTO_INSTALL='false', MISE_OFFLINE='true',
            LINT_SELECTION=str(self.marker), PATH=str(self.toolkit.parent)+os.pathsep+str(Path(self.mise).parent)+os.pathsep+os.environ.get('PATH', ''))

    def configure(self, native=True):
        body = '[tools]\n"pipx:gdtoolkit"="4.5.0"\n'
        if native:
            body += '"github:aviorstudio/gdlint"={version="v0.0.3",bin="gdlint",asset_pattern="unused.tar.gz"}\n'
        (self.root/'mise.toml').write_text(body)

    def test_native_lint_executes_the_installed_github_tool_despite_path_collision(self):
        wrong = subprocess.run(['gdlint', '-warn'], cwd=self.root/'app', env=self.env)
        self.assertEqual(wrong.returncode, 47)
        self.assertEqual(self.marker.read_text(), 'wrong-toolkit')
        self.marker.unlink()
        commands = []
        def execute(command, cwd, environment):
            commands.append(command)
            if Path(command[0]) == self.native:
                subprocess.run(command, cwd=cwd, env=environment, check=True)
        with patch.object(godot_game, 'run', side_effect=execute):
            godot_game.lint(self.root, {'components': {'godot': {}}}, self.env)
        self.assertEqual(self.marker.read_text(), 'native:'+str(self.root/'app')+':-warn\n')
        self.assertIn([str(self.native), '-warn'], commands)

    def test_missing_caller_pin_refuses_without_selecting_global_tool(self):
        self.configure(native=False)
        with patch.object(godot_game.subprocess, 'check_output') as lookup:
            with self.assertRaisesRegex(ValueError, 'caller-owned'):
                godot_game.pinned_gdlint(self.root, self.env)
        lookup.assert_not_called()
        self.assertFalse(self.marker.exists())

    def test_missing_native_binary_never_falls_back_to_gdtoolkit(self):
        self.native.unlink()
        with self.assertRaisesRegex(ValueError, 'regular executable'):
            godot_game.pinned_gdlint(self.root, self.env)
        self.assertFalse(self.marker.exists())

    def test_linked_native_binary_cannot_escape_into_the_other_sdk(self):
        self.native.unlink()
        self.native.symlink_to(self.toolkit)
        with self.assertRaisesRegex(ValueError, 'regular executable'):
            godot_game.pinned_gdlint(self.root, self.env)
        self.assertFalse(self.marker.exists())

    def test_native_failure_is_preserved(self):
        self.native.write_text('#!/bin/sh\nexit 9\n')
        command = [godot_game.pinned_gdlint(self.root, self.env), '-warn']
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            subprocess.run(command, cwd=self.root/'app', env=self.env, check=True)
        self.assertEqual(failure.exception.returncode, 9)
        self.assertFalse(self.marker.exists())

    def test_relative_sdk_directory_is_refused(self):
        with patch.object(godot_game.subprocess, 'check_output', return_value='relative/native\n'):
            with self.assertRaisesRegex(ValueError, 'pinned SDK directory'):
                godot_game.pinned_gdlint(self.root, self.env)


if __name__ == '__main__':
    unittest.main()
