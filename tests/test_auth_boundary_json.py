"""Admit owned native findings and preserve generic and Preview enforcement."""
from source_fixture import SDK_ROOT, TOOLS_ROOT, write_source
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
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
        self.checker = self.root / 'tests/native-auth.mjs'
        self.checker.parent.mkdir()
        self.checker.write_text('if(process.argv.slice(2).join()!=="--native")throw Error("native mode required"); console.log("[]");\n')
        subprocess.run(['git', 'init', '--quiet', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)

    def script(self, policy, contract, output='json'):
        native = auth_boundary.native_checker(self.root, 'tests/native-auth.mjs') if policy.get('api') and policy.get('provider') == 'clerk' else None
        args = [self.bun, '--eval', auth_boundary.SCRIPT, self.module,
                json.dumps(policy), json.dumps(contract), json.dumps(native)]
        if output == 'json':
            args.append('json')
        return subprocess.run(args, cwd=self.root, env={**os.environ, 'GITHUB_REPOSITORY': 'fixture/auth'},
                              capture_output=True, text=True, timeout=40)

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

    def test_canonical_preview_composes_actual_native_success_without_go_guessing(self):
        (self.root / 'api').mkdir()
        (self.root / 'api/config.rs').write_text('CLERK_AUTHORIZED_PARTIES ALLOWED_ORIGINS azp\n')
        values = self.findings({'provider': 'clerk', 'api': True},
            {'present': True, 'origin_layout': 'subdomain',
             'origin_variables': ['ALLOWED_ORIGINS', 'CLERK_AUTHORIZED_PARTIES']})
        failures = [item['text'] for item in values if item['level'] == 'fail']
        self.assertEqual(failures, [])
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

    def test_python_passes_the_admitted_native_identity_and_json_only_adds_output_argument(self):
        policy = {'provider': 'clerk', 'api': True, 'native_checker': 'tests/native-auth.mjs'}
        write_source(self.root / 'auth.toml', policy)
        contract = {'present': False}
        with patch.object(auth_boundary, 'load', return_value={}), \
             patch.object(preview_workflows, 'auth_contract', return_value=contract) as composition, \
             patch.object(auth_boundary, 'native_checker', return_value={'path': 'owned', 'sha256': 'a' * 64}), \
             patch.object(auth_boundary.dependencies, 'tools_root', return_value=TOOLS_ROOT), \
             patch.object(auth_boundary.sdk_environment, 'command_environment', return_value={'guarded': 'yes'}) as activate, \
             patch.object(auth_boundary, 'subprocess', SimpleNamespace(run=Mock())):
            native = auth_boundary.subprocess.run
            auth_boundary.check(self.root, 'auth.toml')
            original = ['bun', '--eval', auth_boundary.SCRIPT, self.module,
                        json.dumps(policy), json.dumps(contract), json.dumps({'path': 'owned', 'sha256': 'a' * 64})]
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
            for policy in [{'provider': 'clerk', 'api': True},
                           {'provider': 'clerk', 'api': True, 'language': 'rust'},
                           {'provider': 'none', 'api': False, 'native_checker': 'tests/native-auth.mjs'},
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
            write_source(fixture.root / 'auth.toml', {'provider': 'clerk', 'api': True,
                'native_checker': 'tests/native-auth.mjs'})
            with patch.object(auth_boundary, 'load', return_value=fixture.config), \
                 patch.object(auth_boundary, 'subprocess', SimpleNamespace(run=Mock())), \
                 self.assertRaisesRegex(ValueError, 'reviewed'):
                native = auth_boundary.subprocess.run
                auth_boundary.check(fixture.root, 'auth.toml', output_format='json')
            native.assert_not_called()

    def test_native_failure_is_preserved_once_with_existing_preview_failures(self):
        self.checker.write_text('console.log(JSON.stringify([{level:"fail",text:"owned shipping refusal missing"}]));process.exit(1);')
        values = self.findings({'provider': 'clerk', 'api': True},
            {'present': True, 'origin_layout': 'subdomain', 'origin_variables': ['MISSING_ORIGIN']})
        self.assertEqual(values.count({'level': 'fail', 'text': 'owned shipping refusal missing'}), 1)
        self.assertIn({'level': 'fail', 'text':
            'the declared Preview origin carrier does not reach native API configuration: MISSING_ORIGIN'}, values)

    def test_native_stdout_stderr_and_exit_protocol_is_exact(self):
        cases = ['console.log("not json")', 'console.log("[]");console.log("extra")',
                 'console.error("noise");console.log("[]")', 'console.log("[]");process.exit(1)',
                 'console.log("[]");process.exit(2)',
                 'console.log(JSON.stringify([{level:"fail",text:"native"}]))',
                 'console.log(JSON.stringify([{level:"warn",text:"native"}]))',
                 'console.log(JSON.stringify([{level:"info",text:"native",extra:true}]))']
        for body in cases:
            with self.subTest(body=body):
                self.checker.write_text(body)
                result = self.script({'provider': 'clerk', 'api': True}, {'present': False})
                self.assertNotEqual(result.returncode, 0)

    def test_native_output_is_bounded_before_json_admission(self):
        self.checker.write_text('console.log(JSON.stringify([{level:"info",text:"x".repeat(1048577)}]));')
        result = self.script({'provider': 'clerk', 'api': True}, {'present': False})
        self.assertNotEqual(result.returncode, 0)
        self.assertLess(len(result.stdout), 1048576)
        self.assertIn('did not complete with bounded JSON', result.stderr)

    def test_native_execution_has_a_finite_deadline(self):
        self.checker.write_text('setInterval(()=>{},1000);')
        started = time.monotonic()
        result = self.script({'provider': 'clerk', 'api': True}, {'present': False})
        self.assertNotEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, 40)

    def test_native_checker_is_owned_tracked_bounded_source(self):
        admitted = auth_boundary.native_checker(self.root, 'tests/native-auth.mjs')
        self.assertEqual(admitted, {'path': str(self.checker),
            'sha256': hashlib.sha256(self.checker.read_bytes()).hexdigest()})
        for name in ['../outside.mjs', str(self.checker), 'tests/missing.mjs', 'tests/native.py',
                     'tests//native-auth.mjs', 'tests/./native-auth.mjs', 3]:
            with self.subTest(path=name), self.assertRaises((ValueError, OSError)):
                auth_boundary.native_checker(self.root, name)
        other = self.root / 'tests/untracked.mjs'
        other.write_text('console.log("[]")')
        with self.assertRaisesRegex(ValueError, 'tracked'):
            auth_boundary.native_checker(self.root, 'tests/untracked.mjs')
        tracked = self.root / 'tests/a.mjs'
        tracked.write_text('console.log("[]")')
        subprocess.run(['git', '-C', str(self.root), 'add', 'tests/a.mjs'], check=True)
        pattern = self.root / 'tests/[a].mjs'
        pattern.write_text('console.log("[]")')
        with self.assertRaisesRegex(ValueError, 'tracked'):
            auth_boundary.native_checker(self.root, 'tests/[a].mjs')
        self.checker.unlink()
        self.checker.symlink_to(other)
        with self.assertRaises(ValueError):
            auth_boundary.native_checker(self.root, 'tests/native-auth.mjs')

    def test_native_source_cannot_change_between_admission_and_execution(self):
        native = auth_boundary.native_checker(self.root, 'tests/native-auth.mjs')
        self.checker.write_text('console.log("[]"); // changed source\n')
        result = subprocess.run([self.bun, '--eval', auth_boundary.SCRIPT, self.module,
            json.dumps({'provider': 'clerk', 'api': True}), json.dumps({'present': False}),
            json.dumps(native), 'json'], cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('differs from its admitted source', result.stderr)

    def test_hardlinked_or_oversized_native_sources_refuse(self):
        other = self.root / 'tests/other-name.mjs'
        os.link(self.checker, other)
        with self.assertRaises(ValueError):
            auth_boundary.native_checker(self.root, 'tests/native-auth.mjs')
        other.unlink()
        self.checker.write_text('x' * 1048577)
        with self.assertRaises(ValueError):
            auth_boundary.native_checker(self.root, 'tests/native-auth.mjs')


if __name__ == '__main__':
    unittest.main()
