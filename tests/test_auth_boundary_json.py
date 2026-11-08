"""Expose the existing composed auth decision without changing its rules or status."""
from source_fixture import SDK_ROOT, TOOLS_ROOT, write_source
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import auth_boundary
import preview_workflows
import test_preview_contract as preview_fixture


class AuthBoundaryJSON(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='composed auth source ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bun = shutil.which('bun')
        self.assertIsNotNone(self.bun, 'the prepared provider must retain its declared Bun SDK')
        self.module = (TOOLS_ROOT / 'helpers/auth-boundary.mjs').as_uri()

    def script(self, policy, contract, output='json'):
        args = [self.bun, '--eval', auth_boundary.SCRIPT, self.module,
                json.dumps(policy), json.dumps(contract)]
        if output == 'json':
            args.append('json')
        return subprocess.run(args, cwd=self.root, env={**os.environ, 'GITHUB_REPOSITORY': 'fixture/auth'},
                              capture_output=True, text=True, timeout=20)

    def findings(self, policy, contract):
        result = self.script(policy, contract)
        values = json.loads(result.stdout)
        self.assertIsInstance(values, list)
        self.assertEqual(result.returncode, int(any(item['level'] == 'fail' for item in values)))
        self.assertEqual(result.stderr, '')
        return values

    def test_json_and_default_text_have_exactly_the_same_findings_and_enforcement(self):
        for policy in [{'provider': 'none', 'api': False}, {'provider': 'clerk', 'api': True}]:
            with self.subTest(policy=policy):
                values = self.findings(policy, {'present': False})
                expected = subprocess.run([self.bun, '--eval',
                    "const {report}=await import(process.argv[1]);const r=report('fixture/auth',JSON.parse(process.argv[2]),true);"
                    "console.log(JSON.stringify(r));", self.module, json.dumps(values)],
                    cwd=self.root, capture_output=True, text=True, timeout=20)
                self.assertEqual(expected.returncode, 0, expected.stderr)
                rendered = json.loads(expected.stdout)
                text = self.script(policy, {'present': False}, output='text')
                self.assertEqual(text.returncode, int(rendered['failed']))
                self.assertEqual(text.stderr if rendered['failed'] else text.stdout, rendered['text'] + '\n')
                self.assertEqual(text.stdout if rendered['failed'] else text.stderr, '')

    def test_canonical_preview_uses_its_composed_origin_contract_and_retains_go_only_findings(self):
        (self.root / 'api').mkdir()
        (self.root / 'api/config.rs').write_text('CLERK_AUTHORIZED_PARTIES ALLOWED_ORIGINS azp\n')
        values = self.findings({'provider': 'clerk', 'api': True},
            {'present': True, 'origin_layout': 'subdomain',
             'origin_variables': ['ALLOWED_ORIGINS', 'CLERK_AUTHORIZED_PARTIES']})
        failures = [item['text'] for item in values if item['level'] == 'fail']
        self.assertEqual(len(failures), 2)
        self.assertTrue(failures[0].startswith('no local development path behind `//go:build localdev`,'))
        self.assertTrue(failures[1].startswith('no `//go:build !localdev` counterpart refuses the local switch.'))
        self.assertEqual(values[-1], {'level': 'info', 'text':
            'the canonical trusted-main Preview caller retains readonly full validation, a development tenant and host-resolved PR origins'})

    def test_missing_native_origin_and_authorized_party_remain_failed_json_findings(self):
        (self.root / 'api').mkdir()
        (self.root / 'api/config.rs').write_text('ALLOWED_ORIGINS\n')
        values = self.findings({'provider': 'clerk', 'api': True},
            {'present': True, 'origin_layout': 'subdomain',
             'origin_variables': ['ALLOWED_ORIGINS', 'CLERK_AUTHORIZED_PARTIES']})
        self.assertIn({'level': 'fail', 'text':
            'the declared Preview origin carrier does not reach native API configuration: CLERK_AUTHORIZED_PARTIES'}, values)
        self.assertIn({'level': 'fail', 'text':
            'the native Preview API must retain its token authorized-party decision'}, values)

    def test_invalid_preview_contract_and_missing_real_api_are_not_hidden_by_json(self):
        cases = [({'provider': 'clerk', 'api': True},
                  {'present': True, 'origin_layout': 'shared', 'origin_variables': []},
                  'the cookbook Preview must retain the explicit development-tenant and PR-origin contract'),
                 ({'provider': 'clerk', 'api': False},
                  {'present': True, 'origin_layout': 'subdomain', 'origin_variables': []},
                  'this native Preview declaration requires its real authenticated API boundary')]
        for policy, contract, message in cases:
            with self.subTest(message=message):
                self.assertIn({'level': 'fail', 'text': message}, self.findings(policy, contract))

    def test_generic_core_findings_are_preserved_in_json(self):
        self.assertEqual(self.findings({'provider': 'unexpected', 'api': False}, {'present': False}),
                         [{'level': 'fail', 'text': 'unknown auth provider "unexpected"'}])

    def test_python_default_native_argv_is_unchanged_and_json_only_adds_output_argument(self):
        policy = {'provider': 'clerk', 'api': True}
        write_source(self.root / 'auth.toml', policy)
        contract = {'present': False}
        with patch.object(auth_boundary, 'load', return_value={}), \
             patch.object(preview_workflows, 'auth_contract', return_value=contract) as composition, \
             patch.object(auth_boundary.dependencies, 'tools_root', return_value=TOOLS_ROOT), \
             patch.object(auth_boundary.sdk_environment, 'command_environment', return_value={'guarded': 'yes'}) as activate, \
             patch.object(auth_boundary, 'subprocess', SimpleNamespace(run=Mock())):
            native = auth_boundary.subprocess.run
            auth_boundary.check(self.root, 'auth.toml')
            original = ['bun', '--eval', auth_boundary.SCRIPT, self.module,
                        json.dumps(policy), json.dumps(contract)]
            self.assertEqual(native.call_args.args, (original,))
            self.assertEqual(native.call_args.kwargs, {'cwd': self.root, 'env': {'guarded': 'yes'}, 'check': True})
            auth_boundary.check(self.root, 'auth.toml', output_format='json')
            self.assertEqual(native.call_args.args, (original + ['json'],))
            self.assertEqual(activate.call_count, 2)
            self.assertEqual(composition.call_count, 2)

    def test_invalid_format_and_policy_refuse_before_native_execution(self):
        with patch.object(auth_boundary, 'subprocess', SimpleNamespace(run=Mock())):
            native = auth_boundary.subprocess.run
            with self.assertRaisesRegex(ValueError, 'output format'):
                auth_boundary.check(self.root, 'missing.toml', output_format='other')
            for policy in [{'provider': 'clerk', 'api': True, 'language': 'rust'},
                           {'provider': 'clerk', 'api': 'true'}, {'provider': 'clerk'}]:
                write_source(self.root / 'auth.toml', policy)
                with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, 'auth policy'):
                    auth_boundary.check(self.root, 'auth.toml', output_format='json')
            native.assert_not_called()

    def test_actual_canonical_preview_source_tampering_still_refuses_before_output(self):
        fixture = preview_fixture.PreviewDeclaration()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with patch.object(preview_workflows.images, 'definition', return_value=fixture.definition):
            files = preview_workflows.workflow_files(fixture.root, 'd' * 40, fixture.config)
            for name, body in files.items():
                path = fixture.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
            self.assertTrue(preview_workflows.auth_contract(fixture.root, fixture.config)['present'])
            target = fixture.root / '.github/workflows/pr-preview.yml'
            target.write_text(target.read_text().replace('      merge-gate: false', '      merge-gate: true'))
            write_source(fixture.root / 'auth.toml', {'provider': 'clerk', 'api': True})
            with patch.object(auth_boundary, 'load', return_value=fixture.config), \
                 patch.object(auth_boundary, 'subprocess', SimpleNamespace(run=Mock())), \
                 self.assertRaisesRegex(ValueError, 'reviewed'):
                native = auth_boundary.subprocess.run
                auth_boundary.check(fixture.root, 'auth.toml', output_format='json')
            native.assert_not_called()


if __name__ == '__main__':
    unittest.main()
