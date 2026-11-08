"""A prepared native Rust SDK must reach transitive compiler subprocesses."""
from source_fixture import SDK_ROOT, SDK_REF
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import native_ci as native
import test_native_ci as fixture


class NativeExecutionEnvironment(unittest.TestCase):
    def setUp(self):
        fixture.NativeMatrix.setUp(self)
        self.entry = native.plan(self.root, self.config)['requirements'][0]
        self.activation_calls = []
        self.commands = []
        self.nested_environment = None

    def invoke(self, *, activation_failure=False, setup_failure=False,
               check_failure=False, replacement=None):
        def activate(root, required, environment):
            self.activation_calls.append((root, required, dict(environment)))
            self.assertEqual([argv[-1] for argv, _ in self.commands], ['setup'])
            self.assertEqual(root, self.root)
            self.assertEqual(required, [('rust', 'cargo'), ('rust', 'rustc'), ('rust', 'rustup')])
            if activation_failure:
                raise ValueError('required exact Rust toolchain is not prepared; run explicit setup')
            values = {**environment, 'RUSTUP_HOME': str(self.root / 'prepared-rustup'),
                      'RUSTUP_TOOLCHAIN': '1.94.0', 'RUSTUP_AUTO_INSTALL': '0',
                      'PATH': str(self.root / 'prepared-rust-proxies')}
            if replacement:
                values.update(replacement)
            return values

        def execute(argv, **options):
            self.assertTrue(options['check'])
            self.assertEqual(options['cwd'], self.root)
            self.commands.append((argv, dict(options['env'])))
            if argv[-1] == 'setup' and setup_failure:
                raise subprocess.CalledProcessError(29, argv)
            if argv[-1] == 'test:integration':
                # The existing native source boundary uses isolated absolute
                # Python, and its own app-data fixture changes XDG homes. Its
                # transitive compiler still needs the already prepared SDK.
                self.assertEqual(Path(argv[0]).resolve(), Path(sys.executable).resolve())
                self.nested_environment = dict(options['env'],
                    XDG_CONFIG_HOME=str(self.root / 'isolated-config'),
                    XDG_DATA_HOME=str(self.root / 'isolated-data'))
                if check_failure:
                    raise subprocess.CalledProcessError(31, argv)
            return subprocess.CompletedProcess(argv, 0)

        environment = {**self.environment, 'COOKBOOK_CALLER_ROOT': str(self.root),
                       'COOKBOOK_ROOT': str(SDK_ROOT), 'COOKBOOK_REF': SDK_REF,
                       'TOOLS_ROOT': str(native.TOOLS), 'TOOLS_REF': native.dependencies.tools_identity()['revision'],
                       'COMMIT': self.source, 'VERSION': 'dev', 'DATE': 'source-fixture'}
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(native, 'load', return_value=self.config), \
                patch.object(native.ci, 'identity', return_value=self.tree), \
                patch.object(native, 'helper', return_value=SimpleNamespace(host=lambda: ('Linux', 'x86_64'), activation=activate)), \
                patch.object(native.recipe, 'configure_environment'), \
                patch.object(native.subprocess, 'run', side_effect=execute), \
                patch.object(native, 'collect', return_value={'native': 'unit-only proof'}) as collect, \
                patch.object(native, 'export') as export:
            try:
                native.run_entry(self.root, self.source, self.tools, self.entry['id'])
            except BaseException:
                collect.assert_not_called()
                export.assert_not_called()
                raise
        return collect, export

    def test_prepared_rust_reaches_absolute_python_checks_after_xdg_isolation(self):
        collect, export = self.invoke()
        self.assertEqual([argv[-1] for argv, _ in self.commands], ['setup', 'build', 'test:unit', 'test:integration'])
        self.assertEqual(len(self.activation_calls), 1)
        self.assertNotIn('RUSTUP_HOME', self.commands[0][1])
        for _argv, environment in self.commands[1:]:
            self.assertEqual(environment['RUSTUP_HOME'], str(self.root / 'prepared-rustup'))
            self.assertEqual(environment['RUSTUP_TOOLCHAIN'], '1.94.0')
            self.assertEqual(environment['RUSTUP_AUTO_INSTALL'], '0')
        self.assertEqual(self.nested_environment['RUSTUP_HOME'], str(self.root / 'prepared-rustup'))
        self.assertEqual(self.nested_environment['RUSTUP_TOOLCHAIN'], '1.94.0')
        self.assertEqual(self.nested_environment['XDG_DATA_HOME'], str(self.root / 'isolated-data'))
        collect.assert_called_once()
        export.assert_called_once_with(self.root, {'native': 'unit-only proof'})

    def test_failed_setup_never_activates_or_starts_a_compiler(self):
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.invoke(setup_failure=True)
        self.assertEqual(error.exception.returncode, 29)
        self.assertEqual([argv[-1] for argv, _ in self.commands], ['setup'])
        self.assertEqual(self.activation_calls, [])

    def test_missing_prepared_rust_refuses_before_build_or_native_checks(self):
        with self.assertRaisesRegex(ValueError, 'not prepared'):
            self.invoke(activation_failure=True)
        self.assertEqual([argv[-1] for argv, _ in self.commands], ['setup'])

    def test_replaced_caller_source_or_actions_identity_refuses_before_execution(self):
        for key in ['COOKBOOK_CALLER_ROOT', 'COOKBOOK_ROOT', 'COOKBOOK_REF',
                    'TOOLS_ROOT', 'TOOLS_REF', 'GITHUB_SHA', 'COMMIT', 'RUST_TARGET']:
            with self.subTest(key=key):
                self.commands = []
                with self.assertRaisesRegex(ValueError, 'caller/source and Actions identity'):
                    self.invoke(replacement={key: 'foreign-unit-value'})
                self.assertEqual([argv[-1] for argv, _ in self.commands], ['setup'])

    def test_native_check_failure_keeps_argv_status_and_blocks_proof(self):
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.invoke(check_failure=True)
        self.assertEqual(error.exception.returncode, 31)
        self.assertEqual(error.exception.cmd[-1], 'test:integration')
        self.assertEqual([argv[-1] for argv, _ in self.commands], ['setup', 'build', 'test:unit', 'test:integration'])


if __name__ == '__main__':
    unittest.main()
