from source_fixture import SDK_ROOT, parser_root
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from unittest.mock import patch

import source_policy
import generate


REVISION = 'a' * 40


def read_yaml(body):
    parser = source_policy.interpreter(parser_root())
    if isinstance(body, bytes):
        body = body.decode('utf-8')
    return json.loads(subprocess.check_output([parser, '-I', '-B', '-c',
        'import json,sys,yaml;print(json.dumps(yaml.safe_load(sys.stdin.read())))'],
        input=body, text=True, timeout=30))


class TransitionSourceCI(unittest.TestCase):
    def test_yaml_uses_the_existing_locked_parser_instead_of_the_test_interpreter(self):
        prepared = parser_root()
        for body in ('version: 1\n', b'version: 1\n'):
            with self.subTest(body=body), patch(__name__ + '.parser_root', return_value=prepared) as selected, \
                    patch.object(source_policy, 'interpreter', return_value='/prepared-parser/python') as parser, \
                    patch.object(subprocess, 'check_output', return_value='{"version": 1}') as child:
                self.assertEqual(read_yaml(body), {'version': 1})
                selected.assert_called_once_with()
                parser.assert_called_once_with(prepared)
                command = child.call_args.args[0]
                self.assertEqual(command[:4], ['/prepared-parser/python', '-I', '-B', '-c'])
                self.assertIn('yaml.safe_load', command[4])
                self.assertEqual(child.call_args.kwargs, {'input': 'version: 1\n', 'text': True, 'timeout': 30})

    def caller(self, family):
        temporary = tempfile.TemporaryDirectory(prefix='canonical source CI ')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / 'cookbook.toml').write_text(
            'version=1\nrecipe="' + family + '"\n'
            '[[tests.unit]]\nrunner="python"\npath="tests/native.py"\n')
        (root / 'tests').mkdir()
        (root / 'tests/native.py').write_text('raise SystemExit("source-only fixture must not execute")\n')
        files = generate.workflow_files(root, REVISION)
        return files, read_yaml(files['.github/workflows/build-and-test.yml'])

    def action(self):
        path = SDK_ROOT / 'ci-result/action.yml'
        return read_yaml(path.read_bytes())

    def result(self, event='pull_request', values=None, raw=None):
        if values is None:
            values = {'validate': 'success'}
        needs = {key: {'result': value, 'outputs': {}} for key, value in values.items()}
        action = self.action()
        command = action['runs']['steps'][0]['run']
        environment = {**os.environ, 'CI_EVENT_NAME': event,
                       'CI_NEEDS_JSON': json.dumps(needs) if raw is None else raw,
                       'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', '')}
        with tempfile.TemporaryDirectory(prefix='source CI result ') as directory:
            return subprocess.run(['bash', '-euc', command], cwd=directory, env=environment,
                                  capture_output=True, text=True, timeout=15)

    def test_each_recipe_uses_one_full_route_for_PR_and_main(self):
        workflows = {'godot-addon': 'build-and-test-host.yml', 'react-native': 'build-and-test-host.yml',
                     'rust-cli': 'build-and-test-native.yml', 'godot-game': 'build-and-test-godot.yml'}
        for family, workflow in workflows.items():
            with self.subTest(family=family):
                files, caller = self.caller(family)
                self.assertEqual(set(caller['on']), {'pull_request', 'push', 'workflow_dispatch', 'merge_group'})
                self.assertEqual(caller['on']['push'], {'branches': ['main']})
                single_host = family in ('godot-addon', 'react-native')
                self.assertEqual(set(caller['jobs']), {'validate'} if single_host else {'validate', 'build-test'})
                route = caller['jobs']['validate']
                self.assertEqual(route['uses'], 'nicodes/cookbook/.github/workflows/' + workflow + '@' + REVISION)
                self.assertNotIn('if', route)
                self.assertNotIn('merge-gate', route['with'])
                self.assertNotIn('recipe', route['with'])
                if single_host:
                    self.assertEqual(route['name'], 'Build and test')
                    self.assertEqual(route['with']['mode'], 'full')
                self.assertEqual(route['with']['source'], '${{ inputs.source || github.sha }}')
                self.assertEqual(set(route['permissions'].values()), {'read'})

    def test_terminal_result_requires_the_full_route_even_when_it_is_skipped(self):
        _, caller = self.caller('rust-cli')
        job = caller['jobs']['build-test']
        self.assertEqual(job['name'], 'Build and test')
        self.assertEqual(job['if'], 'always()')
        self.assertEqual(job['needs'], ['validate'])
        self.assertEqual(job['permissions'], {})
        self.assertEqual(job['timeout-minutes'], 1)
        self.assertEqual(len(job['steps']), 1)
        step = job['steps'][0]
        self.assertEqual(step['uses'], 'nicodes/cookbook/ci-result@' + REVISION)
        self.assertEqual(step['with'], {'event-name': '${{ github.event_name }}', 'needs': '${{ toJSON(needs) }}'})
        self.assertEqual(step['timeout-minutes'], 1)

    def test_dynamic_merge_router_cannot_select_fast_checks(self):
        document = read_yaml((SDK_ROOT / '.github/workflows/build-and-test-recipe.yml').read_bytes())
        self.assertNotIn('merge-gate', document['on']['workflow_call']['inputs'])
        self.assertEqual(set(document['jobs']), {'full', 'native', 'godot', 'unsupported'})
        jobs = document['jobs']
        self.assertEqual(jobs['full']['with']['mode'], 'full')
        for job, recipe in [('native', 'rust-cli'), ('godot', 'godot-game')]:
            self.assertEqual(jobs[job]['if'], "inputs.recipe == '" + recipe + "'")
        self.assertIn('["godot-addon","react-native"]', jobs['full']['if'])
        self.assertIn('!contains(', jobs['unsupported']['if'])
        self.assertIn('exit 1', jobs['unsupported']['steps'][0]['run'])

    def test_guard_accepts_success_on_all_canonical_events(self):
        for event in ('pull_request', 'push', 'workflow_dispatch', 'merge_group'):
            with self.subTest(event=event):
                result = self.result(event)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_guard_refuses_non_success_and_retired_duplicate_routes(self):
        for outcome in ('failure', 'skipped', 'cancelled', 'timed_out', 'neutral', '', None, True):
            with self.subTest(outcome=outcome):
                self.assertNotEqual(self.result(values={'validate': outcome}).returncode, 0)
        self.assertNotEqual(self.result(values={'validate': 'success', 'complete': 'success'}).returncode, 0)

    def test_guard_refuses_unknown_events_and_forged_shapes(self):
        for event in ('pull_request_target', 'workflow_call', '', 'push; exit 0'):
            with self.subTest(event=event):
                self.assertNotEqual(self.result(event).returncode, 0)
        valid = {'validate': {'result': 'success', 'outputs': {}}}
        bad = [{}, {**valid, 'unexpected': valid['validate']},
               {'validate': {'result': 'success'}},
               {'validate': {**valid['validate'], 'unknown': True}},
               {'validate': {'result': 'success', 'outputs': []}}, [], None]
        for value in bad:
            with self.subTest(value=value):
                self.assertNotEqual(self.result(raw=json.dumps(value)).returncode, 0)
        duplicate = '{"validate":{"result":"failure","result":"success","outputs":{}}}'
        self.assertNotEqual(self.result(raw=duplicate).returncode, 0)
        self.assertNotEqual(self.result(raw=' ' * 32769).returncode, 0)


if __name__ == '__main__':
    unittest.main()
