from source_fixture import SDK_ROOT
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('configuration', SDK_ROOT / 'cookbook/configuration.py')
configuration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configuration)


class RecipeConfiguration(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'tests').mkdir()
        (self.root / 'tests/test_behavior.py').write_text('assert 1 + 1 == 2\n')

    def write(self, text):
        (self.root / 'cookbook.toml').write_text('version=1\nrecipe="godot-addon"\n' + text)

    def test_native_test_programs_are_data_without_a_second_task_graph(self):
        self.write('[[tests.unit]]\nrunner="python"\npath="tests/test_behavior.py"\nargs=["--case", "literal;value"]\n')
        self.assertEqual(configuration.load(self.root)['tests']['unit'][0]['args'], ['--case', 'literal;value'])

    def test_deferred_autoloads_are_explicitly_limited_to_game_native_checks(self):
        (self.root/'tests/node.gd').write_text('extends Node\n')
        for recipe, runner, boolean, accepted in [('godot-game','godot-check','true',True),
                ('godot-addon','godot-check','true',False),('godot-game','python','true',False),
                ('godot-game','godot-check','"yes"',False)]:
            (self.root/'cookbook.toml').write_text('version=1\nrecipe="'+recipe+'"\n[[tests.unit]]\n'
                +'runner="'+runner+'"\npath="tests/node.gd"\npolicy={deferred_autoloads='+boolean+'}\n')
            with self.subTest(recipe=recipe,runner=runner,boolean=boolean):
                if accepted:configuration.load(self.root)
                else:
                    with self.assertRaises(ValueError):configuration.load(self.root)

    def test_declared_empty_or_unknown_suites_are_refused(self):
        for text in ['[tests]\nunit=[]\n', '[tests]\nunknown=[]\n', '[tasks]\nbuild="anything"\n']:
            with self.subTest(text=text):
                self.write(text)
                with self.assertRaises(ValueError):
                    configuration.load(self.root)

    def test_product_program_paths_must_be_owned_test_sources(self):
        (self.root / 'runtime.py').write_text('pass\n')
        for name in ['../outside.py', '/outside.py', '.git/config', 'runtime.py', 'tests/missing.py']:
            self.write('[[tests.unit]]\nrunner="python"\npath="' + name + '"\n')
            with self.subTest(name=name), self.assertRaises(ValueError):
                configuration.load(self.root)

    def test_native_runners_cannot_be_arbitrary_shell_commands(self):
        self.write('[[tests.unit]]\nrunner="python; echo leak"\npath="tests/test_behavior.py"\n')
        with self.assertRaises(ValueError):
            configuration.load(self.root)

    def test_colocated_javascript_tests_keep_native_ownership_and_literal_test_identity(self):
        (self.root/'app/native').mkdir(parents=True)
        for runner,name,accepted in [('bun-test','authoring.test.ts',True),
                ('node-test','runtime.spec.mjs',True),('node-test','runtime.mjs',False),
                ('bun','authoring.test.ts',False),('python','policy.test.py',False)]:
            source=self.root/'app/native'/name
            source.write_text('// real native test source\n')
            self.write('[[tests.unit]]\nrunner="'+runner+'"\npath="app/native/'+name+'"\n')
            with self.subTest(runner=runner,name=name):
                if accepted:
                    self.assertEqual(configuration.load(self.root)['tests']['unit'][0]['path'],'app/native/'+name)
                else:
                    with self.assertRaises(ValueError):configuration.load(self.root)
        self.write('[[tests.unit]]\nrunner="node-test"\npath="app/native"\n')
        with self.assertRaises(ValueError):configuration.load(self.root)

    def test_source_and_output_paths_reject_symlink_escapes(self):
        try:
            (self.root / 'linked').symlink_to(self.root.parent, target_is_directory=True)
        except OSError:
            self.skipTest('symlink creation unavailable')
        with self.assertRaises(ValueError):
            configuration.relative(self.root, 'linked/output.zip')

    def test_godot_pins_have_one_sdk_authority_and_exact_download_checksums(self):
        fields = {'GODOT_VERSION': '4.7.2', 'GODOT_EXPECTED_VERSION': '4.7.2.stable.official.fixture',
                  'GODOT_BINARY_CHECKSUM': 'sha256:' + 'a' * 64,
                  'GODOT_TEMPLATES_CHECKSUM': 'sha256:' + 'b' * 64}
        def save():
            (self.root / 'mise.toml').write_text('[env]\n' + '\n'.join(name + '="' + value + '"' for name, value in fields.items()))
        save()
        self.assertEqual(configuration.godot_pins(self.root), fields)
        fields['GODOT_BINARY_CHECKSUM'] = 'latest'
        save()
        with self.assertRaises(ValueError):
            configuration.godot_pins(self.root)

    def test_reviewed_sha512_godot_pins_retain_their_exact_identity(self):
        (self.root / 'mise.toml').write_text('[env]\nGODOT_VERSION="4.7.2"\nGODOT_EXPECTED_VERSION="4.7.2.stable.fixture"\n'
            + 'GODOT_BINARY_CHECKSUM="sha512:' + 'a' * 128 + '"\nGODOT_TEMPLATES_CHECKSUM="sha512:' + 'b' * 128 + '"\n')
        self.assertEqual(configuration.godot_pins(self.root)['GODOT_BINARY_CHECKSUM'], 'sha512:' + 'a' * 128)

    def test_native_working_directory_cannot_escape_the_owning_checkout(self):
        self.write('[[tests.unit]]\nrunner="python"\npath="tests/test_behavior.py"\ncwd="../outside"\n')
        with self.assertRaises(ValueError):
            configuration.load(self.root)

    def test_paths_cannot_inject_multiline_artifact_inputs_or_windows_stream_names(self):
        for path in ['tests/first\nsecond', 'tests/file\rname', 'tests/file:stream', 'tests/null\0name']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                configuration.relative(self.root, path)

    def test_preserved_native_suite_deadlines_remain_explicit_and_bounded(self):
        for timeout, accepted in [(5400, True), (10800, True), (10801, False), (0, False), (True, False)]:
            value = str(timeout).lower() if type(timeout) is bool else str(timeout)
            self.write('[[tests.e2e]]\nrunner="python"\npath="tests/test_behavior.py"\ntimeout=' + value + '\n')
            with self.subTest(timeout=timeout):
                if accepted:
                    self.assertEqual(configuration.load(self.root)['tests']['e2e'][0]['timeout'], timeout)
                else:
                    with self.assertRaises(ValueError):
                        configuration.load(self.root)


if __name__ == '__main__':
    unittest.main()
