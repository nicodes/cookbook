from source_fixture import SDK_ROOT, SDK_REF, run_task
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import dispatch
import mise_entrypoint


class MakeContract(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'tests').mkdir()
        (self.root / 'tests/native.py').write_text('raise RuntimeError("native hook is capture-only in this unit control")\n')
        self.hooks = {
            'setup': 'setup', 'fmt': 'fmt', 'build': 'build', 'validate': 'validate',
            'test_lint': 'lint', 'test_unit': 'unit', 'test_security': 'security',
            'test_integration': 'integration', 'test_e2e': 'e2e', 'clean': 'clean',
        }
        self.write()

    def write(self, optional='', suites='lint unit integration e2e security'):
        self.suites = suites.split()
        names = set(self.suites) & set(mise_entrypoint.SUITES)
        body = 'version=1\nrecipe="react-native"\n'
        for name in self.suites:
            body += '[[tests.' + name + ']]\nrunner="python"\npath="tests/native.py"\n'
        capabilities = []
        if optional:
            body += '[database.status]\nrunner="python"\npath="tests/native.py"\n'
            capabilities.append('db:status')
            self.hooks['db_status'] = 'status'
        (self.root / 'cookbook.toml').write_text(body)
        text = mise_entrypoint.render('[tools]\npython="3.13.11"\n', SDK_REF,
            'react-native', body.encode(), [name for name in mise_entrypoint.SUITES
                                           if name in names | {'lint', 'unit', 'security'}], capabilities)
        if not {'lint', 'unit', 'security'} <= names:
            text = re.sub(r'\n\[tasks\."test:security"\].*?(?=\n\[tasks\.|\n# END)', '', text, flags=re.S)
        (self.root / 'mise.toml').write_text(text)

    def invoke(self, root, config, operation):
        if operation == 'test':
            dispatch.execute(root, '_tests')
            return
        name = operation.replace(':', '_')
        if name not in self.hooks:
            raise ValueError('recipe does not implement build output validation')
        with (self.root / 'operations').open('a') as output:
            output.write(self.hooks[name] + '\n')

    def run_make(self, *targets, success=True):
        operation = targets[0] if targets else 'help'
        if operation == 'help':
            result = run_task(self.root, 'help')
        else:
            try:
                with patch.object(dispatch, 'invoke', side_effect=self.invoke):
                    dispatch.execute(self.root, operation)
                result = subprocess.CompletedProcess(['mise', 'run', operation], 0, '', '')
            except ValueError as error:
                result = subprocess.CompletedProcess(['mise', 'run', operation], 1, '', str(error))
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def operations(self):
        path = self.root / 'operations'
        return path.read_text().splitlines() if path.exists() else []

    def test_default_help_does_not_prepare_build_or_test(self):
        output = self.run_make().stdout
        self.assertIn('mise run build', output)
        self.assertIn('mise run test:integration', output)
        self.assertNotIn('mise run dev ', output)
        self.assertNotIn('mise run db:', output)
        self.assertEqual(self.operations(), [])

    def test_aggregate_runs_exact_declared_suites_without_build_or_setup(self):
        self.run_make('test')
        self.assertCountEqual(self.operations(), ['lint', 'unit', 'security', 'integration', 'e2e'])
        self.assertEqual(self.operations(), ['lint', 'unit', 'security', 'integration', 'e2e'])

    def test_build_always_validates_without_running_tests(self):
        self.run_make('build')
        self.assertEqual(self.operations(), ['build', 'validate'])

    def test_missing_validator_cannot_report_a_successful_build(self):
        del self.hooks['validate']
        self.write()
        result = self.run_make('build', success=False)
        self.assertIn('does not implement build output validation', result.stderr)

    def test_absent_capabilities_do_not_become_successful_empty_targets(self):
        self.write(suites='lint unit security')
        for target in ['dev', 'dev:stop', 'dev:logs', 'db:reset', 'test:e2e', 'test:integration']:
            self.run_make(target, success=False)
        self.assertEqual(self.operations(), [])

    def test_selected_test_does_not_run_other_groups(self):
        self.run_make('test:unit')
        self.assertEqual(self.operations(), ['unit'])

    def test_declared_optional_capability_is_shown_and_callable(self):
        self.write('declare native database status')
        self.assertIn('mise run db:status', self.run_make('help').stdout)
        self.run_make('db:status')
        self.assertEqual(self.operations(), ['status'])

    def test_unknown_or_missing_required_suite_is_refused(self):
        for suites in ['lint unit security typo', 'lint unit']:
            self.write(suites=suites)
            self.run_make('test', success=False)


if __name__ == '__main__':
    unittest.main()
