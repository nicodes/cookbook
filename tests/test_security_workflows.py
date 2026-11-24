from source_fixture import SDK_ROOT
"""The daily Security workflow of one-job Rust CLI and Godot addon recipes, and its security-only phase."""
import importlib.util
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import test_single_job_rust_cli as cli

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import ci
import configuration
import rust_cli
import security_workflows

PIN = 'a' * 40
FILE = '.github/workflows/security.yml'
SOURCE, TOOLS = 'c' * 40, 'd' * 40

HEADER = """# Generated cookbook caller. Change recipe data and regenerate.
name: Security
run-name: "Security · ${{ github.event.schedule || github.event_name }}"
'on':
  schedule:
    - cron: "CRON"
  workflow_dispatch:
permissions: {}
concurrency:
  group: security-${{ github.ref }}
  cancel-in-progress: false
jobs:
  security:
    name: Security
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-24.04
    timeout-minutes: JOB
    permissions:
      contents: read
      issues: write
    steps:
      - name: Prepare
        timeout-minutes: 20
        uses: nicodes/cookbook/prepare@PIN
        with:
          cookbook-ref: PIN
          source: ${{ github.sha }}
          advisories: run
"""
BUILD = """      - name: Build
        timeout-minutes: 30
        uses: nicodes/cookbook/build@PIN
"""
TAIL = """      - name: Audit
        timeout-minutes: 30
        uses: nicodes/cookbook/security@PIN
      - name: Finish
        if: always()
        timeout-minutes: 5
        uses: nicodes/cookbook/finish@PIN
        with:
          status: ${{ job.status }}
      - name: Report failure
        if: failure()
        timeout-minutes: 5
        run: python3 "$TOOLS_ROOT/helpers/report-failure.py"
        env:
          GH_TOKEN: ${{ github.token }}
"""
RUST = (HEADER.replace('CRON', '51 4 * * *').replace('JOB', '60') + '          target: x86_64-unknown-linux-musl\n' + TAIL).replace('PIN', PIN)
RUST_BUILT = (HEADER.replace('CRON', '51 4 * * *').replace('JOB', '90') + '          target: x86_64-unknown-linux-musl\n'
              + BUILD + TAIL).replace('PIN', PIN)
ADDON = (HEADER.replace('CRON', '36 3 * * *').replace('JOB', '60') + TAIL).replace('PIN', PIN)


class Rust(cli.Source):
    def security(self):
        return self.files().get(FILE)

    def test_rust_cli_golden_runs_only_the_scans_after_prepare_with_advisories_on(self):
        self.assertEqual(self.security(), RUST)
        self.assertEqual(set(self.files()), {cli.CHECK, FILE})

    def test_declared_security_assertions_add_the_build_they_may_need(self):
        self.config['tests']['security'] = [{'runner': 'python', 'path': 'tests/test_behavior.py'}]
        self.assertEqual(self.security(), RUST_BUILT)

    def test_the_job_runs_on_the_linux_x86_64_platform_of_the_recipe_with_its_target(self):
        text = self.security()
        self.assertIn('    runs-on: ubuntu-24.04\n', text)
        self.assertIn('          target: x86_64-unknown-linux-musl\n', text)
        self.assertNotIn('matrix', text)
        self.config['package']['native_targets'][0]['runner'] = 'ubuntu-24.04'
        self.assertEqual(self.security(), text)

    def test_flag_absent_is_unchanged_and_has_no_security_workflow(self):
        self.config['ci'] = {}
        absent = self.files()
        self.config['ci'] = {'single_job': False}
        self.assertEqual(self.files(), absent)
        self.assertEqual(set(absent), {cli.CHECK})
        self.assertNotIn('nicodes/cookbook/security', absent[cli.CHECK])

    def test_the_build_and_test_caller_is_identical_with_and_without_the_security_workflow(self):
        self.assertEqual(self.files()[cli.CHECK], cli.GOLDEN)

    def test_one_workflow_one_job_with_a_visible_failure_report_and_least_privilege(self):
        text = self.security()
        self.assertEqual(re.findall(r'^  ([a-z-]+):$', text.split('\njobs:\n')[1], flags=re.MULTILINE), ['security'])
        self.assertIn('permissions: {}\n', text)
        self.assertIn('    permissions:\n      contents: read\n      issues: write\n    steps:', text)
        self.assertEqual(text.count('issues: write'), 1)
        self.assertTrue(text.endswith('      - name: Report failure\n        if: failure()\n        timeout-minutes: 5\n'
                                      '        run: python3 "$TOOLS_ROOT/helpers/report-failure.py"\n'
                                      '        env:\n          GH_TOKEN: ${{ github.token }}\n'))
        self.assertIn("'on':\n  schedule:\n    - cron: ", text)
        self.assertIn('\n  workflow_dispatch:\npermissions', text)
        self.assertNotIn('pull_request', text)


class Schedule(unittest.TestCase):
    def config(self, binary):
        return {'recipe': 'rust-cli', 'package': {'binary': binary}}

    def test_the_schedule_is_deterministic_daily_and_inside_the_window(self):
        for name in ['alpha', 'beta', 'x' * 40, '']:
            first = security_workflows.cron(self.config(name))
            self.assertEqual(first, security_workflows.cron(self.config(name)))
            minute, hour, *rest = first.split()
            self.assertEqual(rest, ['*', '*', '*'])
            self.assertIn(int(minute), security_workflows.MINUTES)
            self.assertIn(int(hour), security_workflows.HOURS)

    def test_projects_spread_over_the_window_and_keep_off_peak(self):
        slots = {security_workflows.cron(self.config('tool-' + str(number))) for number in range(40)}
        self.assertGreater(len(slots), 25)
        self.assertEqual({int(slot.split()[0]) for slot in slots} & {0}, set())
        self.assertTrue(all(2 <= int(slot.split()[1]) <= 5 for slot in slots))

    def test_known_slots_are_pinned(self):
        self.assertEqual(security_workflows.cron({'recipe': 'godot-addon', 'package': {'archive': 'dist/@owner_addon.zip'}}),
                         '36 3 * * *')

    def test_the_identity_is_recipe_data_not_the_checkout_location(self):
        one = security_workflows.identity({'recipe': 'godot-addon', 'package': {'archive': 'dist/a.zip'}})
        self.assertEqual(one, 'godot-addon:dist/a.zip')
        self.assertNotEqual(one, security_workflows.identity({'recipe': 'godot-addon', 'package': {'archive': 'dist/b.zip'}}))


class Addon(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'tests').mkdir()
        (self.root / 'tests/check.py').write_text('assert True\n')
        (self.root / 'package.json').write_text('{}\n')
        (self.root / 'package-lock.json').write_text('{}\n')
        (self.root / 'mise.toml').write_text('[tools]\npython="3.13.11"\nnode="22.0.0"\n')
        self.write('')
        spec = importlib.util.spec_from_file_location('cookbook_generate_security', SDK_ROOT / 'cookbook/generate.py')
        self.generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.generator)

    def write(self, extra, single=True):
        (self.root / 'cookbook.toml').write_text(
            'version=1\nrecipe="godot-addon"\n' + ('[ci]\nsingle_job=true\n' if single else '')
            + '[package]\narchive="dist/@owner_addon.zip"\n[[tests.unit]]\nrunner="python"\npath="tests/check.py"\n' + extra)

    def files(self):
        return self.generator.workflow_files(self.root, PIN)

    def test_addon_with_a_node_package_gets_the_golden_security_workflow(self):
        self.write('[[node.packages]]\ndirectory="."\n')
        files = self.files()
        self.assertEqual(files[FILE], ADDON)
        self.assertEqual(set(files), {'.github/workflows/build-and-test.yml', FILE})
        self.assertIn('runs-on: ubuntu-24.04\n', files[FILE])

    def test_addon_with_nothing_to_scan_gets_no_security_workflow(self):
        self.assertEqual(set(self.files()), {'.github/workflows/build-and-test.yml'})

    def test_flag_absent_is_unchanged(self):
        self.write('[[node.packages]]\ndirectory="."\n', single=False)
        self.assertNotIn(FILE, self.files())

    def test_addon_runner_follows_the_recipe_ci_runner(self):
        self.write('[[node.packages]]\ndirectory="."\n')
        path = self.root / 'cookbook.toml'
        path.write_text(path.read_text().replace('single_job=true\n', 'single_job=true\nrunner="fast-runner"\ntimeout_minutes=20\n'))
        text = self.files()[FILE]
        self.assertIn('    runs-on: fast-runner\n    timeout-minutes: 50\n', text)
        self.assertIn('      - name: Audit\n        timeout-minutes: 20\n', text)


class Selection(unittest.TestCase):
    def test_only_recipes_with_something_to_scan_and_no_operations_declaration_are_selected(self):
        rust = {'recipe': 'rust-cli'}
        self.assertTrue(security_workflows.selected(rust))
        self.assertEqual(security_workflows.scanners({**rust, 'security': {'trivy_paths': ['.']},
                                                      'node': {'packages': [{}]}}), ['cargo audit', 'trivy fs', 'npm audit'])
        self.assertFalse(security_workflows.selected({'recipe': 'godot-addon'}))
        self.assertTrue(security_workflows.selected({'recipe': 'godot-addon', 'node': {'packages': [{}]}}))
        self.assertFalse(security_workflows.selected({**rust, 'operations': {'definition': 'deploy/operations.toml'}}))
        for name in ['react-native', 'godot-game']:
            self.assertFalse(security_workflows.selected({'recipe': name, 'node': {'packages': [{}]}}))

    def test_a_workflow_needs_a_full_pin(self):
        with self.assertRaisesRegex(ValueError, 'full source pin'):
            security_workflows.workflow({'recipe': 'rust-cli'}, 'abc')


class Phase(cli.Source):
    """The security phase runs only the security group and only with advisories on."""

    def setUp(self):
        super().setUp()
        self.calls = []
        self.environment = patch.dict(os.environ, {'COOKBOOK_ADVISORIES': 'run', native_target(): 'x86_64-unknown-linux-musl'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for name in ['COOKBOOK_GODOT_ENTRY', 'COOKBOOK_GODOT_HOST', 'GITHUB_OUTPUT']:
            os.environ.pop(name, None)
        for target, options in [
                ('ci.load', {'return_value': self.config}), ('ci.identity', {}),
                ('ci.recipe.configure_environment', {}),
                ('native_ci.primary_environment', {}),
                ('ci.subprocess.run', {'side_effect': lambda command, **kwargs: self.calls.append((command[-1], kwargs))})]:
            patcher = patch(target, **options)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_phase(self, phase='security'):
        ci.run_single_job(self.root, 'full', phase, SOURCE, TOOLS)

    def test_security_dispatches_only_the_security_group_with_no_receipt_or_proof(self):
        with patch('ci.proof') as proof:
            self.run_phase()
        self.assertEqual([name for name, _ in self.calls], ['test:security'])
        self.assertEqual(self.calls[0][1]['cwd'], self.root)
        self.assertEqual(self.calls[0][1]['env']['COOKBOOK_ADVISORIES'], 'run')
        proof.assert_not_called()
        self.assertFalse((self.root / '.artifacts/cookbook-validation.json').exists())

    def test_it_refuses_to_run_with_advisories_skipped(self):
        os.environ['COOKBOOK_ADVISORIES'] = 'skip'
        with self.assertRaisesRegex(ValueError, 'refuses COOKBOOK_ADVISORIES=skip'):
            self.run_phase()
        self.assertEqual(self.calls, [])

    def test_it_runs_only_on_the_platform_that_hosts_the_groups(self):
        os.environ[native_target()] = 'aarch64-apple-darwin'
        with self.assertRaisesRegex(ValueError, 'platform that hosts the groups'):
            self.run_phase()
        self.assertEqual(self.calls, [])

    def test_the_composite_action_runs_the_security_phase(self):
        text = (SDK_ROOT / 'security/action.yml').read_text()
        self.assertTrue(text.rstrip().endswith('--single-job --phase security'))
        self.assertIn('using: composite', text)


def native_target():
    import native_ci
    return native_ci.LOCAL_TARGET


class Scans(unittest.TestCase):
    """Advisories on run cargo audit and trivy fs for a Rust CLI; the Build and test default skips both."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'Cargo.toml').write_text('[package]\nname="x"\nversion="0.1.0"\n')
        (self.root / 'Cargo.lock').write_text('version=4\n')
        self.config = {'components': {'rust': [{'name': 'cli', 'manifest': 'Cargo.toml'}]},
                       'security': {'trivy_paths': ['.']}}
        self.commands = []
        self.addCleanup(patch.dict(os.environ).stop)
        os.environ.pop('COOKBOOK_ADVISORIES', None)

    def security(self, advisories):
        environment = {'COOKBOOK_ADVISORIES': advisories}
        with patch.object(rust_cli, 'run', side_effect=lambda command, *args: self.commands.append(command)), \
                patch.object(rust_cli, 'helper', MagicMock()), patch.object(rust_cli, 'product_tests'), \
                patch.object(rust_cli.node_component, 'audit') as audit:
            rust_cli.security(self.root, self.config, environment)
        return audit

    def names(self):
        return [' '.join(command[:2]) for command in self.commands if command[0] in {'cargo', 'trivy'}]

    def test_advisories_run_executes_cargo_audit_and_trivy_fs(self):
        self.security('run')
        self.assertEqual(self.names(), ['cargo audit', 'trivy fs'])

    def test_advisories_skip_executes_neither(self):
        self.security('skip')
        self.assertEqual(self.names(), [])


if __name__ == '__main__':
    unittest.main()
