from source_fixture import SDK_ROOT
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml
import generate


REVISION = 'a' * 40


class TransitionSourceCI(unittest.TestCase):
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
        return files, yaml.safe_load(files['.github/workflows/ci.yml'])

    def action(self):
        path = SDK_ROOT / 'ci-result/action.yml'
        return yaml.safe_load(path.read_bytes())

    def result(self, event='pull_request', values=None, raw=None):
        if values is None:
            values = {'validate': 'success', 'complete': 'success'}
        needs = {key: {'result': value, 'outputs': {}} for key, value in values.items()}
        action = self.action()
        command = action['runs']['steps'][0]['run']
        environment = {**os.environ, 'CI_EVENT_NAME': event,
                       'CI_NEEDS_JSON': json.dumps(needs) if raw is None else raw,
                       'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', '')}
        with tempfile.TemporaryDirectory(prefix='source CI result ') as directory:
            return subprocess.run(['bash', '-euc', command], cwd=directory, env=environment,
                                  capture_output=True, text=True, timeout=15)

    def test_all_four_families_keep_fast_and_one_complete_PR_route(self):
        for family in ('godot-addon', 'rust-cli', 'godot-game', 'react-native'):
            with self.subTest(family=family):
                files, caller = self.caller(family)
                self.assertNotIn('run:', files['.github/workflows/ci.yml'])
                self.assertEqual(set(caller['on']), {'pull_request', 'push', 'workflow_dispatch'})
                self.assertEqual(caller['on']['push'], {'branches': ['main']})
                self.assertEqual(set(caller['on']['workflow_dispatch']['inputs']), {'source', 'cookbook'})
                jobs = caller['jobs']
                first, complete = jobs['validate'], jobs['complete']
                self.assertNotIn('name', first)
                self.assertEqual(first['uses'], 'nicodes/cookbook/.github/workflows/cookbook-ci.yml@' + REVISION)
                self.assertEqual(first['uses'], complete['uses'])
                self.assertIs(first['with']['merge-gate'], True)
                self.assertIs(complete['with']['merge-gate'], False)
                self.assertEqual(complete['if'], "github.event_name == 'pull_request'")
                self.assertEqual({k: v for k, v in first['with'].items() if k != 'merge-gate'},
                                 {k: v for k, v in complete['with'].items() if k != 'merge-gate'})
                self.assertEqual(first['with'].get('recipe', 'godot-addon'), family)
                self.assertEqual(first['with']['source'], '${{ inputs.source || github.sha }}')
                self.assertEqual(first['permissions'], complete['permissions'])
                self.assertEqual(set(first['permissions'].values()), {'read'})

    def test_terminal_is_an_actual_exact_name_job_with_fail_closed_needs(self):
        _, caller = self.caller('rust-cli')
        job = caller['jobs']['full-validation']
        self.assertEqual(job['name'], 'Full validation')
        self.assertEqual(job['if'], 'always()')
        self.assertEqual(job['needs'], ['validate', 'complete'])
        self.assertEqual(job['permissions'], {})
        self.assertEqual(job['timeout-minutes'], 1)
        self.assertEqual(len(job['steps']), 1)
        step = job['steps'][0]
        self.assertEqual(step['uses'], 'nicodes/cookbook/ci-result@' + REVISION)
        self.assertEqual(step['with'], {'event-name': '${{ github.event_name }}', 'needs': '${{ toJSON(needs) }}'})
        self.assertEqual(step['timeout-minutes'], 1)
        action = self.action()
        self.assertEqual(set(action['inputs']), {'event-name', 'needs'})
        self.assertTrue(all(value['required'] is True for value in action['inputs'].values()))
        self.assertEqual(action['runs']['using'], 'composite')
        self.assertEqual(action['runs']['steps'][0]['env'],
                         {'CI_EVENT_NAME': '${{ inputs.event-name }}', 'CI_NEEDS_JSON': '${{ inputs.needs }}'})

    def test_existing_full_reuse_and_native_routing_are_retained(self):
        document = yaml.safe_load((SDK_ROOT / '.github/workflows/cookbook-ci.yml').read_bytes())
        jobs = document['jobs']
        self.assertEqual(jobs['fast']['name'], 'Fast source checks')
        self.assertEqual(jobs['fast']['if'], "github.event_name == 'pull_request' && inputs.merge-gate")
        for job, recipe in [('native', 'rust-cli'), ('godot', 'godot-game')]:
            self.assertEqual(jobs[job]['if'], "(github.event_name != 'pull_request' || !inputs.merge-gate) && inputs.recipe == '" + recipe + "'")
        self.assertIn('inputs.recipe', jobs['full']['if'])
        self.assertIn('["godot-addon","react-native"]', jobs['full']['if'])
        self.assertEqual(jobs['full']['name'], 'Full source checks')
        host = yaml.safe_load((SDK_ROOT / '.github/workflows/cookbook-validation.yml').read_bytes())
        self.assertEqual(host['jobs']['validate']['name'], 'Make ${{ inputs.mode }}')
        # On non-PR events only validate is eligible, and that exact existing
        # reusable selects full; the second complete call is structurally absent.
        _, caller = self.caller('godot-addon')
        self.assertNotIn('if', caller['jobs']['validate'])
        self.assertEqual(caller['jobs']['complete']['if'], "github.event_name == 'pull_request'")

    def test_dependency_security_is_an_actual_required_dependency(self):
        text = generate.completion_jobs(REVISION, '      recipe: rust-cli\n', True)
        job = yaml.safe_load(text)['full-validation']
        self.assertEqual(job['needs'], ['validate', 'complete', 'dependency-security'])
        result = self.result(values={'validate':'success','complete':'success','dependency-security':'success'})
        self.assertEqual(result.returncode, 0, result.stderr)
        for outcome in ('failure','skipped','cancelled','neutral'):
            with self.subTest(outcome=outcome):
                result = self.result(values={'validate':'success','complete':'success','dependency-security':outcome})
                self.assertNotEqual(result.returncode, 0)

    def test_actual_guard_accepts_only_complete_PR_and_non_PR_routes(self):
        result = self.result()
        self.assertEqual(result.returncode, 0, result.stderr)
        for event in ('push','workflow_dispatch'):
            with self.subTest(event=event):
                result = self.result(event, {'validate':'success','complete':'skipped'})
                self.assertEqual(result.returncode, 0, result.stderr)
                duplicate = self.result(event, {'validate':'success','complete':'success'})
                self.assertNotEqual(duplicate.returncode, 0)

    def test_actual_guard_refuses_every_non_success_required_native_route(self):
        for name in ('validate','complete'):
            for outcome in ('failure','skipped','cancelled','timed_out','neutral','',None,True):
                with self.subTest(job=name,outcome=outcome):
                    values={'validate':'success','complete':'success'}
                    values[name]=outcome
                    self.assertNotEqual(self.result(values=values).returncode, 0)
        self.assertNotEqual(self.result('push', {'validate':'skipped','complete':'skipped'}).returncode, 0)

    def test_actual_guard_refuses_unknown_events_and_incomplete_or_forged_shapes(self):
        for event in ('pull_request_target','workflow_call','merge_group','', 'push; exit 0'):
            with self.subTest(event=event):
                self.assertNotEqual(self.result(event).returncode, 0)
        valid={'validate':{'result':'success','outputs':{}},'complete':{'result':'success','outputs':{}}}
        bad=[{}, {'validate':valid['validate']}, {**valid,'unexpected':valid['complete']},
             {**valid,'complete':{'result':'success'}},
             {**valid,'complete':{**valid['complete'],'unknown':True}},
             {**valid,'complete':{'result':'success','outputs':[]}}, [], None]
        for value in bad:
            with self.subTest(value=value):
                self.assertNotEqual(self.result(raw=json.dumps(value)).returncode, 0)
        duplicate='{"validate":{"result":"failure","result":"success","outputs":{}},"complete":{"result":"success","outputs":{}}}'
        self.assertNotEqual(self.result(raw=duplicate).returncode, 0)
        self.assertNotEqual(self.result(raw=' ' * 32769).returncode, 0)


if __name__ == '__main__':
    unittest.main()
