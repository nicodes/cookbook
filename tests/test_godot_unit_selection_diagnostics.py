from source_fixture import SDK_ROOT
"""Native shard selection survives real failing and timed-out Mise children."""
from contextlib import chdir, ExitStack
import json
import os
from pathlib import Path
import shutil
import shlex
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import ci
import configuration
import godot_ci as c
import godot_game as game


@unittest.skipUnless(shutil.which('mise') and os.name != 'nt', 'native process-group controls require a POSIX Mise host')
class UnitSelectionDiagnostics(unittest.TestCase):
    source = 'a' * 40
    tools = 'b' * 40

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / '.artifacts').mkdir(mode=0o700)
        (self.root / '.artifacts/godot-imported-build.json').write_text('{"artifacts":{}}')
        (self.root / 'app/tests').mkdir(parents=True)
        for name in ('alpha', 'beta', 'gamma'):
            (self.root / f'app/tests/{name}.gd').write_text('extends Node\n')
        self.config = {'recipe': 'godot-game', 'tests': {'unit': [
            {'name': 'test-' + name, 'runner': 'godot-check', 'path': f'app/tests/{name}.gd'}
            for name in ('alpha', 'beta', 'gamma')]},
            'ci': {'runner': 'ubuntu-24.04', 'godot': {'unit_shards': 1, 'unit_workers': 4}}}
        (self.root / 'mise.toml').write_text('[settings]\ntask.run_auto_install=false\n'
            '[tasks."test:unit"]\nrun=' + json.dumps(shlex.quote(sys.executable) + ' native_case.py') + '\n')
        (self.root / 'native_case.py').write_text('''import json,os,sys,time
from pathlib import Path
record=json.loads(Path('.artifacts/godot-test-selection.json').read_text())
expected=json.loads(Path('expected.json').read_text())
assert record==dict(expected,status='running'),(record,expected)
Path('observed.json').write_text(json.dumps({'selection':record,'group':os.getpgrp()}))
if os.environ['SDK_SELECTION_CASE_MODE']=='failure':sys.exit(7)
if os.environ['SDK_SELECTION_CASE_MODE']=='timeout':time.sleep(4)
''')
        self.verify = ci.verification()
        self.environment = {'GITHUB_REPOSITORY': 'fixture/game', 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2'}
        self.path = self.root / '.artifacts/godot-test-selection.json'

    def expected(self, shard='1/1', attempt='2'):
        names = ['test-alpha', 'test-beta', 'test-gamma']
        return {'source_commit': self.source, 'run_id': '123', 'attempt': attempt, 'shard': shard,
                'workers': 4, 'inventory': names, 'selected': c.partition(names, shard)}

    def invoke(self, mode='passed', entry='unit-1-1', timeout=False, **environment):
        values = dict(self.environment, SDK_SELECTION_CASE_MODE=mode, **environment)
        shard = entry.removeprefix('unit-').replace('-', '/')
        (self.root / 'expected.json').write_text(json.dumps(self.expected(shard, values['GITHUB_RUN_ATTEMPT'])))
        real_run = subprocess.run
        def bounded(argv, *args, **kwargs):
            if str(c.COOKBOOK / 'cookbook/dispatch.py') in argv:
                # This existing orchestration fixture substitutes its native
                # child only; the real Mise process executes native_case.py.
                argv = ['mise', 'run', 'test:unit']
                if timeout:
                    kwargs.update(timeout=0.5, start_new_session=True)
            return real_run(argv, *args, **kwargs)
        with chdir(self.root), ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, values))
            stack.enter_context(patch.object(configuration, 'load', return_value=self.config))
            stack.enter_context(patch.object(game, 'configure_environment', return_value={}))
            stack.enter_context(patch.object(game, 'verification_scope', return_value={}))
            stack.enter_context(patch.object(self.verify, 'artifacts', return_value={}))
            stack.enter_context(patch.object(ci, 'verification', return_value=self.verify))
            stack.enter_context(patch.object(c, 'collect_partial', return_value={'complete': False}))
            exporter = stack.enter_context(patch.object(c, 'export_packet'))
            stack.enter_context(patch.object(subprocess, 'run', side_effect=bounded))
            try:
                c.execute_entry(self.root, self.source, self.tools, entry)
            except BaseException:
                exporter.assert_not_called()
                raise
            exporter.assert_called_once()

    def test_successful_real_child_observes_running_and_existing_schema_accepts_final_selection(self):
        self.invoke()
        record = json.loads(self.path.read_text())
        self.assertEqual(record, dict(self.expected(), status='passed'))
        self.assertEqual(json.loads((self.root / 'observed.json').read_text())['selection']['status'], 'running')
        self.assertEqual(c.verify_selections(self.root / '.artifacts', self.source,
            self.expected()['inventory'], 1, '123', '2')['status'], 'passed')

    def test_real_failure_replaces_prior_pass_and_cannot_be_accepted(self):
        self.invoke()
        with self.assertRaises(subprocess.CalledProcessError):
            self.invoke('failure', GITHUB_RUN_ATTEMPT='3')
        self.assertEqual(json.loads(self.path.read_text()), dict(self.expected(attempt='3'), status='failed'))
        observed = json.loads((self.root / 'observed.json').read_text())['selection']
        self.assertEqual(observed, dict(self.expected(attempt='3'), status='running'))
        with self.assertRaises(ValueError):
            c.verify_selections(self.root / '.artifacts', self.source, self.expected()['inventory'], 1, '123', '3')

    def test_real_timeout_keeps_current_failed_selection_and_no_success_export(self):
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.invoke('timeout', timeout=True)
            self.assertEqual(json.loads(self.path.read_text()), dict(self.expected(), status='failed'))
            self.assertEqual(json.loads((self.root / 'observed.json').read_text())['selection']['status'], 'running')
        finally:
            observed = self.root / 'observed.json'
            if observed.exists():
                try:
                    os.killpg(json.loads(observed.read_text())['group'], signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_exact_shard_selection_contains_full_inventory_and_four_workers(self):
        self.config['ci']['godot']['unit_shards'] = 2
        self.invoke(entry='unit-1-2')
        self.assertEqual(json.loads(self.path.read_text()), dict(self.expected('1/2'), status='passed'))
        self.assertEqual(json.loads(self.path.read_text())['selected'], ['test-alpha', 'test-gamma'])

    def test_invalid_execution_source_workers_and_shard_refuse_before_native_child(self):
        for key, value in [('GITHUB_RUN_ID', '0'), ('GITHUB_RUN_ATTEMPT', '0'), ('GITHUB_REPOSITORY', 'invalid')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.invoke(**{key: value})
            self.assertFalse(self.path.exists())
        for workers, shards in [(3, 1), (4, 0), (4, 4)]:
            self.config['ci']['godot'].update(unit_workers=workers, unit_shards=shards)
            with self.subTest(workers=workers, shards=shards), self.assertRaises(ValueError):
                self.invoke()
            self.assertFalse(self.path.exists())
        self.config['ci']['godot'].update(unit_workers=4, unit_shards=1)
        self.source = 'main'
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertFalse((self.root / 'observed.json').exists())

    def test_protected_writer_refuses_linked_target_and_parent_before_native_child(self):
        target = self.root / 'private.json'
        target.write_text('preserve fixture source')
        self.path.symlink_to(target)
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertEqual(target.read_text(), 'preserve fixture source')
        self.path.unlink()
        (self.root / '.artifacts').rename(self.root / 'saved-artifacts')
        (self.root / '.artifacts').symlink_to(self.root / 'saved-artifacts', target_is_directory=True)
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertFalse((self.root / 'observed.json').exists())


if __name__ == '__main__':
    unittest.main()
