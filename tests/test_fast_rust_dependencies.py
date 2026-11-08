"""Fast native harnesses consume dependency inputs prepared only by setup."""
from source_fixture import SDK_ROOT
import ast
import copy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import rust_component


def rust_setup_block(source):
    setup = next(node for node in ast.parse(source).body
                 if isinstance(node, ast.FunctionDef) and node.name == 'setup')
    return next(node for node in setup.body if isinstance(node, ast.If)
                and any(isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and isinstance(call.func.value, ast.Name)
                        and call.func.value.id == 'rust_component'
                        and call.func.attr == 'setup' for call in ast.walk(node)))


class FastRustDependencies(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='native harness dependency inputs ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'mise.toml').write_text(
            '[tools]\nrust="1.94.0"\n[env]\nRUST_NIGHTLY="nightly-2026-03-06"\n')
        self.records = [
            {'manifest': 'app/extension/Cargo.toml', 'target': 'wasm32-unknown-emscripten',
             'toolchain_environment': 'RUST_NIGHTLY', 'build_std': True,
             'lint': {'enabled': False}},
            {'manifest': 'tests/native-check/Cargo.toml', 'binaries': ['native-check'],
             'target_directory': '.artifacts/native-target'},
        ]
        for record in self.records:
            manifest = self.root / record['manifest']
            manifest.parent.mkdir(parents=True)
            manifest.write_text('[package]\nname="native-input"\nversion="0.1.0"\n')
        self.environment = {'RUST_NIGHTLY': 'nightly-2026-03-06'}
        self.source = (SDK_ROOT / 'cookbook/godot_game.py').read_text()

    def trace(self, stage, *, source=None, components=None, fail=False):
        calls = []
        def sdk(argv, root, environment):
            calls.append((list(argv), root, dict(environment)))
            if fail and 'fetch' in argv:
                raise subprocess.CalledProcessError(23, argv)
        namespace = {'stage': stage, 'root': self.root,
                     'components': {'rust': self.records} if components is None else components,
                     'config': {}, 'env': self.environment, 'sdk': sdk,
                     'rust_component': rust_component,
                     'cargo_records': lambda config, environment: copy.deepcopy(self.records),
                     'tomllib': __import__('tomllib')}
        block = rust_setup_block(self.source if source is None else source)
        exec(compile(ast.Module(body=[block], type_ignores=[]), '<actual setup Rust block>', 'exec'), namespace)
        return calls

    def test_fast_prepares_every_locked_closure_without_compiling_any_program(self):
        calls = self.trace('fast')
        commands = [argv for argv, _, _ in calls]
        self.assertIn(['rustup', 'component', 'add', '--toolchain', '1.94.0',
                       'rustfmt', 'clippy'], commands)
        fetches = [argv for argv in commands if 'cargo' in argv]
        self.assertTrue(fetches)
        self.assertEqual({argv[argv.index('--manifest-path') + 1] for argv in fetches},
                         {record['manifest'] for record in self.records})
        self.assertTrue(all(argv[argv.index('cargo') + 1] == 'fetch' and '--locked' in argv
                            for argv in fetches))
        self.assertFalse(any(token in {'build', 'test', 'run'}
                             for argv in fetches for token in argv[argv.index('cargo') + 1:]))
        self.assertTrue(all(root == self.root and environment ==
                            self.environment | ({'RUSTUP_AUTO_INSTALL': '0'} if 'cargo' in argv else {})
                            for argv, root, environment in calls))

    def test_full_setup_commands_and_order_are_identical_to_the_previous_branch(self):
        original = self.source.replace(
            "    if components.get('rust') and stage in {'fast', 'full'}:\n"
            "        rust_component.setup(root, cargo_records(config, env), env, sdk)\n",
            "    if components.get('rust'):\n"
            "        if stage == 'full':\n"
            "            rust_component.setup(root, cargo_records(config, env), env, sdk)\n"
            "        elif stage == 'fast':\n"
            "            pins = tomllib.loads((root / 'mise.toml').read_text())['tools']\n"
            "            sdk(['rustup', 'component', 'add', '--toolchain', pins['rust'], 'rustfmt'], root, env)\n", 1)
        self.assertNotEqual(original, self.source)
        self.assertEqual(self.trace('full'), self.trace('full', source=original))
        before = [argv for argv, _, _ in self.trace('fast', source=original)]
        self.assertEqual(before, [['rustup', 'component', 'add', '--toolchain', '1.94.0', 'rustfmt']])

    def test_unrelated_stages_and_projects_without_rust_do_not_prepare_cargo(self):
        for stage in ['security', 'unit']:
            self.assertEqual(self.trace(stage), [])
        self.assertEqual(self.trace('fast', components={}), [])

    def test_dependency_preparation_failure_is_preserved(self):
        with self.assertRaises(subprocess.CalledProcessError) as result:
            self.trace('fast', fail=True)
        self.assertEqual(result.exception.returncode, 23)
        self.assertIn('fetch', result.exception.cmd)


if __name__ == '__main__':
    unittest.main()
