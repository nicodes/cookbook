from source_fixture import SDK_ROOT
"""Compact declarations preserve the complete native execution and evidence contract."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import configuration
import generate
import godot_ci
import native_tests


def literal(value):
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(key) + '=' + literal(item) for key, item in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ','.join(map(literal, value)) + ']'
    return json.dumps(value)


class NativeCaseGroups(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='native case checkout ')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'tests').mkdir()
        (self.root / 'tests/first.py').write_text('assert True\n')
        (self.root / 'tests/second.py').write_text('assert True\n')
        (self.root / 'app/tests').mkdir(parents=True)
        for name in ['first', 'second']:
            (self.root / 'app/tests' / (name + '.gd')).write_text('extends Node\n')

    def write(self, tests, recipe='godot-addon', **settings):
        data = {'version': 1, 'recipe': recipe, 'tests': tests, **settings}
        body = '\n'.join(key + '=' + literal(value) for key, value in data.items()) + '\n'
        (self.root / 'cookbook.toml').write_text(body)
        return body

    def group(self):
        return {'runner': 'python', 'cwd': 'tests', 'timeout': 7,
                'cases': [{'path': 'tests/first.py', 'name': 'test-first', 'args': ['literal;value']},
                          {'path': 'tests/second.py', 'name': 'test-second', 'args': ['second']} ]}

    def test_all_four_families_expand_to_the_exact_ordered_original_native_entries(self):
        group = self.group()
        before = {'runner': 'python', 'path': 'tests/first.py', 'args': ['before']}
        after = {'runner': 'python', 'path': 'tests/second.py', 'args': ['after']}
        shared = {key: value for key, value in group.items() if key != 'cases'}
        expected = [before, *[{**shared, **case} for case in group['cases']], after]
        for family in ['godot-addon', 'godot-game', 'react-native', 'rust-cli']:
            with self.subTest(family=family):
                self.write({'unit': expected}, family)
                original = configuration.load(self.root)
                self.write({'unit': [before, group, after]}, family)
                self.assertEqual(configuration.load(self.root), original)
                self.assertEqual(original['tests']['unit'], expected)

    def test_literal_cases_run_in_order_with_original_arguments_cwd_and_deadlines(self):
        source = ('from pathlib import Path\nimport sys\n'
                  'assert Path.cwd().name == "tests"\n'
                  'with Path("execution.log").open("a") as output:\n'
                  ' output.write(Path(__file__).stem + ":" + sys.argv[1] + "\\n")\n')
        for name in ['first.py', 'second.py']:
            (self.root / 'tests' / name).write_text(source)
        self.write({'unit': [self.group()]})
        config = configuration.load(self.root)
        real_run = subprocess.run
        with patch.object(native_tests.subprocess, 'run', wraps=real_run) as invoke:
            native_tests.run_entries(self.root, config, dict(os.environ), 'unit')
        self.assertEqual((self.root / 'tests/execution.log').read_text(),
                         'first:literal;value\nsecond:second\n')
        self.assertEqual(len(invoke.call_args_list), 2)
        for call in invoke.call_args_list:
            self.assertEqual(call.kwargs['cwd'], self.root / 'tests')
            self.assertEqual(call.kwargs['timeout'], 7)
            self.assertTrue(call.kwargs['check'])

    def test_a_failed_native_case_still_fails_the_group(self):
        (self.root / 'tests/second.py').write_text('raise SystemExit(23)\n')
        self.write({'unit': [self.group()]})
        with self.assertRaises(subprocess.CalledProcessError) as failed:
            native_tests.run_entries(self.root, configuration.load(self.root), dict(os.environ), 'unit')
        self.assertEqual(failed.exception.returncode, 23)

    def test_exact_godot_case_ids_shards_and_host_coverage_are_preserved(self):
        shared = {'runner': 'godot-check', 'cwd': 'app', 'timeout': 180,
                  'policy': {'deferred_autoloads': True}}
        cases = [{'path': 'app/tests/first.gd', 'name': 'test-first'},
                 {'path': 'app/tests/second.gd', 'name': 'test-second'}]
        native = {'runner': 'python', 'path': 'tests/first.py', 'timeout': 7}
        ci = {'runner': 'ubuntu-24.04', 'timeout_minutes': 20,
              'godot': {'unit_shards': 2, 'unit_workers': 4,
                        'build_timeout_minutes': 25, 'unit_timeout_minutes': 20, 'host_timeout_minutes': 30}}
        expected = [{**shared, **case} for case in cases] + [native]
        self.write({'unit': expected}, 'godot-game', ci=ci)
        original = configuration.load(self.root)
        before = godot_ci.plan(None, original)
        self.write({'unit': [{**shared, 'cases': cases}, native]}, 'godot-game', ci=ci)
        compact = configuration.load(self.root)
        after = godot_ci.plan(None, compact)
        self.assertEqual(compact['tests']['unit'], expected)
        self.assertEqual(after, before)
        self.assertEqual([godot_ci.case_id('unit', entry) for entry in compact['tests']['unit']],
                         [godot_ci.case_id('unit', entry) for entry in expected])
        requirements = after['requirements']
        selections = [set(entry.get('cases', [])) for entry in requirements]
        self.assertEqual(set().union(*selections), set(after['written_case_inventory']))
        self.assertEqual(sum(map(len, selections)), len(after['written_case_inventory']))
        self.assertEqual(len(after['godot_unit_inventory']), 2)

    def test_grouped_source_keeps_its_own_raw_hash_in_generated_make(self):
        group = self.group()
        shared = {key: value for key, value in group.items() if key != 'cases'}
        self.write({'unit': [{**shared, **case} for case in group['cases']]})
        (self.root / 'mise.toml').write_text('[tools]\npython="3.13.11"\n')
        expanded_make = generate.entrypoint_files(self.root, 'a' * 40)['mise.toml']
        body = self.write({'unit': [group]})
        grouped_make = generate.entrypoint_files(self.root, 'a' * 40)['mise.toml']
        self.assertNotEqual(grouped_make, expanded_make)
        self.assertIn('cookbook_sha256 = "' + hashlib.sha256(body.encode()).hexdigest() + '"', grouped_make)
        self.assertIn('cookbook_ref = "' + 'a' * 40 + '"', grouped_make)

    def test_empty_oversize_recursive_unknown_or_ambiguous_groups_are_refused(self):
        group = self.group()
        mutations = [
            {'cases': []}, {'cases': [group['cases'][0]] * 257}, {'cases': 'tests/first.py'},
            {'cases': [True]}, {'cases': [{'cases': group['cases']}]},
            {'cases': [{'path': 'tests/first.py', 'unknown': 'ignored'}]},
            {'cases': [{'path': 'tests/first.py', 'timeout': 7}]},
            {'cases': [{'path': 'tests/first.py', 'runner': 'python'}]},
            {'cases': [{'path': 'tests/first.py', 'cwd': 'tests'}]},
            {'cases': [{'path': 'tests/first.py', 'policy': {}}], 'policy': {}},
            {'path': 'tests/first.py'}, {'args': ['--shared']}, {'unknown': True},
        ]
        for changes in mutations:
            with self.subTest(changes=changes):
                self.write({'unit': [{**group, **changes}]})
                with self.assertRaises(ValueError):
                    configuration.load(self.root)

    def test_case_limit_accepts_the_complete_boundary_inventory(self):
        cases = [{'path': 'tests/first.py', 'name': 'test-case-' + str(index), 'args': [str(index)]}
                 for index in range(256)]
        self.write({'unit': [{'runner': 'python', 'timeout': 7, 'cases': cases}]})
        actual = configuration.load(self.root)['tests']['unit']
        self.assertEqual(actual, [{'runner': 'python', 'timeout': 7, **case} for case in cases])

    def test_expanded_cases_still_require_owned_sources_and_valid_execution_policy(self):
        for case in [{'path': '../outside.py'}, {'path': 'tests/missing.py'},
                     {'path': 'runtime.py'}, {'path': 'tests/first.py', 'args': 'shell command'}]:
            with self.subTest(case=case):
                (self.root / 'runtime.py').write_text('assert True\n')
                self.write({'unit': [{'runner': 'python', 'cases': [case]}]})
                with self.assertRaises(ValueError):
                    configuration.load(self.root)
        for changes in [{'runner': 'python; echo unsafe'}, {'timeout': 0}, {'timeout': True},
                        {'cwd': '../outside'}, {'policy': {'deferred_autoloads': True}},
                        {'python_environment': '../outside'}]:
            with self.subTest(changes=changes):
                self.write({'unit': [{**self.group(), **changes}]})
                with self.assertRaises(ValueError):
                    configuration.load(self.root)

    def test_common_nested_policy_data_does_not_alias_between_cases_or_mutate_source(self):
        group = {'runner': 'godot-check', 'policy': {'deferred_autoloads': True},
                 'cases': [{'path': 'app/tests/first.gd'}, {'path': 'app/tests/second.gd'}]}
        source = {'unit': [group]}
        original = deepcopy(source)
        expanded = configuration.expand_tests(source)
        self.assertEqual(source, original)
        expanded['unit'][0]['policy']['deferred_autoloads'] = False
        self.assertTrue(expanded['unit'][1]['policy']['deferred_autoloads'])
        self.assertEqual(source, original)


if __name__ == '__main__':
    unittest.main()
