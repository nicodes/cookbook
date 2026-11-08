"""Carry the lower pure Rust context through explicit setup, never execution."""
from source_fixture import SDK_ROOT
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import dependencies
import provider
import sdk_environment
import sdk_install

PIN = dependencies.helper('pinned-tools').PIN


class ExplicitInstallContext(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='explicit SDK source ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cargo = str(self.root / 'prepared-cargo')
        # This is an explicit lower-API fixture, not a replacement source SDK.
        # The lower provider owns real default selection and native setup proof.
        self.context = SimpleNamespace(needs_rust=Mock(return_value=True),
            environment=Mock(side_effect=lambda values: dict(values, CARGO_HOME=values.get('CARGO_HOME', self.cargo))))

    def test_selected_setup_delegates_to_one_lower_pure_context_without_mutating_input(self):
        original = {'HOME': str(self.root), 'RUSTUP_HOME': 'caller-owned', 'MISE_CUSTOM': 'same', 'GITHUB_SHA': 'same'}
        with patch.object(dependencies, 'helper', return_value=self.context) as helper:
            result = sdk_environment.install_environment(self.root, original, 'python rust')
        helper.assert_called_once_with('rust_install_context')
        self.context.needs_rust.assert_called_once_with(self.root, 'python rust')
        self.assertEqual(result, dict(original, CARGO_HOME=self.cargo))
        self.assertNotIn('CARGO_HOME', original)

    def test_explicit_installer_hook_retains_context_for_following_native_setup_commands(self):
        original = {'HOME': str(self.root), 'RUSTUP_HOME': 'same', 'COOKBOOK_REF': 'same'}
        command = ['mise', 'install', 'python', 'rust']
        with patch.object(dependencies, 'helper', return_value=self.context) as helper:
            result = sdk_environment.command_environment(command, self.root, original)
        self.assertEqual(command, ['mise', 'install', 'python', 'rust'])
        self.assertEqual(original['CARGO_HOME'], self.cargo)
        self.assertEqual(result['CARGO_HOME'], self.cargo)
        self.assertEqual(result['RUSTUP_HOME'], 'same')
        self.assertEqual(result['COOKBOOK_REF'], 'same')
        self.assertEqual(result['MISE_TASK_RUN_AUTO_INSTALL'], 'false')
        helper.assert_called_once_with('rust_install_context')

    def test_nonRust_installer_does_not_prepare_a_Rust_context(self):
        self.context.needs_rust.return_value = False
        original = {'HOME': str(self.root), 'NATIVE_FLAG': 'same'}
        with patch.object(dependencies, 'helper', return_value=self.context):
            result = sdk_environment.command_environment(['mise', 'install', 'python', 'bun'], self.root, original)
        self.context.environment.assert_not_called()
        self.assertNotIn('CARGO_HOME', original)
        self.assertNotIn('CARGO_HOME', result)
        self.assertEqual(result['NATIVE_FLAG'], 'same')

    def test_native_activation_never_prepares_or_installs_a_context(self):
        original = {'COOKBOOK_CALLER_ROOT': str(self.root), 'GITHUB_SHA': 'same'}
        pinned = SimpleNamespace(activation=Mock(return_value=dict(original, PATH='existing-owned-proxies')))
        with patch.object(dependencies, 'helper', return_value=pinned) as helper:
            result = sdk_environment.command_environment(['cargo', '--version'], self.root, original)
        helper.assert_called_once_with('pinned-tools')
        pinned.activation.assert_called_once_with(self.root, [('rust', 'cargo')], original)
        self.assertEqual(result['PATH'], 'existing-owned-proxies')
        self.assertNotIn('CARGO_HOME', original)

    def invoke_ci_install(self, *, fail=False):
        events = []
        native_python = self.root / ('python3.exe' if os.name == 'nt' else 'python3')
        native_python.write_bytes(b'finite metadata-only Python path')
        action_env = self.root / 'actions-env'
        action_path = self.root / 'actions-path'
        env = {'HOME': str(self.root), 'RUSTUP_HOME': 'caller-owned',
               'GITHUB_ENV': str(action_env), 'GITHUB_PATH': str(action_path), 'GITHUB_SHA': 'same'}
        pinned = SimpleNamespace(PIN=PIN, pins=Mock(return_value={'python': '3.13.11', 'rust': '1.94.0'}))
        def activate(root, required, values):
            events.append('activate')
            self.assertEqual(values['CARGO_HOME'], self.cargo)
            self.assertEqual(required, [('python', 'python3')])
            return dict(values, PATH=str(self.root))
        pinned.activation = activate
        def helper(name):
            if name == 'pinned-tools': return pinned
            if name == 'rust_install_context': return self.context
            if name == 'select-tools': return SimpleNamespace(select=lambda *_: ['python', 'rust'])
            raise AssertionError('unexpected source helper: ' + name)
        def install(argv, **options):
            events.append('install')
            self.assertEqual(argv, ['mise', 'install', 'python@3.13.11', 'rust@1.94.0'])
            self.assertEqual(options['cwd'], self.root)
            self.assertTrue(options['check'])
            self.assertEqual(options['env']['CARGO_HOME'], self.cargo)
            self.assertEqual(options['env']['RUSTUP_HOME'], 'caller-owned')
            self.assertEqual(options['env']['GITHUB_SHA'], 'same')
            if fail: raise subprocess.CalledProcessError(23, argv)
        def version(argv, **options):
            events.append('version')
            self.assertEqual(argv[:4], [str(native_python), '-I', '-B', '-c'])
            self.assertEqual(options['env']['CARGO_HOME'], self.cargo)
            return '3.13.11\n'
        with patch.dict(os.environ, env, clear=True), \
             patch.object(dependencies, 'helper', side_effect=helper), \
             patch.object(sdk_install.effective_tasks, 'check'), \
             patch.object(sdk_install, 'subprocess', SimpleNamespace(run=install, check_output=version)), \
             patch.object(sdk_install.shutil, 'which', return_value=str(native_python)):
            if fail:
                with self.assertRaises(subprocess.CalledProcessError) as error:
                    sdk_install.install(self.root, 'python rust', env)
                self.assertEqual(error.exception.returncode, 23)
                self.assertEqual(events, ['install'])
                self.assertFalse(action_env.exists())
                self.assertFalse(action_path.exists())
                return
            result = sdk_install.install(self.root, 'python rust', env)
        self.assertEqual(events, ['install', 'activate', 'version'])
        self.assertEqual(result['CARGO_HOME'], self.cargo)
        self.assertEqual(action_env.read_text(), 'CARGO_HOME=' + self.cargo + '\nCOOKBOOK_BOOTSTRAP_PYTHON=' + str(native_python) + '\n')
        self.assertEqual(action_path.read_text(), str(native_python.parent) + '\n')
        self.assertNotIn('CARGO_HOME', env)

    def test_ci_installer_exports_the_exact_prepared_context_to_parent_and_later_steps(self):
        self.invoke_ci_install()

    def test_failed_explicit_install_preserves_native_status_without_publishing_context(self):
        self.invoke_ci_install(fail=True)

    def test_publisher_refuses_environment_framing_and_has_no_nonActions_side_effect(self):
        destination = self.root / 'actions-env'
        for bad in ['first\nSECOND=value', 'first\rsecond', 'first\0second']:
            with self.subTest(value=bad), self.assertRaisesRegex(ValueError, 'Actions environment'):
                sdk_environment.publish_install_environment({'GITHUB_ENV': str(destination), 'CARGO_HOME': bad})
        sdk_environment.publish_install_environment({'CARGO_HOME': self.cargo})
        sdk_environment.publish_install_environment({'GITHUB_ENV': str(destination)})
        self.assertFalse(destination.exists())

    def test_provider_carries_context_before_original_policy_and_native_operations(self):
        events = []
        policy = SimpleNamespace(setup=lambda root, env: events.append(('policy', dict(env))))
        def helper(name):
            if name == 'rust_install_context': return self.context
            if name == 'source_policy': return policy
            raise AssertionError(name)
        def run(argv, **_options):
            events.append((argv, dict(os.environ)))
        with patch.dict(os.environ, {'HOME': str(self.root), 'GITHUB_SHA': 'same'}, clear=True), \
             patch.object(provider, 'ROOT', self.root), \
             patch.object(provider, 'source', side_effect=lambda: events.append(('source', {}))), \
             patch.object(dependencies, 'helper', side_effect=helper), \
             patch.object(provider, 'run', side_effect=run):
            provider.setup()
        self.assertEqual([item[0] for item in events], ['source', 'policy',
            ['rustup', 'component', 'add', '--toolchain', '1.94.0', 'rustfmt', 'clippy']])
        self.assertTrue(all(environment['CARGO_HOME'] == self.cargo for _, environment in events[1:]))
        self.assertTrue(all(environment['GITHUB_SHA'] == 'same' for _, environment in events[1:]))

    def test_provider_task_keeps_python_private_parent_and_authenticated_source_preparation(self):
        data = tomllib.loads((SDK_ROOT / 'mise.toml').read_text())
        run = data['tasks']['setup']['run']
        self.assertEqual(len(run), 4)
        self.assertEqual(run[0], 'mise install python')
        self.assertIn('SDK bootstrap seed failed integrity validation', run[1])
        self.assertEqual(run[2], {'task': '_tools',
            'args': ['setup', 'helpers/rust_install_context.py', '--', 'mise', 'install']})
        self.assertEqual(run[-1]['task'], '_tools')
        self.assertEqual(run[-1]['args'][-2:], ['cookbook/provider.py', 'setup'])
        self.assertNotIn('mise install', run[1:])


if __name__ == '__main__':
    unittest.main()
