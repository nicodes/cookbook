from source_fixture import SDK_ROOT
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import operational_workflows as operations
import configuration
import generate
from source_fixture import read_source, write_source, dump_source
from workflow_fixtures import read_yaml


class OperationalWorkflows(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ['deploy/health.py', 'deploy/security.py', 'deploy/backup.toml',
                     'deploy/deployed-scan.toml', 'deploy/restore.toml', 'api/Cargo.toml']:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# Native fixture\n')
        self.config = {'recipe': 'react-native', 'delivery': {'definition': 'deploy/delivery.toml'},
                       'operations': {'definition': 'deploy/operations.toml'}}
        delivery = dict(version=1, project='demo', komizo_ref='a' * 40,
                        publish_runner='ubuntu-24.04', publish_timeout=35,
                        deploy_runner='ubuntu-24.04', deploy_timeout=100,
                        health_urls=['https://example.test/readyz'], notification_url='https://example.test/')
        write_source(self.root / 'deploy/delivery.toml', delivery)
        self.data = dict(version=1, project='demo',
            backup=dict(runner='ubuntu-24.04', timeout_minutes=40, environment='production',
                        schedule=['8 0 * * *'], config='deploy/backup.toml',
                        concurrency='deploy-production', queue='max', gate_variable='BACKUP_ENABLED'),
            security=dict(runner='ubuntu-24.04', timeout_minutes=110, report_timeout=5,
                          deployed_policy='deploy/deployed-scan.toml', evidence=['.artifacts/security/']),
            production=dict(runner='ubuntu-24.04', timeout_minutes=45, environment='Production',
                            checks=[dict(runner='python', path='deploy/health.py', timeout=180)]),
            paid=[dict(id='model_smoke', description='Run the existing live model assertion',
                       runner='ubuntu-24.04', timeout_minutes=20, environment='Production',
                       deployment_required=True, skip_default=True,
                       check=dict(runner='cargo-run', path='api/Cargo.toml', binary='model-smoke',
                                  args=['--live'], timeout=1200))],
            restore_profiles=[dict(name='default', project='demo', config='deploy/restore.toml')])

    def write(self):
        write_source(self.root / 'deploy/operations.toml', self.data)

    def declaration(self):
        self.write()
        return operations.declaration(self.root, self.config)

    def production(self):
        self.write()
        mode = operations.production_verification(self.root, 'b' * 40, self.config)
        return '\n'.join([*mode['inputs'], *mode['jobs']])

    def test_native_budgets_schedules_environments_and_transport_remain_explicit(self):
        self.declaration()
        files = operations.workflow_files(self.root, 'b' * 40, self.config)
        backup = files['.github/workflows/backup.yml']
        self.assertIn('cron: "8 0 * * *"', backup)
        self.assertIn('timeout-minutes: 40', backup)
        self.assertIn('queue: max', backup)
        self.assertIn("vars.BACKUP_ENABLED == 'true'", backup)
        self.assertNotIn('.github/workflows/recovery.yml', files)
        self.assertEqual(read_yaml(files['.github/workflows/security.yml'])['jobs']['report-security-failure']['with']['timeout-minutes'], 5)

    def test_paid_provider_key_only_enters_the_explicit_default_off_job(self):
        text = self.production()
        self.assertIn('default: false', text)
        ordinary, paid = text.split('\n  paid-model_smoke:', 1)
        self.assertIn('!inputs.model_smoke', ordinary)
        self.assertNotIn('secrets.OPENROUTER_API_KEY', ordinary)
        self.assertIn('&& inputs.model_smoke', paid)
        self.assertIn('timeout-minutes: 20', paid)
        self.assertIn('environment: Production', paid)
        self.assertIn('secrets.OPENROUTER_API_KEY', paid)

    def test_failure_report_inherits_CI_runner_without_changing_audit_authority(self):
        self.config['ci'] = {'runner': 'blacksmith-4vcpu-ubuntu-2404'}
        self.write()
        jobs = read_yaml(operations.workflow_files(self.root, 'b' * 40, self.config)[
            '.github/workflows/security.yml'])['jobs']
        report, audit = jobs['report-security-failure'], jobs['security']
        self.assertEqual(report['with']['runner'], self.config['ci']['runner'])
        self.assertEqual(report['with']['timeout-minutes'], 5)
        self.assertEqual(report['permissions'], {'contents': 'read', 'issues': 'write'})
        self.assertEqual(audit['with']['runner'], self.data['security']['runner'])
        self.assertEqual(audit['permissions']['issues'], 'write')  # the audit workflow may report its own failure

    def single_files(self, **security):
        self.config['ci'] = {'single_job': True}
        self.data['security'].update(security)
        self.data['maintenance'] = {
            'tools': dict(runner='ubuntu-24.04', timeout_minutes=40, schedule=['5 2 * * *'], concurrency='dependency-maintenance-tools'),
            'packages': dict(runner='ubuntu-24.04', timeout_minutes=60, schedule=['10 2 * * 1'])}
        self.data['security']['schedule'] = ['15 3 * * *']
        self.write()
        return operations.workflow_files(self.root, 'b' * 40, self.config)

    def test_single_job_security_is_one_audit_job_that_reports_itself(self):
        files = self.single_files()
        text = files['.github/workflows/security.yml']
        workflow = read_yaml(text)
        self.assertEqual(set(workflow['jobs']), {'security'})
        self.assertEqual(workflow['on'], {'schedule': [{'cron': '15 3 * * *'}], 'workflow_dispatch': None})
        job = workflow['jobs']['security']
        self.assertEqual(job['with']['report'], True)
        self.assertTrue(job['uses'].startswith('nicodes/komizo-actions/.github/workflows/audit-security.yml@'))
        self.assertEqual(job['permissions']['issues'], 'write')
        self.assertEqual(job['if'], "${{ github.ref == 'refs/heads/main' }}")
        for absent in ['report-security-failure', 'inputs.task', 'report-timeout', '\n  tools:', '\n  packages:']:
            self.assertNotIn(absent, text)
        self.assertNotIn('COOKBOOK_ADVISORIES', text)

    def test_single_job_security_without_reporting_omits_the_report_input(self):
        text = self.single_files(report=False)['.github/workflows/security.yml']
        self.assertNotIn('report: true', text)
        self.assertIn('      issues: write\n', text)

    def test_single_job_maintenance_moves_to_its_own_workflow_unchanged(self):
        files = self.single_files()
        legacy_config = dict(self.config, ci={})
        legacy = read_yaml(operations.workflow_files(self.root, 'b' * 40, legacy_config)['.github/workflows/security.yml'])['jobs']
        maintenance = read_yaml(files['.github/workflows/maintenance.yml'])
        self.assertEqual(maintenance['name'], 'Maintenance')
        self.assertEqual(maintenance['on']['schedule'], [{'cron': '5 2 * * *'}, {'cron': '10 2 * * 1'}])
        self.assertEqual(maintenance['on']['workflow_dispatch']['inputs']['task']['options'], ['dependencies'])
        self.assertEqual(maintenance['jobs'], {name: legacy[name] for name in ['tools', 'packages']})

    def test_canonical_generator_emits_single_job_security_and_maintenance_workflows(self):
        self.data.pop('production');self.data.pop('paid')
        self.single_files()
        (self.root/'tests').mkdir();(self.root/'tests/unit.py').write_text('assert 2 + 2 == 4\n')
        (self.root/'cookbook.toml').write_text('version=1\nrecipe="react-native"\n[ci]\nsingle_job=true\n[delivery]\ndefinition="deploy/delivery.toml"\n[operations]\ndefinition="deploy/operations.toml"\n[[tests.unit]]\nrunner="python"\npath="tests/unit.py"\n')
        files=generate.workflow_files(self.root,'b'*40)
        self.assertLessEqual({'.github/workflows/security.yml','.github/workflows/maintenance.yml'},set(files))
        self.assertEqual(set(read_yaml(files['.github/workflows/security.yml'])['jobs']),{'security'})
        self.assertEqual(set(read_yaml(files['.github/workflows/maintenance.yml'])['jobs']),{'tools','packages'})

    # Inputs and secrets declared by the reusable verification workflows, read from nicodes/komizo-actions at commit
    # 89d0efbfd085dc1107fd782672b587f322c0909a (.github/workflows/verify-production.yml and
    # verify-public-production.yml; the workflow_run admission change in operational.py leaves them untouched).
    # This repository cannot read that sibling in CI, so a change there needs these lists updated.
    VERIFY_REQUIRED = {'cookbook-ref', 'komizo-ref', 'declaration', 'step-timeouts', 'runner', 'timeout-minutes',
        'operation', 'check'}
    VERIFY_INPUTS = VERIFY_REQUIRED | {'environment', 'inline-paid', 'inline-step-timeouts'}
    VERIFY_SECRETS = {'OPENROUTER_API_KEY'}
    VERIFY_PUBLIC_INPUTS = VERIFY_REQUIRED | {'environment'}

    def verification(self, **changes):
        self.config['ci'] = {'single_job': True}
        for role in ['backup', 'security', 'restore_profiles']:
            self.data.pop(role, None)
        self.data['production'].update(changes)
        self.write()
        return operations.workflow_files(self.root, 'b' * 40, self.config)

    def test_single_job_production_verification_is_its_own_workflow_golden(self):
        self.data.pop('paid')
        files = self.verification()
        self.assertEqual(set(files), {'.github/workflows/verify-production.yml'})
        self.assertEqual(files['.github/workflows/verify-production.yml'], """# Generated cookbook caller. Change native operation data and regenerate.
name: Verify production
'on':
  workflow_run:
    workflows: [Deploy]
    types: [completed]
    branches: [main]
  workflow_dispatch:
permissions: {}
concurrency:
  group: production-verification
  cancel-in-progress: false
jobs:
  production:
    name: Verify production
    if: ${{ github.ref == 'refs/heads/main' && ((github.event_name == 'workflow_run' && github.event.workflow_run.path == '.github/workflows/deploy.yml' && github.event.workflow_run.conclusion == 'success' && github.event.workflow_run.head_branch == 'main' && contains(fromJSON('["push","workflow_dispatch"]'), github.event.workflow_run.event) && github.event.workflow_run.head_repository.full_name == github.repository && github.event.workflow_run.repository.full_name == github.repository && github.event.workflow_run.head_sha == github.sha) || (github.event_name == 'workflow_dispatch')) }}
    uses: nicodes/komizo-actions/.github/workflows/verify-production.yml@""" + 'a' * 40 + """
    permissions:
      contents: read
      actions: read
      attestations: read
    with:
      cookbook-ref: """ + 'b' * 40 + """
      komizo-ref: """ + 'a' * 40 + """
      declaration: "deploy/operations.toml"
      runner: ubuntu-24.04
      timeout-minutes: 45
      step-timeouts: "{\\"artifact\\": 45, \\"checkout\\": 45, \\"plan\\": 5, \\"prepare\\": 45, \\"run\\": 45, \\"setup\\": 45}"
      operation: production
      check: ""
      environment: Production
""")

    def test_single_job_verification_runs_after_deploy_on_schedule_and_on_manual_dispatch_only(self):
        files = self.verification(schedule=['30 4 * * *'], concurrency='production-verification', queue='max')
        workflow = read_yaml(files['.github/workflows/verify-production.yml'])
        self.assertEqual(workflow['on'], {
            'workflow_run': {'workflows': ['Deploy'], 'types': ['completed'], 'branches': ['main']},
            'schedule': [{'cron': '30 4 * * *'}],
            'workflow_dispatch': {'inputs': {'model_smoke': {
                'description': 'Run the existing live model assertion', 'type': 'boolean', 'default': False}}}})
        self.assertEqual(workflow['concurrency'], {'group': 'production-verification', 'cancel-in-progress': False, 'queue': 'max'})
        self.assertEqual(workflow['permissions'], {})
        self.assertEqual(set(workflow['jobs']), {'production', 'paid-model_smoke'})
        for name, job in workflow['jobs'].items():
            with self.subTest(job=name):
                self.assertIn("github.event_name == 'schedule' && contains(fromJSON('[\"30 4 * * *\"]'), github.event.schedule)", job['if'])
                self.assertEqual(job['permissions'], {'contents': 'read', 'actions': 'read', 'attestations': 'read'})
                self.assertEqual(job['with']['environment'], 'Production')
        self.assertTrue(workflow['jobs']['production']['if'].endswith('&& !inputs.model_smoke }}'))
        self.assertTrue(workflow['jobs']['paid-model_smoke']['if'].endswith('&& inputs.model_smoke }}'))
        self.assertIn('OPENROUTER_API_KEY', workflow['jobs']['paid-model_smoke']['secrets'])
        self.assertNotIn('secrets', workflow['jobs']['production'])

    def test_workflow_run_verification_only_acts_on_this_repositorys_own_main_deploy_of_the_current_revision(self):
        text = self.verification()['.github/workflows/verify-production.yml']
        workflow = read_yaml(text)
        condition = workflow['jobs']['production']['if']
        for required in ["github.ref == 'refs/heads/main'",                          "github.event.workflow_run.path == '.github/workflows/deploy.yml'",
                         "github.event.workflow_run.conclusion == 'success'",
                         "github.event.workflow_run.head_branch == 'main'",
                         "contains(fromJSON('[\"push\",\"workflow_dispatch\"]'), github.event.workflow_run.event)",
                         "github.event.workflow_run.head_repository.full_name == github.repository",
                         "github.event.workflow_run.head_sha == github.sha"]:
            self.assertIn(required, condition)
        self.assertNotIn('workflow_run.name', text)  # a run's name is its run-name, e.g. "Deploy · main"
        self.assertNotIn('workflow_run.display_title', text)
        # No pull request trigger, no checkout and no step of its own: only the pinned reusable verification runs.
        self.assertEqual(set(workflow['on']), {'workflow_run', 'workflow_dispatch'})
        for absent in ['pull_request', 'actions/checkout', 'pull_request_target', 'steps:', 'secrets: inherit']:
            self.assertNotIn(absent, text)

    def test_single_job_verification_caller_matches_the_reusable_workflow_interface(self):
        for environment, inputs in [('Production', self.VERIFY_INPUTS), (None, self.VERIFY_PUBLIC_INPUTS)]:
            with self.subTest(environment=environment):
                if environment is None:
                    self.data['production'].pop('environment');self.data.pop('paid', None)
                files = self.verification()
                jobs = read_yaml(files['.github/workflows/verify-production.yml'])['jobs']
                for job in jobs.values():
                    self.assertTrue(job['uses'].startswith('nicodes/komizo-actions/.github/workflows/verify-'))
                    self.assertLessEqual(set(job['with']), inputs)
                    self.assertLessEqual(self.VERIFY_REQUIRED, set(job['with']))
                    self.assertLessEqual(set(job.get('secrets', {})), self.VERIFY_SECRETS)
                    self.assertEqual(set(json.loads(job['with']['step-timeouts'])), {'artifact', 'checkout', 'plan', 'prepare', 'run', 'setup'})
                if environment is None:
                    self.assertIn('verify-public-production.yml@', jobs['production']['uses'])

    def test_single_job_generation_emits_verification_beside_the_one_job_deploy(self):
        self.data.pop('security');self.data.pop('restore_profiles');self.data.pop('backup');self.write()
        (self.root/'tests').mkdir();(self.root/'tests/unit.py').write_text('assert 2 + 2 == 4\n')
        for flag, expected in [('[ci]\nsingle_job=true\n', True), ('', False)]:
            (self.root/'cookbook.toml').write_text('version=1\nrecipe="react-native"\n'+flag+'[delivery]\ndefinition="deploy/delivery.toml"\n[operations]\ndefinition="deploy/operations.toml"\n[[tests.unit]]\nrunner="python"\npath="tests/unit.py"\n')
            files=generate.workflow_files(self.root,'b'*40)
            with self.subTest(single_job=expected):
                self.assertEqual('.github/workflows/verify-production.yml' in files, expected)
                deploy=read_yaml(files['.github/workflows/deploy.yml'])
                if expected:
                    self.assertEqual(set(deploy['jobs']),{'deploy'})
                    self.assertNotIn('mode',deploy['on']['workflow_dispatch']['inputs'])
                else:
                    self.assertLessEqual({'production','paid-model_smoke'},set(deploy['jobs']))
                    self.assertIn('mode',deploy['on']['workflow_dispatch']['inputs'])

    def test_without_the_flag_production_verification_stays_in_deploy_unchanged(self):
        self.data.pop('security');self.data.pop('restore_profiles');self.data.pop('backup');self.write()
        explicit = operations.workflow_files(self.root, 'b' * 40, dict(self.config, ci={'single_job': False}))
        absent = operations.workflow_files(self.root, 'b' * 40, {k: v for k, v in self.config.items() if k != 'ci'})
        self.assertEqual(explicit, absent)
        self.assertEqual(absent, {})
        legacy = operations.production_verification(self.root, 'b' * 40, self.config)
        self.assertEqual(legacy['condition'], "github.ref == 'refs/heads/main' && ((github.event_name == 'workflow_dispatch' && inputs.mode == 'verify'))")
        self.assertNotIn('workflow_run', '\n'.join(legacy['jobs']))

    def test_without_the_flag_security_keeps_its_legacy_shape_apart_from_issue_authority(self):
        self.single_files()
        explicit = operations.workflow_files(self.root, 'b' * 40, dict(self.config, ci={'single_job': False}))
        absent = operations.workflow_files(self.root, 'b' * 40, {k: v for k, v in self.config.items() if k != 'ci'})
        self.assertEqual(explicit, absent)
        self.assertEqual(set(absent), {'.github/workflows/backup.yml', '.github/workflows/security.yml'})
        workflow = read_yaml(absent['.github/workflows/security.yml'])
        self.assertEqual(set(workflow['jobs']), {'security', 'report-security-failure', 'tools', 'packages'})
        self.assertNotIn('report', workflow['jobs']['security']['with'])
        self.assertEqual(workflow['jobs']['security']['permissions']['issues'], 'write')
        self.assertIn('inputs.task', absent['.github/workflows/security.yml'])

    def test_environment_free_production_keeps_the_public_native_callee(self):
        self.data['production'].pop('environment')
        self.data.pop('paid')
        text = self.production()
        self.assertIn('verify-public-production.yml@' + 'a' * 40, text)
        self.assertNotIn('environment:', text)

    def test_inline_paid_assertion_shares_the_original_job_and_total_budget(self):
        check = self.data['paid'][0]
        check.update(inline=True, skip_default=False, timeout_minutes=45,
                     step_timeouts={'prepare': 5, 'run': 5})
        text = self.production()
        self.assertNotIn('\n  paid-model_smoke:', text)
        self.assertNotIn('!inputs.model_smoke', text)
        self.assertEqual(text.count('    timeout-minutes: 45'), 1)
        self.assertIn('      check: "model_smoke"', text)
        self.assertIn('inline-paid: ${{ inputs.model_smoke || false }}', text)
        self.assertIn('inline-step-timeouts:', text)
        for changes in [dict(timeout_minutes=20), dict(environment='production'),
                        dict(runner='ubuntu-22.04'), dict(skip_default=True)]:
            original = copy.deepcopy(check)
            check.update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'original production job'):
                self.declaration()
            check.clear()
            check.update(original)

    def test_native_evidence_names_and_absence_policy_remain_explicit(self):
        record = self.data['production']
        record.update(evidence=['production-smoke-evidence/'],
                      artifact_name='anonymous-production-smoke-{run_id}', evidence_missing='warn')
        with self.assertRaisesRegex(ValueError, 'explicitly registered'):
            self.declaration()
        self.config['artifacts'] = {'evidence_paths': ['production-smoke-evidence/']}
        self.declaration()
        self.assertEqual(operations.artifact_name('production', record,
            {'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2'}), 'anonymous-production-smoke-123')
        for template in ['../escape', '${{ secrets.KEY }}', 'evidence-{branch}', 'evidence\nname']:
            record['artifact_name'] = template
            with self.subTest(template=template), self.assertRaises(ValueError):
                self.declaration()
        record['artifact_name'] = 'native-{run_attempt}'
        self.data['backup']['evidence_missing'] = 'ignore'
        with self.assertRaisesRegex(ValueError, 'upload behavior'):
            self.declaration()

    def test_retired_recovery_declaration_is_rejected(self):
        self.data['recovery'] = dict(runner='ubuntu-24.04', timeout_minutes=45, environment='production')
        with self.assertRaisesRegex(ValueError, 'unsupported native operational declaration'):
            self.declaration()

    def test_authority_environment_and_unused_native_selectors_are_rejected(self):
        original = copy.deepcopy(self.data)
        mutations = [
            lambda d: d.update(project='other'),
            lambda d: d.update(version=True),
            lambda d: d['backup'].update(checks=[dict(runner='python', path='deploy/health.py')]),
            lambda d: d['security'].update(timeout_minutes=111),
            lambda d: d['security'].update(report_timeout=12),
            lambda d: d['production'].update(runner='${{ vars.RUNNER }}'),
            lambda d: d['production'].update(concurrency='${{ github.ref }}'),
            lambda d: d['production'].update(queue='max'),
            lambda d: d['production']['checks'][0].update(binary='unused'),
            lambda d: d['production']['checks'][0].update(path='scripts/generic-wrapper.py'),
            lambda d: d['production']['checks'][0].update(path='deploy/../secret.py'),
        ]
        for mutation in mutations:
            self.data = copy.deepcopy(original)
            mutation(self.data)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.declaration()
        for key in ['TOOLS_ROOT', 'EXPECTED_REVISION', 'OPENROUTER_API_KEY', 'PATH', 'PYTHONPATH', 'GITHUB_SHA']:
            self.data = copy.deepcopy(original)
            self.data['production']['checks'][0]['environment'] = {key: 'replacement'}
            with self.subTest(environment=key), self.assertRaises(ValueError):
                self.declaration()

    def test_prepare_lists_profiles_and_native_timeouts_cannot_hide_missing_work(self):
        original = copy.deepcopy(self.data)
        mutations = [
            lambda d: d['production'].update(checks=[]),
            lambda d: d['production'].update(checks='program'),
            lambda d: d['production'].update(prepare=dict(native='program')),
            lambda d: d['production'].update(prepare=dict(packages='api')),
            lambda d: d['production'].update(prepare=dict(browser='api')),
            lambda d: d['production'].update(evidence='.artifacts/security'),
            lambda d: d['production']['checks'][0].update(timeout=2701),
            lambda d: d['paid'][0]['check'].update(timeout=1201),
            lambda d: d['restore_profiles'].append(copy.deepcopy(d['restore_profiles'][0])),
            lambda d: d['paid'].append(copy.deepcopy(d['paid'][0])),
            lambda d: d['paid'][0].update(id='production'),
            lambda d: d.pop('production'),
        ]
        for mutation in mutations:
            self.data = copy.deepcopy(original)
            mutation(self.data)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.declaration()

    def test_symlinked_programs_are_never_native_source(self):
        path = self.root / 'deploy/health.py'
        path.unlink()
        path.symlink_to(self.root / 'deploy/security.py')
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            self.declaration()

    def test_source_deployed_and_report_steps_keep_distinct_original_deadlines(self):
        self.data['security'].update(report_timeout=11,
            step_timeouts={'source':30,'deployed':55,'report_checkout':3,'report_setup':5,'report':3})
        self.declaration()
        values=operations.step_timeouts('security',self.data['security'])
        self.assertEqual(values['source'],30)
        self.assertEqual(values['deployed'],55)
        self.assertEqual(values['report'],3)
        self.assertEqual(values['report_setup'],5)
        self.assertEqual(values['checkout'],110)
        for changes in [{'command':3},{'deployed':111},{'source':True},{'report':12}]:
            record=copy.deepcopy(self.data['security'])
            record['step_timeouts'].update(changes)
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                operations.step_timeouts('security',record)

    def test_canonical_generator_includes_only_valid_native_operational_callers(self):
        self.write()
        (self.root/'tests').mkdir()
        (self.root/'tests/unit.py').write_text('assert 2 + 2 == 4\n')
        (self.root/'cookbook.toml').write_text('version=1\nrecipe="react-native"\n[delivery]\ndefinition="deploy/delivery.toml"\n[operations]\ndefinition="deploy/operations.toml"\n[[tests.unit]]\nrunner="python"\npath="tests/unit.py"\n')
        files=generate.workflow_files(self.root,'b'*40)
        self.assertIn('.github/workflows/security.yml',files)
        self.assertNotIn('.github/workflows/recovery.yml',files)
        self.assertNotIn('.github/workflows/verify-production.yml', files)
        deploy = read_yaml(files['.github/workflows/deploy.yml'])
        self.assertEqual(deploy['on']['workflow_dispatch']['inputs']['mode']['options'], ['deploy', 'verify'])
        for job in ['validate', 'publish', 'deploy', 'report', 'notify']:
            self.assertIn("inputs.mode == 'deploy'", deploy['jobs'][job]['if'])
        for job in ['production', 'paid-model_smoke']:
            verify = deploy['jobs'][job]
            self.assertNotIn('needs', verify)
            self.assertIn("inputs.mode == 'verify'", verify['if'])
            self.assertEqual(verify['permissions'], {'contents': 'read', 'actions': 'read', 'attestations': 'read'})
            self.assertNotIn('KOMIZO_DEPLOY_KEY', verify.get('secrets', {}))
        self.assertIn('step-timeouts:',files['.github/workflows/security.yml'])
        backup=read_yaml(files['.github/workflows/backup.yml'])
        self.assertEqual(backup['permissions'],{})
        self.assertEqual(backup['jobs']['backup']['permissions'],{
            'contents':'read','actions':'read','issues':'write','id-token':'write','attestations':'read'})
        with patch.object(operations,'workflow_files',return_value={'.github/workflows/build-and-test.yml':'overwrite'}):
            with self.assertRaisesRegex(ValueError,'one native lifecycle owner'):
                generate.workflow_files(self.root,'b'*40)
        self.data['production']['checks']=[]
        self.write()
        with self.assertRaises(ValueError):
            configuration.load(self.root)

    def test_artifact_security_selects_only_real_unique_named_assertions(self):
        self.config['tests']={'integration':[dict(runner='python',path='deploy/health.py',name='native-artifact')]}
        self.data['security'].update(artifact_validation=True, artifact_checks=['native-artifact'])
        self.declaration()
        original=copy.deepcopy(self.data['security'])
        for changes in [dict(artifact_validation=False),dict(artifact_validation='true'),
                        dict(artifact_checks=['missing']),dict(artifact_checks=['native-artifact']*2)]:
            self.data['security']=original | changes
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                self.declaration()

    def test_source_only_read_only_audit_cannot_gain_registry_or_issue_capabilities(self):
        self.data['security'].pop('deployed_policy')
        self.data['security']['report']=False
        self.write()
        text=operations.workflow_files(self.root,'b'*40,self.config)['.github/workflows/security.yml']
        self.assertIn('audit-source.yml@',text)
        for capability in ['packages: read','deployments: read']:
            self.assertNotIn(capability,text)
        self.assertIn('      issues: write\n',text)
        self.assertNotIn('report:',text)

    def test_native_playwright_preserves_its_real_package_configuration(self):
        package=self.root/'app';package.mkdir()
        (package/'package.json').write_text('{}\n')
        (package/'playwright.config.ts').write_text('export default {};\n')
        self.config['node']={'packages':[{'directory':'app','manager':'bun'}]}
        check=dict(runner='playwright',path='app/playwright.config.ts',cwd='app',manager='bun',timeout=1200)
        self.data['production']['checks'].append(check)
        self.declaration()
        for changes in [dict(cwd='deploy'),dict(manager='npm'),dict(path='deploy/health.py')]:
            original=copy.deepcopy(check);check.update(changes)
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                self.declaration()
            check.clear();check.update(original)


if __name__ == '__main__':
    unittest.main()
