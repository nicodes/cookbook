from source_fixture import SDK_ROOT
"""Recipe defaults retain exact worker coverage and owned diagnostic admission."""
import copy
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TOOLS = SDK_ROOT
sys.path.insert(0, str(TOOLS / 'cookbook'))
import generate
import godot_ci
import orchestration

spec = importlib.util.spec_from_file_location('recipe_default_diagnostics', TOOLS / 'helpers/native-diagnostics.py')
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)


class GateRunnerDefaults(unittest.TestCase):
    def config(self):
        return {'version': 1, 'recipe': 'godot-game',
                'tests': {'unit': [{'name': 'test-native', 'runner': 'godot-check',
                                    'path': 'app/tests/native.gd', 'cwd': 'app', 'timeout': 180}],
                          'integration': [{'name': 'test-backup', 'runner': 'python',
                                           'path': 'tests/backup.py', 'timeout': 600}]},
                'node': {'packages': [{'directory': 'tests/browser', 'manager': 'npm'}]},
                'ci': {'runner': 'blacksmith-4vcpu-ubuntu-2404', 'timeout_minutes': 45,
                       'godot': {'unit_shards': 1, 'unit_workers': 4, 'build_timeout_minutes': 25,
                                 'unit_timeout_minutes': 20, 'host_timeout_minutes': 45,
                                 'gates': [
                                     {'name': 'backup-fixture', 'runner': 'blacksmith-4vcpu-ubuntu-2404',
                                      'timeout_minutes': 20, 'suite': 'integration', 'tests': ['test-backup']},
                                     {'name': 'browser-capability', 'kind': 'capability',
                                      'runner': 'blacksmith-4vcpu-ubuntu-2204', 'timeout_minutes': 8,
                                      'setup': 'browser', 'packages': ['tests/browser'],
                                      'entry': {'runner': 'node', 'path': 'tests/browser/capability.mjs', 'timeout': 120}},
                                     {'name': 'production', 'kind': 'production',
                                      'runner': 'blacksmith-4vcpu-ubuntu-2204', 'timeout_minutes': 15,
                                      'setup': 'browser', 'packages': ['tests/browser'],
                                      'entry': {'runner': 'node', 'path': 'tests/browser/public.mjs', 'timeout': 900}}
                                 ]}}}

    def test_inherited_runner_preserves_complete_plan_and_distinct_hosts(self):
        explicit = self.config()
        inherited = copy.deepcopy(explicit)
        del inherited['ci']['godot']['gates'][0]['runner']
        before = godot_ci.plan(None, explicit)
        self.assertEqual(godot_ci.plan(None, inherited), before)
        self.assertEqual(orchestration.contract(inherited), orchestration.contract(explicit))
        requirements = {entry['id']: entry for entry in before['requirements']}
        self.assertEqual(requirements['gate-backup-fixture']['runner'], 'blacksmith-4vcpu-ubuntu-2404')
        self.assertEqual(requirements['gate-backup-fixture']['timeout_minutes'], 20)
        self.assertEqual(requirements['gate-browser-capability']['runner'], 'blacksmith-4vcpu-ubuntu-2204')
        self.assertEqual(requirements['gate-browser-capability']['timeout_minutes'], 8)
        production = before['production']['include'][0]
        self.assertEqual((production['runner'], production['timeout_minutes']), ('blacksmith-4vcpu-ubuntu-2204', 15))
        self.assertEqual([entry['timeout_minutes'] for entry in before['units']['include']], [20])
        self.assertEqual(requirements['host']['timeout_minutes'], 45)
        self.assertIn('runner', explicit['ci']['godot']['gates'][0], 'planning must not mutate source data')
        self.assertNotIn('runner', inherited['ci']['godot']['gates'][0])

    def test_omitted_parent_runner_uses_the_existing_host_default(self):
        explicit = self.config()
        explicit['ci']['runner'] = 'ubuntu-24.04'
        explicit['ci']['godot']['gates'][0]['runner'] = 'ubuntu-24.04'
        inherited = copy.deepcopy(explicit)
        del inherited['ci']['runner']
        del inherited['ci']['godot']['gates'][0]['runner']
        self.assertEqual(godot_ci.plan(None, inherited), godot_ci.plan(None, explicit))

    def test_invalid_parent_or_gate_runner_and_missing_deadline_refuse(self):
        for value in ('', 'windows-2025', 'ubuntu-latest', None, False, [], {}):
            for parent in (False, True):
                config = self.config()
                if parent:
                    config['ci']['runner'] = value
                    del config['ci']['godot']['gates'][0]['runner']
                else:
                    config['ci']['godot']['gates'][0]['runner'] = value
                with self.subTest(value=value, parent=parent), self.assertRaises(ValueError):
                    godot_ci.plan(None, config)
        for value in (None, False, 0, 121, '20'):
            config = self.config()
            config['ci']['godot']['gates'][0]['timeout_minutes'] = value
            with self.subTest(deadline=value), self.assertRaises(ValueError):
                godot_ci.plan(None, config)
        config = self.config()
        del config['ci']['godot']['gates'][0]['timeout_minutes']
        with self.assertRaisesRegex(ValueError, 'invalid native auxiliary gate'):
            godot_ci.plan(None, config)

    def test_generated_callers_preserve_effective_topology_after_runner_elision(self):
        source = '''version=1
recipe="godot-game"
[[tests.unit]]
runner="godot-check"
path="app/tests/native.gd"
cwd="app"
timeout=180
[[tests.integration]]
name="test-backup"
runner="python"
path="tests/backup.py"
timeout=600
[ci]
runner="blacksmith-4vcpu-ubuntu-2404"
timeout_minutes=45
[ci.godot]
unit_shards=1
unit_workers=4
build_timeout_minutes=25
unit_timeout_minutes=20
host_timeout_minutes=45
gates=[{name="backup-fixture",runner="blacksmith-4vcpu-ubuntu-2404",timeout_minutes=20,suite="integration",tests=["test-backup"]}]
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('app/tests/native.gd', 'tests/backup.py'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('# Native assertion fixture\n')
            path = root / 'cookbook.toml'
            path.write_text(source)
            explicit = generate.workflow_files(root, 'a' * 40)
            from configuration import load
            explicit_plan = godot_ci.plan(root, load(root))
            path.write_text(source.replace('name="backup-fixture",runner="blacksmith-4vcpu-ubuntu-2404",', 'name="backup-fixture",'))
            self.assertEqual(generate.workflow_files(root, 'a' * 40), explicit)
            self.assertEqual(godot_ci.plan(root, load(root)), explicit_plan)


class DiagnosticProjectDefaults(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        project = self.root / 'app/project.godot'
        project.parent.mkdir()
        project.write_text('config_version=5\n')
        subprocess.run(['git', '-C', str(self.root), 'add', 'app/project.godot'], check=True)
        report = self.root / 'app/build/browser-report'
        report.mkdir(parents=True)
        (report / 'failure.json').write_text('{"passed":false}')

    def config(self):
        return {'recipe': 'godot-game', 'components': {'godot': {}},
                'artifacts': {'evidence_paths': ['app/build/browser-report'],
                              'evidence_retention_days': {'app/build/browser-report': 7}}}

    def test_default_project_matches_explicit_app_with_actual_checkout_and_report(self):
        implicit = self.config()
        explicit = copy.deepcopy(implicit)
        explicit['components']['godot']['project'] = 'app'
        expected = [{'days': 7, 'paths': ['app/build/browser-report']}]
        self.assertEqual(diagnostics.declaration(self.root, explicit, inspect=True, status='failure'), expected)
        self.assertEqual(diagnostics.declaration(self.root, implicit, inspect=True, status='failure'), expected)
        del implicit['components']
        self.assertEqual(diagnostics.declaration(self.root, implicit, inspect=True, status='failure'), expected)

    def test_real_default_project_reader_preserves_upload_admission(self):
        source = self.root / 'cookbook.toml'
        source.write_text('recipe="godot-game"\n[artifacts]\n'
                          'evidence_paths=["app/build/browser-report"]\n'
                          'evidence_retention_days={"app/build/browser-report"=7}\n')
        output = self.root / 'outputs'
        result = subprocess.run([sys.executable, str(TOOLS / 'helpers/native-diagnostics.py'), '--root', str(self.root),
                                 '--inspect', '--status', 'failure', '--expected-sha256', hashlib.sha256(source.read_bytes()).hexdigest()],
                                env=dict(os.environ, GITHUB_OUTPUT=str(output)), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('app/build/browser-report', output.read_text())
        self.assertIn('retention-days-1=7', output.read_text())
        self.assertNotIn('app/project.godot', output.read_text())

    def test_default_preserves_owned_project_tracked_source_and_link_refusals(self):
        for value in ('other', '..', '', None, False, []):
            config = self.config()
            config['components']['godot']['project'] = value
            with self.subTest(project=value), self.assertRaises(ValueError):
                diagnostics.declaration(self.root, config)
        tracked = self.root / 'app/build/browser-report/private.py'
        tracked.write_text('secret_source=True\n')
        subprocess.run(['git', '-C', str(self.root), 'add', 'app/build/browser-report/private.py'], check=True)
        with self.assertRaisesRegex(ValueError, 'tracked source'):
            diagnostics.declaration(self.root, self.config(), inspect=True)
        subprocess.run(['git', '-C', str(self.root), 'rm', '-q', '--cached', 'app/build/browser-report/private.py'], check=True)
        tracked.unlink()
        (self.root / 'app/build/browser-report/link').symlink_to(self.root / 'app/project.godot')
        with self.assertRaisesRegex(ValueError, 'unlinked'):
            diagnostics.declaration(self.root, self.config(), inspect=True)
        (self.root / 'app/project.godot').unlink()
        with self.assertRaisesRegex(ValueError, 'owned Godot project'):
            diagnostics.declaration(self.root, self.config())


if __name__ == '__main__':
    unittest.main()
