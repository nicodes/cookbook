from source_fixture import SDK_ROOT
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

COOKBOOK = SDK_ROOT / 'cookbook'
sys.path.insert(0, str(COOKBOOK))
spec = importlib.util.spec_from_file_location('native_node_component', COOKBOOK / 'node_component.py')
node = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node)


class NodeComponents(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='node package ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = self.root / 'app'
        self.app.mkdir()
        (self.app / 'package.json').write_text(json.dumps({'scripts': {
            'build': 'native-compiler', 'typecheck': 'native-typecheck',
            'test': 'native-tests', 'lint': 'native-source-checks'}}))
        self.package = {'directory': 'app', 'manager': 'npm', 'build': 'build',
                        'typecheck': 'typecheck', 'test': 'test', 'lint': 'lint'}
        self.calls = []

    def capture(self, arguments, cwd, environment):
        self.calls.append((arguments, cwd, environment))

    def test_setup_only_installs_locked_dependencies_in_the_native_package(self):
        node.install(self.root, [self.package], {}, self.capture)
        self.assertEqual(self.calls, [(['npm', 'ci', '--no-audit', '--no-fund'], self.app, {})])
        self.calls.clear()
        node.install(self.root, [self.package | {'manager': 'bun'}], {}, self.capture)
        self.assertEqual(self.calls[0][0], ['bun', 'install', '--frozen-lockfile'])

    def test_source_checks_and_tests_do_not_build_or_install(self):
        node.lint(self.root, [self.package], {}, self.capture)
        node.test(self.root, [self.package], {}, self.capture)
        self.assertEqual([args for args, _, _ in self.calls], [
            ['npm', 'run', 'typecheck'], ['npm', 'run', 'lint'], ['npm', 'run', 'test']])

    def test_expo_checks_the_installed_sdk_table_without_disabling_validation_or_other_package_checks(self):
        environment = {'EXPO_OFFLINE': '0', 'EXPO_NO_DEPENDENCY_VALIDATION': '1', 'NATIVE_INPUT': 'preserved'}
        for manager, prefix in [('npm', ['npm', 'exec', '--no', '--']), ('bun', ['bun', 'x', '--no-install'])]:
            self.calls.clear()
            node.lint(self.root, [self.package | {'manager': manager, 'expo': True}], environment, self.capture)
            command, directory, selected = self.calls[0]
            self.assertEqual(command, [*prefix, 'expo', 'install', '--check'])
            self.assertEqual(directory, self.app)
            self.assertEqual(selected, {'EXPO_OFFLINE': '1', 'NATIVE_INPUT': 'preserved'})
            self.assertNotIn('EXPO_NO_DEPENDENCY_VALIDATION', selected)
            self.assertEqual([call[0] for call in self.calls[1:]], [[manager, 'run', 'typecheck'], [manager, 'run', 'lint']])
            self.assertTrue(all(call[2] is environment for call in self.calls[1:]))
            self.assertEqual(environment, {'EXPO_OFFLINE': '0', 'EXPO_NO_DEPENDENCY_VALIDATION': '1', 'NATIVE_INPUT': 'preserved'})

    def test_expo_installed_sdk_mismatch_stops_source_acceptance(self):
        for manager in ['npm', 'bun']:
            calls = []
            def incompatible(command, directory, environment):
                calls.append(command)
                self.assertEqual(environment.get('EXPO_OFFLINE'), '1')
                self.assertNotIn('EXPO_NO_DEPENDENCY_VALIDATION', environment)
                raise subprocess.CalledProcessError(1, command, stderr='Incompatible installed SDK dependency')
            with self.subTest(manager=manager), self.assertRaises(subprocess.CalledProcessError):
                node.lint(self.root, [self.package | {'manager': manager, 'expo': True}], {}, incompatible)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][-3:], ['expo', 'install', '--check'])

    def test_native_scripts_cannot_be_missing_empty_or_command_strings(self):
        for script in ['missing', 'test; echo unexpected', '']:
            with self.subTest(script=script), self.assertRaises(ValueError):
                node.validate(self.package | {'test': script}, self.root)
        (self.app / 'package.json').write_text('{"scripts":{"test":""}}')
        with self.assertRaises(ValueError):
            node.validate({'directory': 'app', 'test': 'test'}, self.root)

    def test_native_package_arguments_are_passed_without_shell_interpolation(self):
        node.test(self.root, [self.package | {'test_arguments': ['literal;value']}], {}, self.capture)
        self.assertEqual(self.calls[0][0], ['npm', 'run', 'test', '--', 'literal;value'])

    def test_npm_and_bun_audits_preserve_their_native_policy_and_failures(self):
        node.audit(self.root, [self.package], {}, self.capture)
        self.assertEqual(self.calls[0][0], ['npm', 'audit', '--audit-level=high'])
        self.calls.clear()
        node.audit(self.root, [self.package | {'manager': 'bun'}], {}, self.capture)
        from source_fixture import native_entry
        self.assertEqual(native_entry(self.calls[0][0])[1], str(node.TOOLS/'helpers/audit.py'))
        def fail(arguments, cwd, environment):
            raise subprocess.CalledProcessError(1, arguments)
        with self.assertRaises(subprocess.CalledProcessError):
            node.audit(self.root, [self.package], {}, fail)

    def test_explicit_install_script_policy_is_not_changed_by_the_manager(self):
        for manager in ['npm', 'bun']:
            self.calls.clear()
            node.install(self.root, [self.package | {'manager': manager, 'ignore_scripts': True}], {}, self.capture)
            self.assertEqual(self.calls[0][0][-1], '--ignore-scripts')
        for value in ['true', 1]:
            with self.assertRaises(ValueError):
                node.validate(self.package | {'ignore_scripts': value}, self.root)

    def test_formatting_selects_tracked_source_and_excludes_generated_outputs(self):
        (self.app / 'src').mkdir()
        (self.app / 'src/source.ts').write_text('const value=1\n')
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'add', 'app/src'], check=True)
        (self.app / 'src/generated.ts').write_text('generated artifact\n')
        node.format(self.root, [self.package | {'format_paths': ['src']}], {}, self.capture)
        self.assertEqual(self.calls[0][0], ['prettier', '--write', str(self.app / 'src/source.ts')])


if __name__ == '__main__':
    unittest.main()
