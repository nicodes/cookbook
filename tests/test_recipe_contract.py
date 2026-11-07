from source_fixture import SDK_ROOT
"""Closed declarations fail before execution and retain native capability contracts."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

COOKBOOK = SDK_ROOT / 'cookbook'
sys.path.insert(0, str(COOKBOOK))
import configuration
import images
import node_component
import recipe_contract
import recipe_schema
import react_native
import preview_workflows
import request_merge


def recipe(family='react-native', **values):
    return {'version': 1, 'recipe': family, **values}


class StrictRecipeContract(unittest.TestCase):
    def test_unknown_family_top_component_and_policy_shapes_fail_closed(self):
        values = [recipe('native'), recipe(version=True), recipe(commands={'test': 'true'}),
            recipe(components={'new_task': {}}), recipe(components={'app': {'unknown': True}}),
            recipe('rust-cli', dev={'kind': 'native'}), recipe(node={'packages': ['app']}),
            recipe(components={'app': None}), recipe('godot-game', components={'godot': {'rust_web': None}}),
            recipe(tests={'unit': [{'runner': 'python', 'path': 'tests/native.py', 'policy': {'unknown': True}}]})]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                configuration.validate_data(value)

    def test_retired_fields_are_refused_even_when_the_old_default_is_spelled(self):
        values = [recipe(components={'app': {'directory': 'app'}}),
            recipe(components={'app': {'verify_image': False}}),
            recipe(components={'app': {'verify_image': True}}),
            recipe(components={'compose': {'path': 'deploy/compose.yml'}}),
            recipe(artifacts={'definition': 'deploy/release.toml'}),
            recipe('godot-game', artifacts={'definition': 'deploy/release.toml'}),
            recipe('rust-cli', package={'output': '.artifacts/build/tool'}),
            recipe('rust-cli', artifacts={'revision_environment': 'VERSION'}),
            recipe('godot-addon', package={'source': 'addon'}),
            recipe('godot-addon', publication={'plugin': 'addon/plugin.cfg'}),
            recipe('godot-game', components={'godot': {'rust_web': {'directory': '.artifacts/toolchains/rust-web'}}}),
            recipe('godot-game', ci={'godot': {'reset_import_cache': True}})]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                configuration.validate_data(value)

    def test_shared_cases_preserve_every_ordered_native_entry_without_mutating_input(self):
        common = {'runner': 'python', 'cwd': 'tests', 'timeout': 1500}
        cases = [{'path': 'tests/one.py', 'args': ['literal;value'], 'name': 'native-one'},
                 {'path': 'tests/two.py', 'args': ['two'], 'name': 'native-two'}]
        original = recipe(tests={'integration': [{**common, 'cases': cases}]})
        before = deepcopy(original)
        actual = configuration.validate_data(original)
        self.assertEqual(actual['tests']['integration'], [{**common, **case} for case in cases])
        self.assertEqual(original, before)
        self.assertEqual(configuration.validate_data(actual), actual)

    def test_effective_definition_is_a_pure_sdk_api_without_retired_key_injection(self):
        data = configuration.validate_data(recipe(components={'app': {}}))
        before = deepcopy(data)
        self.assertEqual(configuration.release_definition(data), 'deploy/release.toml')
        self.assertEqual(data, before)
        self.assertNotIn('artifacts', data)
        with patch.object(Path, 'read_text', side_effect=AssertionError('pure decoder read a source file')):
            self.assertEqual(configuration.validate_data(data), data)

    def test_remote_merge_and_preview_authorities_use_the_same_closed_validator(self):
        invalid = recipe(components={'app': {'verify_image': False}})
        with patch.object(preview_workflows, 'declaration') as read:
            with self.assertRaises(ValueError):
                preview_workflows.auth_contract(Path('/unused'), invalid)
            read.assert_not_called()
        with patch.object(request_merge, 'api', side_effect=[{'merge_commit_sha':'c'*40}, {}]) as api, \
                patch.object(request_merge.candidate, 'snapshot', return_value={'head':'a'*40,'base':'b'*40}), \
                patch.object(request_merge, 'source_file', return_value=
                    'version=1\nrecipe="react-native"\n[components.app]\nverify_image=false\n'), \
                patch.object(request_merge, 'status') as status:
            with self.assertRaises(ValueError):
                request_merge.snapshot('example/native', 1)
            self.assertEqual(api.call_count, 2)
            status.assert_not_called()

    def test_native_runners_and_original_ci_worker_budgets_remain_available(self):
        self.assertEqual(recipe_schema.RUNNERS, {'python', 'python-unittest', 'node', 'node-test',
            'bun', 'bun-test', 'bash', 'godot', 'godot-suite', 'godot-check', 'playwright', 'godot-browser'})
        for runner, timeout in [('ubuntu-22.04', 30), ('ubuntu-24.04', 90),
                ('blacksmith-4vcpu-ubuntu-2204', 90), ('blacksmith-4vcpu-ubuntu-2404', 180)]:
            data = recipe(ci={'runner': runner, 'timeout_minutes': timeout,
                'dependency_security': {'runner': 'blacksmith-4vcpu-ubuntu-2404',
                    'timeout_minutes': 10, 'setup_timeout_minutes': 5}})
            self.assertEqual(configuration.validate_data(data), data)
            self.assertEqual(configuration.ci_settings(data), {'runner': runner, 'timeout_minutes': timeout})
        data = recipe('godot-game', ci={'runner': 'blacksmith-4vcpu-ubuntu-2404', 'timeout_minutes': 45,
            'godot': {'unit_workers': 4, 'unit_shards': 1, 'unit_timeout_minutes': 45,
                'build_timeout_minutes': 45, 'host_timeout_minutes': 45}})
        self.assertEqual(configuration.validate_data(data), data)

    def test_native_mobile_and_disposable_variants_are_not_removed_as_path_exceptions(self):
        data = recipe(components={'app': {'native_exports': [{'platforms': ['ios', 'android'],
                'output': '.artifacts/native', 'timeout': 900}],
            'fixture_exports': [{'output': 'dist-e2e', 'environment': {'BUILD_VARIANT': 'local'}, 'timeout': 900},
                {'output': 'dist-check', 'environment': {'BUILD_VARIANT': 'production'}, 'timeout': 900}],
            'image_source': 'app/dist', 'installed_root': '/srv/app'},
            'compose': {'environment_files': ['secrets.env', 'secrets/postgres-owner.env']}},
            node={'packages': [{'directory': 'app', 'manager': 'bun', 'test': 'test'}]})
        self.assertEqual(configuration.validate_data(data), data)
        self.assertEqual(recipe_contract.app_directory(data), 'app')
        self.assertEqual(recipe_contract.compose_path(data), 'deploy/compose.yml')
        self.assertEqual(recipe_contract.release_definition(data), 'deploy/release.toml')

    def test_canonical_component_roots_reject_alternates_escapes_and_staging_outside_artifacts(self):
        bad = [recipe(node={'packages': [{'directory': 'poc/app'}]}),
            recipe(node={'packages': [{'directory': '../app'}]}),
            recipe(node={'packages': [{'directory': 'app/../tests'}]}),
            recipe('rust-cli', components={'rust': [{'manifest': 'elsewhere/Cargo.toml'}]}),
            recipe('rust-cli', components={'rust': [{'manifest': 'crates/app/Cargo.toml', 'target_directory': '/tmp/target'}]}),
            recipe('rust-cli', components={'rust': [{'manifest': 'crates/app/Cargo.toml', 'target_directory': 'target'}]}),
            recipe('rust-cli', components={'native': [{'name': 'app', 'role': 'cli', 'output': 'dist/app'}]}),
            recipe('godot-game', components={'godot': {'exports': [{'preset': 'Web', 'output': 'public/index.html'}]}}),
            recipe('godot-game', components={'godot': {'exports': [{'preset': 'Web', 'output': 'app/build/../web/index.html'}]}})]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                configuration.validate_data(value)
        for family, manifest in [('rust-cli', 'crates/probe-rust/Cargo.toml'),
                                 ('godot-game', 'tests/fixtures/native/Cargo.toml')]:
            value = recipe(family, components={'rust': [{'manifest': manifest, 'target_directory': '.artifacts/rust-target'}]})
            self.assertEqual(configuration.validate_data(value), value)
        value = recipe('godot-game', components={'godot': {'exports': [{'preset': 'Web',
            'output': 'app/build/web/index.html', 'kind': 'release'}], 'rust_web': {}}})
        self.assertEqual(configuration.validate_data(value), value)

    def test_recipe_cargo_build_aliases_and_unknown_nested_selectors_are_refused(self):
        base = {'manifest': 'crates/app/Cargo.toml', 'profile': 'release'}
        for extra in [{'release': False}, {'release': True}, {'build': {'release': False}},
                      {'build': {'release': True}}, {'build': {'unknown': True}},
                      {'lint': {'unknown': True}}, {'test': {'unknown': True}}]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                configuration.validate_data(recipe('rust-cli', components={'rust': [{**base, **extra}]}))
        data = recipe('rust-cli', components={'rust': [{**base,
            'test': {'release': True, 'features': ['native-fixture'], 'test_arguments': ['--nocapture']}}]})
        self.assertEqual(configuration.validate_data(data), data)
        data = recipe('react-native', components={'rust': [{'manifest': 'api/Cargo.toml',
            'profile': 'debug', 'features': ['localdev'], 'test': {'enabled': False}}]})
        self.assertEqual(configuration.validate_data(data), data)

    def test_node_shorthand_audit_waivers_and_severity_selection_are_rejected_before_native_work(self):
        for package in ['app', {'directory': 'app', 'audit': False}, {'directory': 'app', 'audit': True},
                        {'directory': 'app', 'audit_level': 'critical'}]:
            with self.subTest(package=package), self.assertRaises(ValueError):
                configuration.validate_data(recipe(node={'packages': [package]}))
            with self.subTest(package=package), self.assertRaises(ValueError):
                node_component.declaration(package)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'app').mkdir(); (root/'app/package.json').write_text('{}')
            calls = []
            def run(command, cwd, environment): calls.append((command, cwd))
            self.assertEqual(node_component.audit(root, [{'directory': 'app'}, {'directory': 'app', 'manager': 'bun'}], {}, run), 2)
            self.assertEqual(calls[0], (['npm', 'audit', '--audit-level=high'], root/'app'))
            from source_fixture import native_entry
            self.assertEqual(native_entry(calls[1][0]), [sys.executable, str(node_component.TOOLS/'helpers/audit.py'), str(root/'app')])
            def failed(*_args): raise subprocess.CalledProcessError(1, 'native audit')
            with self.assertRaises(subprocess.CalledProcessError):
                node_component.audit(root, [{'directory': 'app'}], {}, failed)

    def test_native_image_comparison_has_no_waiver_at_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'public').mkdir()
            data = recipe(components={'app': {'image_source': 'public', 'installed_root': '/srv/public'}})
            with patch.object(images, 'validate_release'), patch.object(images, 'definition', return_value={'image_base':'ghcr.io/example/app'}), \
                    patch.object(images, 'static_image') as verify, patch.object(react_native, 'phase') as phase:
                react_native.validate_outputs(root, data, {'APP_VERSION':'a'*40})
            verify.assert_called_once_with('ghcr.io/example/app-gate:'+'a'*40, root/'public', '/srv/public')
            phase.assert_called_once_with(root, data, {'APP_VERSION':'a'*40}, 'validate')

    def test_foreign_cached_policy_modules_are_refused_before_use(self):
        code = '''import importlib.util,sys,types
from pathlib import Path
sdk=Path(sys.argv[1]);target=sys.argv[2]
foreign=types.ModuleType(target);foreign.__file__='/foreign/sdk/'+target+'.py';sys.modules[target]=foreign
sys.path.insert(0,str(sdk))
name='configuration' if target=='recipe_schema' else 'recipe_schema'
spec=importlib.util.spec_from_file_location('checked_'+name,sdk/(name+'.py'));module=importlib.util.module_from_spec(spec)
try: spec.loader.exec_module(module)
except ValueError as error:
 assert 'origin differs' in str(error),str(error)
 print('REFUSED foreign '+target)
else: raise AssertionError('cached foreign policy was admitted')
'''
        for name in ['recipe_schema', 'recipe_contract']:
            result = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(COOKBOOK), name],
                check=True, capture_output=True, text=True, timeout=10)
            self.assertIn('REFUSED foreign '+name, result.stdout)

    def test_exact_trusted_mirror_and_prepared_sdk_share_the_same_pure_policy(self):
        code = '''import json,sys
sys.path.insert(0,sys.argv[1]);import recipe_schema
out=[]
for value in json.loads(sys.stdin.read()):
 try: out.append({'accepted':recipe_schema.validate_data(value)})
 except ValueError: out.append({'refused':True})
print(json.dumps(out,sort_keys=True))
'''
        cases = [recipe(), recipe(node={'packages': [{'directory': 'app', 'manager': 'bun'}]}),
            recipe(components={'app': {'verify_image': False}}), recipe('native'),
            recipe('godot-game', components={'godot': {'exports':[{'preset':'Web','output':'app/build/web/index.html'}]}})]
        with tempfile.TemporaryDirectory() as directory:
            mirror = Path(directory)
            for name in ['recipe_schema.py', 'recipe_contract.py']:
                shutil.copyfile(COOKBOOK/name, mirror/name)
            outputs = []
            for root in [COOKBOOK, mirror]:
                result = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(root)], input=json.dumps(cases),
                    capture_output=True, text=True, check=True, timeout=10)
                outputs.append(json.loads(result.stdout))
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual([bool(row.get('refused')) for row in outputs[0]], [False,False,True,True,False])


if __name__ == '__main__': unittest.main()
