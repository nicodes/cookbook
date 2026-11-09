from source_fixture import SDK_ROOT
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

COOKBOOK = SDK_ROOT / 'cookbook'
sys.path.insert(0, str(COOKBOOK))
spec = importlib.util.spec_from_file_location('react_native_recipe', COOKBOOK / 'react_native.py')
recipe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recipe)


class ReactNativeRecipe(unittest.TestCase):
    def setUp(self):
        recipe.dependencies.helper('rust_install_context')
        temporary = tempfile.TemporaryDirectory(prefix='native application ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / 'source.txt').write_text('owned source\n')
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Recipe Controls',
                        '-c', 'user.email=recipe@example.invalid', 'commit', '-qm', 'fixture'], check=True)
        self.source = subprocess.check_output(['git', '-C', str(self.root), 'rev-parse', 'HEAD'], text=True).strip()

    def test_build_defaults_are_bound_without_overwriting_workflow_identity_or_explicit_empty_values(self):
        config = {'components': {'app': {'environment': {'EXPO_PUBLIC_API_URL': 'https://api.example.invalid'}}}}
        with patch.dict(os.environ, {'EXPO_PUBLIC_API_URL': '', 'GITHUB_SHA': 'a' * 40, 'GH_TOKEN': 'test-context'}, clear=True):
            result = recipe.configure_environment(self.root, config)
            self.assertEqual(result['APP_VERSION'], self.source)
            self.assertEqual(result['EXPO_PUBLIC_API_URL'], '')
            self.assertEqual(result['GITHUB_SHA'], 'a' * 40)
            self.assertEqual(result['GH_TOKEN'], 'test-context')
        for name in ['APP_VERSION', 'SOURCE_COMMIT', 'GITHUB_SHA', 'GH_TOKEN', 'MISE_AUTO_INSTALL', 'KOMIZO_ACTIONS_ROOT']:
            with self.subTest(name=name), patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, 'authority'):
                recipe.configure_environment(self.root, {'components': {'app': {'environment': {name: 'replacement'}}}})

    def test_source_security_selects_this_checkout_without_inherited_source_alias(self):
        import godot_security
        definition = {'project': 'fixture-app', 'image_base': 'ghcr.io/fixture/app',
                      'inputs': {'images': ['ghcr.io/fixture/app-api:{revision}']}}
        for supplied in [None, '', self.source]:
            inherited = {'GITHUB_SHA': 'a' * 40}
            if supplied is not None:
                inherited['SOURCE_COMMIT'] = supplied
            with self.subTest(supplied=supplied), patch.dict(os.environ, inherited, clear=True):
                environment = recipe.configure_environment(self.root, {})
                with patch.object(godot_security.images, 'definition', return_value=definition), \
                     patch.object(godot_security, 'scan_references') as scan:
                    godot_security.scan(self.root, {}, environment, recipe.run)
                self.assertEqual(environment['APP_VERSION'], self.source)
                self.assertEqual(environment['SOURCE_COMMIT'], self.source)
                self.assertEqual(scan.call_args.args[4], [('api', 'ghcr.io/fixture/app-api:' + self.source)])

    def test_foreign_source_alias_is_refused_before_sdk_selection_or_environment_updates(self):
        with patch.dict(os.environ, {'APP_VERSION': self.source, 'SOURCE_COMMIT': 'a' * 40}, clear=True), \
             patch.object(recipe.komizo_sdk, 'source_root') as sdk:
            before = dict(os.environ)
            with self.assertRaisesRegex(ValueError, 'SOURCE_COMMIT must match this exact source checkout'):
                recipe.configure_environment(self.root, {})
            self.assertEqual(dict(os.environ), before)
            sdk.assert_not_called()
        with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'pull_request'}, clear=True), \
             self.assertRaisesRegex(ValueError, 'authority'):
            recipe.configure_environment(self.root, {'components': {'app': {
                'preview_environment': {'SOURCE_COMMIT': 'a' * 40}}}})

    def test_declared_komizo_source_is_bound_without_a_hidden_setup_or_ambient_override(self):
        expected=self.root/'.artifacts/sdk/komizo/source'
        with patch.object(recipe.komizo_sdk,'source_root',return_value=expected),patch.dict(os.environ,{},clear=True):
            self.assertEqual(recipe.configure_environment(self.root,{})['KOMIZO_ACTIONS_ROOT'],str(expected))
            self.assertFalse(expected.exists())
        with patch.object(recipe.komizo_sdk,'source_root',return_value=expected),\
             patch.dict(os.environ,{'KOMIZO_ACTIONS_ROOT':'/unreviewed/source'},clear=True):
            with self.assertRaisesRegex(ValueError,'exact accepted SDK'):recipe.configure_environment(self.root,{})

    def test_preview_requires_public_input_only_when_compiling_the_app(self):
        config = {'components': {'app': {'preview_environment': {'EXPO_PUBLIC_API_URL': '/api'},
                 'preview_variables': {'EXPO_PUBLIC_CLERK_PUBLISHABLE_KEY': 'COOKBOOK_PREVIEW_CLERK_KEY'}}}}
        with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'pull_request'}, clear=True):
            # Lint/source setup can run without a delivery environment.
            result = recipe.configure_environment(self.root, config)
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                recipe.build(self.root, config, result)
            os.environ['COOKBOOK_PREVIEW_CLERK_KEY'] = 'pk_test_fixture'
            result = recipe.configure_environment(self.root, config)
            self.assertEqual(result['EXPO_PUBLIC_API_URL'], '/api')
            self.assertEqual(result['EXPO_PUBLIC_CLERK_PUBLISHABLE_KEY'], 'pk_test_fixture')
        self.assertTrue(recipe.preview_context({'GITHUB_EVENT_NAME': 'workflow_dispatch', 'REQUEST_HEAD': 'a' * 40, 'REQUEST_BASE': 'b' * 40}))

    def test_native_components_are_owned_programs_with_literal_arguments(self):
        directory = self.root / 'internal/compiler'
        directory.mkdir(parents=True)
        program = directory / 'main.py'
        program.write_text('import json,sys\nfrom pathlib import Path\nPath("result.json").write_text(json.dumps(sys.argv[1:]))\n')
        entry = {'runner': 'python', 'path': 'internal/compiler/main.py', 'args': ['literal;value']}
        recipe.native(self.root, [entry], {'APP_VERSION': self.source})
        self.assertEqual(json.loads((self.root / 'result.json').read_text()), ['literal;value'])
        (self.root / 'pipeline.py').write_text('raise SystemExit(0)\n')
        with self.assertRaisesRegex(ValueError, 'owning component'):
            recipe.native(self.root, [entry | {'path': 'pipeline.py'}], {'APP_VERSION': self.source})

    def test_native_component_metadata_cannot_create_an_empty_behavioral_success(self):
        with self.assertRaisesRegex(ValueError, 'no real implemented checks'):
            recipe.behavioral_tests(self.root, {}, {}, 'unit')

    def test_build_only_fixture_features_cannot_replace_product_test_component(self):
        config = {'components': {'rust': [
            {'manifest': 'api/Cargo.toml', 'features': [], 'environment': {}},
            {'manifest': 'api/Cargo.toml', 'features': ['localdev'], 'test': {'enabled': False}, 'environment': {}}],
            'rust_tests': [{'manifest': 'api/Cargo.toml', 'suite': 'unit', 'selectors': {'lib': True}}]}}
        from unittest.mock import Mock
        cargo = Mock()
        with patch.object(recipe, 'cargo', return_value=cargo):
            recipe.behavioral_tests(self.root, config, {}, 'unit')
        selected = cargo.test.call_args.args[1][0]
        self.assertEqual(selected['features'], [])
        self.assertEqual(selected['test'], {'lib': True})

    def test_required_tools_select_only_current_lane_capabilities(self):
        (self.root / 'mise.toml').write_text('''[tools]
python="3.13.11"
node="24.18.1"
actionlint="1.7.12"
trivy="0.74.0"
mprocs="0.9.6"
''')
        self.assertEqual(recipe.required_tools(self.root, {}, 'fast'), ['actionlint', 'node', 'python'])
        self.assertNotIn('trivy', recipe.required_tools(self.root, {'components': {'image_security': {'reports': '.artifacts/reports/security'}}}, 'security'))
        self.assertNotIn('trivy', recipe.required_tools(self.root, {}, 'security'))
        self.assertNotIn('mprocs', recipe.required_tools(self.root, {}, 'full'))

    def test_setup_resolves_newly_installed_sdks_through_mise_instead_of_ambient_path(self):
        (self.root / 'mise.toml').write_text('[tools]\npython="' + '.'.join(map(str, sys.version_info[:3])) + '"\nnode="24.18.1"\nactionlint="1.7.12"\n')
        calls = []
        def install(root, packages, environment, run):
            run(['node', '--version'], root, environment)
        selected = recipe.dependencies.helper('pinned-tools')
        exact = {'PATH': '/reviewed/node/bin', 'MISE_AUTO_INSTALL': 'false',
                 'MISE_EXEC_AUTO_INSTALL': 'false', 'MISE_TASK_RUN_AUTO_INSTALL': 'false'}
        def execute(arguments, *, cwd, env, **options):
            calls.append((arguments, cwd, env))
            return subprocess.CompletedProcess(arguments, 0)
        with patch.object(selected, 'activation', return_value=exact) as activation, \
                patch.object(recipe.subprocess, 'run', side_effect=execute), \
                patch.object(recipe.node_component, 'install', side_effect=install):
            recipe.setup(self.root, {}, {})
        self.assertEqual(calls[0][0][:2], ['mise', 'install'])
        self.assertEqual(calls[1][0], ['node', '--version'])
        activation.assert_called_once_with(self.root, [('node', 'node')], {})
        self.assertEqual(calls[1][1:], (self.root,
            exact | {'BUN_OPTIONS': '--no-env-file', 'EXPO_NO_DOTENV': '1'}))

    def test_lint_keeps_plain_package_checks_and_enforces_expo_compatibility_only_for_expo(self):
        packages = [
            {'directory': 'app', 'manager': 'bun', 'expo': True, 'typecheck': 'typecheck'},
            {'directory': 'internal/worker', 'manager': 'bun', 'typecheck': 'typecheck'},
        ]
        for package in packages:
            directory = self.root / package['directory']
            directory.mkdir(parents=True)
            (directory / 'package.json').write_text(json.dumps({'scripts': {'typecheck': 'tsc --noEmit'}}))
        config = {'node': {'packages': packages}}
        calls = []
        def execute(command, directory, environment):
            calls.append((command, directory))
        with patch.object(recipe, 'run', side_effect=execute), \
                patch.object(recipe.source_policy, 'check'), patch.object(recipe.images, 'validate_application'):
            recipe.lint(self.root, config, {})
        compatibility = ['node', str(recipe.TOOLS / 'helpers/dependency-compatibility.cjs'), str(self.root / 'app')]
        self.assertEqual([command for command, _ in calls if 'dependency-compatibility.cjs' in ' '.join(command)], [compatibility])
        self.assertIn((['bun', 'x', '--no-install', 'expo', 'install', '--check'], self.root / 'app'), calls)
        for package in packages:
            self.assertIn((['bun', 'run', 'typecheck'], self.root / package['directory']), calls)

        def refuse_compatibility(command, directory, environment):
            if command == compatibility:
                raise subprocess.CalledProcessError(1, command)
        with patch.object(recipe, 'run', side_effect=refuse_compatibility), \
                patch.object(recipe.source_policy, 'check'), patch.object(recipe.images, 'validate_application'):
            with self.assertRaises(subprocess.CalledProcessError):
                recipe.lint(self.root, config, {})
        packages[1]['expo'] = 'false'
        with patch.object(recipe, 'run'), patch.object(recipe.source_policy, 'check'):
            with self.assertRaisesRegex(ValueError, 'must be booleans'):
                recipe.lint(self.root, config, {})

    def test_expo_development_passes_selected_port_and_never_signals_listeners(self):
        (self.root / 'app').mkdir()
        calls = []
        with patch.object(recipe, 'select_port', return_value=8181), patch.object(recipe, 'run', side_effect=lambda *args: calls.append(args)):
            recipe.launch_development(self.root, {'dev': {'kind': 'expo', 'port': 8081}}, {})
        self.assertEqual(calls[0][0], ['bun', '--no-env-file', 'run', '--cwd', str(self.root / 'app'), 'start', '--port', '8181'])
        self.assertEqual(calls[0][2], {'EXPO_NO_DOTENV': '1'})
        self.assertEqual(len(calls), 1)

    def test_supervised_native_development_remains_long_lived_without_disabling_build_deadlines(self):
        (self.root / 'internal/dev').mkdir(parents=True)
        (self.root / 'internal/dev/stack.py').write_text('raise SystemExit(0)\n')
        entry = {'runner': 'python', 'path': 'internal/dev/stack.py'}
        calls = []
        with patch.object(recipe, 'run', side_effect=lambda *args: calls.append(args)):
            recipe.launch_development(self.root, {'dev': {'kind': 'native', 'program': entry}}, {'APP_VERSION': self.source})
            recipe.native(self.root, [entry], {'APP_VERSION': self.source})
        self.assertIsNone(calls[0][-1])
        self.assertEqual(calls[1][-1], 300)

    def test_database_reset_isolated_to_local_identity_and_owned_checkout_lease(self):
        (self.root / 'ghenv.toml').write_text('version=1\n')
        calls = []
        config = {'dev': {'kind': 'native'}, 'database': {'reset': {'runner': 'python', 'path': 'internal/dev/database.py'}}}
        with patch.object(recipe, 'pinned_ghenv', return_value='/owned/sdk/ghenv'), patch.object(recipe, 'run', side_effect=lambda *args: calls.append(args)):
            recipe.database(self.root, config, {'APP_VERSION': self.source}, 'db:reset')
        command = calls[0][0]
        self.assertIn('clean', command)
        self.assertIn('/owned/sdk/ghenv', command)
        self.assertIn('local', command)
        self.assertEqual(command[-3:], ['_database', '--suite', 'reset'])

    def test_native_development_cleanup_runs_only_the_owned_stop_program(self):
        (self.root/'internal').mkdir()
        (self.root/'internal/stop.py').write_text('raise SystemExit(0)')
        config={'dev':{'cleanup':{'runner':'python','path':'internal/stop.py','args':['stop']}}}
        calls=[]
        with patch.object(recipe,'native',side_effect=lambda *args,**kwargs:calls.append((args,kwargs))):
            recipe.stop_services(self.root,config,{})
        self.assertEqual(calls[0][0][1],[config['dev']['cleanup']])
        self.assertEqual(config['dev']['cleanup']['args'],['stop'])

    def test_browser_assertion_failure_cleans_only_the_owned_sdk_descendants(self):
        from unittest.mock import Mock
        directory = self.root / 'app/tests'
        directory.mkdir(parents=True)
        (directory / 'e2e.config.ts').write_text('export default {};\n')
        entry = {'runner': 'playwright', 'path': 'app/tests/e2e.config.ts', 'cwd': 'app', 'timeout': 30}
        child = Mock()
        child.wait.return_value = 1
        processes = Mock()
        with patch.object(recipe, 'helper', return_value=processes), patch.object(recipe.subprocess, 'Popen', return_value=child):
            with self.assertRaisesRegex(ValueError, 'assertions failed'):
                recipe.playwright(self.root, entry, {})
        processes.adopt_descendants.assert_called_once()
        processes.stop_descendants.assert_called_once_with(child)
        with patch.object(recipe.subprocess, 'Popen') as popen, self.assertRaisesRegex(ValueError, 'skip/list'):
            recipe.playwright(self.root, entry | {'args': ['--list']}, {})
        popen.assert_not_called()

    def test_port_selection_refuses_an_occupied_port_without_killing_its_owner(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
            with self.assertRaisesRegex(ValueError, 'no available'):
                recipe.select_port(port, 1)
            self.assertEqual(listener.getsockname()[1], port)


if __name__ == '__main__':
    unittest.main()
