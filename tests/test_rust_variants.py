from source_fixture import SDK_ROOT
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import rust_tests
import test_native_ci


class NativeVariants(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'Cargo.toml').write_text('[package]\nname="owner"\nversion="0.0.1"\n')
        self.config = {'components': {'rust': [{'name': 'owner', 'manifest': 'Cargo.toml'}],
            'rust_tests': [{'name': 'source-install', 'component': 'owner', 'suite': 'build',
                            'reuse_native_build': True, 'selectors': {'test_targets': ['source_build']}}]}}

    def test_source_build_checks_run_locally_and_reuse_only_explicitly_joined_native_checks(self):
        self.assertEqual(len(rust_tests.eligible(self.root, self.config, {}, 'Linux/x86_64', 'build')), 1)
        joined = {'COOKBOOK_NATIVE_BUILD_CHECKS': '["source-install"]'}
        self.assertEqual(rust_tests.eligible(self.root, self.config, joined, 'Linux/x86_64', 'build'), [])
        for value in ['["unknown"]', '["source-install","source-install"]', '{}']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                rust_tests.eligible(self.root, self.config, {'COOKBOOK_NATIVE_BUILD_CHECKS': value}, 'Linux/x86_64', 'build')
        self.config['components']['rust_tests'][0]['reuse_native_build'] = False
        self.assertEqual(len(rust_tests.eligible(self.root, self.config, joined, 'Linux/x86_64', 'build')), 1)

    def test_plain_and_packed_owner_variants_receive_distinct_dependency_contexts(self):
        self.config['components']['native'] = [{'name': 'host', 'role': 'host', 'component': 'owner', 'binary': 'owner',
            'output': '.artifacts/native-host/owner_{platform}_{arch}'}]
        self.config['components']['rust_tests'] = [
            {'name': 'plain', 'component': 'owner', 'roles': ['host'], 'dependency_context': 'none', 'selectors': {'test_targets': ['flags']}},
            {'name': 'packed', 'component': 'owner', 'roles': ['host'], 'selectors': {'test_targets': ['flags']}}]
        values = []
        with patch.object(rust_tests.native_components, 'dependencies', return_value=({}, {'PAYLOAD': 'exact'})) as dependencies, \
                patch.object(rust_tests.rust_component, 'test', side_effect=lambda _root, _records, environment, *_args, **_kw: values.append(environment)):
            rust_tests.run_selected(self.root, self.config, {}, {'platform': 'Linux', 'arch': 'x86_64'}, ['plain', 'packed'], None)
        self.assertEqual(dependencies.call_count, 1)
        self.assertEqual(values, [{}, {'PAYLOAD': 'exact'}])

    def test_build_only_selector_cannot_erase_existing_native_unit_coverage(self):
        fixture = test_native_ci.NativeMatrix(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.config['components']['rust_tests'] = [{**value, 'component': 'cli'} for value in self.config['components']['rust_tests']]
        self.assertTrue(all(row['groups'] == ['unit'] for row in fixture.declaration()['requirements']))


if __name__ == '__main__': unittest.main()
