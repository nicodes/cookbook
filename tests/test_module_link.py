from source_fixture import SDK_ROOT
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import node_component


class NativeModuleLink(unittest.TestCase):
    def test_real_linker_resolves_owned_imports_and_refuses_missing_import_without_executing_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'tests').mkdir()
            main = root / 'tests/main.mjs'
            main.write_text("import {value} from './fixture.mjs'; throw Error('must not execute');\n")
            fixture = root / 'tests/fixture.mjs'
            fixture.write_text('export const value=42;\n')
            subprocess.run(['git', 'add', '.'], cwd=root, check=True)
            outputs = []
            def execute(command, cwd, environment):
                outputs.append(Path(command[command.index('--outdir')+1]))
                subprocess.run(command, cwd=cwd, env=environment, check=True, capture_output=True, timeout=15)
            config = {'components': {'module_link': ['tests']}}
            node_component.link(root, config, dict(os.environ), execute)
            self.assertFalse(outputs[0].exists())
            main.write_text("import './missing.mjs';\n")
            with self.assertRaises(subprocess.CalledProcessError):
                node_component.link(root, config, dict(os.environ), execute)
            self.assertFalse(outputs[1].exists())
