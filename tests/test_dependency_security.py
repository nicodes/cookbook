from source_fixture import TOOLS_ROOT
from source_fixture import SDK_ROOT, parser_root, write_entrypoint
"""Independent frozen source audits cannot become application builds or receipts."""
from contextlib import chdir
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = SDK_ROOT
sys.path.insert(0, str(TOOLS / 'cookbook'))
import configuration
import dependency_security as gate
import delivery_workflows
import generate
import source_policy
from source_fixture import read_source, write_source, dump_source


def read_yaml(body):
    parser = source_policy.interpreter(parser_root())
    return json.loads(subprocess.check_output([parser, '-I', '-c',
        'import json,sys,yaml;print(json.dumps(yaml.safe_load(sys.stdin.read())))'],
        input=body, text=True, timeout=30))


class DependencySecurityControls(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'app').mkdir()
        (self.root / 'app/package.json').write_text(json.dumps({'name': 'dependency-fixture', 'version': '1.0.0',
            'scripts': {'postinstall': 'node -e "require(\'fs\').writeFileSync(\'app-build-ran\',\'unexpected\')"'}}))
        (self.root / 'app/package-lock.json').write_text(json.dumps({'name': 'dependency-fixture', 'version': '1.0.0',
            'lockfileVersion': 3, 'requires': True, 'packages': {'': {'name': 'dependency-fixture', 'version': '1.0.0', 'hasInstallScript': True}}}))
        for name in ['api', 'tests/native-fixture']:
            (self.root / name).mkdir(parents=True, exist_ok=True)
            (self.root / name / 'Cargo.toml').write_text('[package]\nname="audit-fixture"\nversion="0.1.0"\n')
            (self.root / name / 'Cargo.lock').write_text('version=4\n[[package]]\nname="fixture-dependency"\nversion="1.0.0"\n')
        (self.root / 'cookbook.toml').write_text('version=1\nrecipe="react-native"\n')
        (self.root / 'mise.toml').write_text('[tools]\npython="3.13.11"\nnode="26.7.0"\ntrivy="0.74.0"\n')
        self.policy = {'runner': 'blacksmith-4vcpu-ubuntu-2404', 'timeout_minutes': 10, 'setup_timeout_minutes': 5}
        self.config = {'recipe': 'react-native', 'ci': {'dependency_security': self.policy},
            'components': {'rust': [{'manifest': 'api/Cargo.toml'}]},
            'security': {'rust_inventory_manifests': ['api/Cargo.toml', 'tests/native-fixture/Cargo.toml']},
            'node': {'packages': [{'directory': 'app', 'manager': 'npm'}]}}
        self.calls = []; self.severity = None; self.omit_inventory = False

    def execute(self, command, root, environment):
        self.calls.append((command, root))
        if command[:2] == ['trivy', 'fs']:
            target = Path(command[-1])
            self.assertEqual({path.relative_to(target).as_posix() for path in target.rglob('*') if path.is_file()},
                {'0/Cargo.lock', '1/Cargo.lock'})
            results = [{'Type': 'cargo', 'Target': f'{index}/Cargo.lock', 'Packages': [{'Name': 'fixture-dependency', 'Version': '1.0.0'}],
                'Vulnerabilities': [] if self.severity is None else [{'Severity': self.severity}]} for index in range(2)]
            if self.omit_inventory: results.pop()
            Path(command[command.index('--output') + 1]).write_text(json.dumps({'SchemaVersion': 2, 'Results': results}))

    def prepare(self):
        with chdir(self.root): gate.setup(self.root, self.config, dict(os.environ), self.execute)

    def test_real_frozen_install_never_executes_application_lifecycle_or_builds(self):
        if not shutil.which('npm'): self.skipTest('prepare the declared native npm SDK')
        before = (self.root / 'app/package-lock.json').read_bytes()
        def native(command, root, environment):
            self.calls.append((command, root))
            if command[:2] != ['mise', 'install']:
                subprocess.run(command, cwd=root, env=environment, check=True, timeout=30, capture_output=True)
        with chdir(self.root): gate.setup(self.root, self.config, dict(os.environ), native)
        self.assertFalse((self.root / 'app/app-build-ran').exists())
        self.assertEqual((self.root / 'app/package-lock.json').read_bytes(), before)
        installed = self.calls[0][0]
        self.assertEqual(installed, ['mise', 'install', 'node', 'python', 'trivy'])
        self.assertNotIn('rust', installed)
        self.assertIn('--ignore-scripts', self.calls[1][0]); self.assertIn('ci', self.calls[1][0])
        self.assertFalse((self.root / '.artifacts/verification/build.json').exists())
        self.assertFalse((self.root / '.artifacts/cookbook-validation.json').exists())

    def test_audit_uses_all_owning_locks_and_fixed_severity_without_installs_or_images(self):
        self.prepare(); self.calls.clear()
        gate.audit(self.root, self.config, dict(os.environ), self.execute)
        self.assertEqual(self.calls[0][0], ['npm', 'audit', '--audit-level=high'])
        scanner = self.calls[1][0]
        self.assertIn('--include-dev-deps', scanner); self.assertIn('--list-all-pkgs', scanner)
        self.assertEqual(scanner[:2], ['trivy', 'fs'])
        self.assertNotIn('image', scanner)
        self.assertTrue(all(command[0] not in {'make', 'cargo', 'docker', 'mise'} and 'install' not in command
                            for command, _ in self.calls))
        for severity in ['HIGH', 'CRITICAL']:
            self.severity = severity
            with self.subTest(severity=severity), self.assertRaisesRegex(ValueError, 'High/Critical'):
                gate.audit(self.root, self.config, dict(os.environ), self.execute)
        self.severity = 'MEDIUM'; gate.audit(self.root, self.config, dict(os.environ), self.execute)
        self.omit_inventory = True
        with self.assertRaisesRegex(ValueError, 'every declared'):
            gate.audit(self.root, self.config, dict(os.environ), self.execute)

    def test_missing_preparation_stale_lock_or_mutating_installer_refuses_without_audit(self):
        with self.assertRaises(ValueError): gate.audit(self.root, self.config, {}, self.execute)
        self.assertEqual(self.calls, [])
        self.prepare(); self.calls.clear()
        lock = self.root / 'app/package-lock.json'; before = lock.read_bytes(); lock.write_bytes(before + b'\n')
        with self.assertRaisesRegex(ValueError, 'not prepared'): gate.audit(self.root, self.config, {}, self.execute)
        self.assertEqual(self.calls, []); lock.write_bytes(before)
        (self.root / gate.RECEIPT).unlink()
        def mutation(command, root, environment):
            if command[0] == 'npm': lock.write_bytes(before + b'\n')
        with chdir(self.root), self.assertRaisesRegex(ValueError, 'changed frozen'):
            gate.setup(self.root, self.config, {}, mutation)
        self.assertFalse((self.root / gate.RECEIPT).exists())

    def test_audit_failure_propagates_and_noop_missing_lock_or_pin_refuses(self):
        self.prepare()
        with self.assertRaises(subprocess.CalledProcessError):
            gate.audit(self.root, self.config, {}, lambda argv, *_: (_ for _ in ()).throw(subprocess.CalledProcessError(1, argv)))
        empty = {'ci': {'dependency_security': self.policy}}
        with self.assertRaisesRegex(ValueError, 'real declared'): gate.dependencies(self.root, empty)
        (self.root / 'app/package-lock.json').unlink()
        with self.assertRaisesRegex(ValueError, 'frozen native'): gate.dependencies(self.root, self.config)
        self.config['node']['packages'] = []
        (self.root / 'mise.toml').write_text('[tools]\npython="3.13.11"\n')
        with self.assertRaisesRegex(ValueError, 'caller Mise pins'): gate.dependencies(self.root, self.config)

    def test_closed_optional_policy_and_source_owned_native_limits(self):
        self.assertIsNone(gate.declaration({}))
        for changes in [{'runner': 'self-hosted'}, {'runner': []}, {'timeout_minutes': True},
                        {'setup_timeout_minutes': 11}, {'build': True}, {'secrets': {}}]:
            invalid = copy.deepcopy(self.config); invalid['ci']['dependency_security'].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError): configuration.ci_settings(invalid)
        self.assertEqual(gate.declaration(self.config), self.policy)

    def test_entire_derived_sdk_subset_refuses_floating_or_nonliteral_pins_before_install(self):
        self.config['node']['packages'].append({'directory': 'app', 'manager': 'bun'})
        (self.root / 'app/bun.lock').write_text('{}\n')
        versions = {'python': '3.13.11', 'node': '26.7.0', 'bun': '1.4.1', 'trivy': '0.74.0'}
        for selected in versions:
            for invalid in ['"latest"', '"1.2"', '{version="1.2.3"}', 'true', '["1.2.3"]']:
                (self.root / 'mise.toml').write_text('[tools]\n' + '\n'.join(
                    name + '=' + (invalid if name == selected else json.dumps(version)) for name, version in versions.items()))
                with self.subTest(tool=selected, invalid=invalid), chdir(self.root), self.assertRaisesRegex(ValueError, 'literal exact numeric'):
                    gate.setup(self.root, self.config, {}, self.execute)
                self.assertEqual(self.calls, [])

    def test_actual_git_source_sdk_and_native_runner_deadlines_are_admitted_before_effects(self):
        sdk = self.root / 'sdk-fixture'; sdk.mkdir(); (sdk / 'entry.py').write_text('trusted = True\n')
        (sdk / 'helpers').mkdir()
        shutil.copyfile(TOOLS_ROOT / 'helpers/select-tools.py', sdk / 'helpers/select-tools.py')
        (self.root / '.gitignore').write_text('sdk-fixture/\n.artifacts/\n')
        def commit(root):
            for args in [['init', '-q'], ['add', '.'], ['-c', 'user.name=Fixture', '-c',
                    'user.email=fixture@example.test', 'commit', '-qm', 'frozen inputs']]:
                subprocess.run(['git', '-C', str(root), *args], check=True, stdout=subprocess.DEVNULL)
            return subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
        subprocess.run(['git', '-C', str(sdk), 'init', '-q'], check=True)
        subprocess.run(['git', '-C', str(sdk), 'remote', 'add', 'origin', 'https://github.com/nicodes/cookbook.git'], check=True)
        tools = commit(sdk)
        (self.root / 'tests/unit.py').write_text('raise RuntimeError("admission must not execute product code")\n')
        # The Godot family owns the existing explicit test-fixture inventory;
        # retain both exact locks while preparing a closed source declaration.
        self.config.update(version=1, recipe='godot-game', tests={'unit':[{'runner':'python','path':'tests/unit.py'}]})
        self.config['ci']['godot']={'unit_shards':0}
        write_source(self.root / 'cookbook.toml', self.config)
        write_entrypoint(self.root, tools)
        source = commit(self.root)
        with patch.object(gate, 'COOKBOOK', sdk):
            gate.admit(self.root, self.config, source, tools, self.policy['runner'], 10, 5)
            for changes in [{'source': 'a'*40}, {'tools': 'b'*40}, {'runner': 'ubuntu-24.04'}, {'timeout': 11}, {'setup_timeout': 6}]:
                values = dict(source=source, tools=tools, runner=self.policy['runner'], timeout=10, setup_timeout=5) | changes
                with self.subTest(changes=changes), self.assertRaises(ValueError): gate.admit(self.root, self.config, **values)
            (sdk / 'entry.py').write_text('changed = True\n')
            with self.assertRaisesRegex(ValueError, 'exact clean'): gate.admit(self.root, self.config, source, tools, self.policy['runner'], 10, 5)
        self.assertEqual(self.calls, [])

    def rendering(self, combined=False):
        (self.root / 'tests/unit.py').write_text('assert 1+1==2\n')
        data = {'version': 1, 'project': 'fixture', 'komizo_ref': 'a'*40,
            'publish_runner': 'ubuntu-24.04', 'publish_timeout': 15,
            'deploy_runner': 'ubuntu-24.04', 'deploy_timeout': 15 if combined else 100,
            'health_urls': ['https://example.test/readyz'], 'notification_url': 'https://example.test/',
            'combined': combined}
        write_source(self.root / 'delivery.toml', data)
        (self.root / 'cookbook.toml').write_text('''version=1
recipe="react-native"
[ci.dependency_security]
runner="blacksmith-4vcpu-ubuntu-2404"
timeout_minutes=10
setup_timeout_minutes=5
[node]
packages=[{directory="app",manager="npm"}]
[delivery]
definition="delivery.toml"
[[tests.unit]]
runner="python"
path="tests/unit.py"
''')
        return generate.workflow_files(self.root, 'b'*40)

    def test_canonical_independent_pr_and_main_gates_bound_source_publish_and_combined_delivery(self):
        for combined in [False, True]:
            files = self.rendering(combined)
            ci, cd = (read_yaml(files['.github/workflows/' + name]) for name in ['ci.yml', 'cd.yml'])
            self.assertIn('pull_request', ci['on']); self.assertNotIn('push', ci['on'])
            self.assertIn('push', cd['on']); self.assertNotIn('pull_request', cd['on'])
            for workflow in [ci, cd]:
                job = workflow['jobs']['dependency-security']
                self.assertNotIn('needs', job)
                self.assertEqual(job['permissions'], {'contents': 'read', 'attestations': 'read'})
                self.assertEqual(job['with']['runner'], self.policy['runner'])
                self.assertEqual(job['with']['timeout-minutes'], 10)
                self.assertEqual(job['with']['setup-timeout-minutes'], 5)
                self.assertEqual(job['with']['source'], '${{ inputs.source || github.sha }}')
            first_writer = cd['jobs']['deploy' if combined else 'publish']
            self.assertEqual(first_writer['needs'], ['validate', 'dependency-security'])
            self.assertEqual(generate.workflow_files(self.root, 'b'*40), files)
        source = (self.root / 'cookbook.toml').read_text()
        source = source.replace('[ci.dependency_security]\nrunner="blacksmith-4vcpu-ubuntu-2404"\ntimeout_minutes=10\nsetup_timeout_minutes=5\n', '')
        (self.root / 'cookbook.toml').write_text(source)
        files = generate.workflow_files(self.root, 'b'*40)
        self.assertNotIn('dependency-security:', files['.github/workflows/ci.yml'])
        self.assertEqual(read_yaml(files['.github/workflows/cd.yml'])['jobs']['deploy']['needs'], 'validate')

    def test_leaf_has_only_readonly_source_steps_and_no_validation_or_host_capabilities(self):
        leaf = read_yaml((TOOLS / '.github/workflows/cookbook-dependency-security.yml').read_text())
        job = leaf['jobs']['audit']
        self.assertEqual(job['permissions'], {'contents': 'read', 'attestations': 'read'})
        self.assertEqual(job['timeout-minutes'], "${{ inputs['timeout-minutes'] }}")
        action = read_yaml((TOOLS / 'dependency-security/action.yml').read_text())
        commands = '\n'.join(step.get('run', '') for step in [*job['steps'], *action['runs']['steps']])
        self.assertIn('dependency_security.py" setup', commands); self.assertIn('dependency_security.py audit', commands)
        for absent in ['make ', 'docker ', 'build', 'release.py', 'publish', 'secrets.', 'upload-artifact']:
            self.assertNotIn(absent, commands)
        self.assertNotIn('environment', job)

    def test_one_outer_setup_deadline_covers_bootstrap_admission_and_every_sdk_install(self):
        leaf = read_yaml((TOOLS / '.github/workflows/cookbook-dependency-security.yml').read_text())
        steps = leaf['jobs']['audit']['steps']
        bounded = [step for step in steps if 'timeout-minutes' in step]
        self.assertEqual(len(bounded), 1)
        self.assertEqual(bounded[0]['uses'], './.artifacts/recipe-sdks/_cookbook/dependency-security')
        self.assertEqual(bounded[0]['timeout-minutes'], "${{ inputs['setup-timeout-minutes'] }}")
        action = read_yaml((TOOLS / 'dependency-security/action.yml').read_text())
        nested = action['runs']['steps']
        self.assertFalse(any('timeout-minutes' in step for step in nested))
        install = next(index for index, step in enumerate(nested) if step.get('uses', '').startswith('jdx/mise-action@'))
        admit = next(index for index, step in enumerate(nested) if step.get('id') == 'admit')
        frozen = next(index for index, step in enumerate(nested) if 'dependency_security.py" setup' in step.get('run', ''))
        self.assertLess(admit, install); self.assertLess(install, frozen)
        self.assertEqual(nested[install]['with']['install_args'], '${{ steps.admit.outputs.tools }}')

    def test_native_delivery_also_requires_the_independent_source_gate(self):
        data=dict(version=1,project='fixture',komizo_ref='a'*40,
            publish_runner='ubuntu-24.04',deploy_runner='ubuntu-24.04',publish_timeout=15,deploy_timeout=15,
            health_urls=['https://example.test/readyz'],notification_url='https://example.test/')
        write_source(self.root/'delivery.toml',data)
        config=copy.deepcopy(self.config);config['delivery']={'definition':'delivery.toml'}
        jobs=read_yaml(delivery_workflows.jobs(self.root,config,'b'*40))
        self.assertEqual(jobs['publish']['needs'],['validate','dependency-security'])
        self.assertIn('cookbook-publish.yml@'+'a'*40,jobs['publish']['uses'])
        self.assertEqual(jobs['deploy']['needs'],'publish')
        del config['ci']['dependency_security']
        original=read_yaml(delivery_workflows.jobs(self.root,config,'b'*40))
        self.assertEqual(original['publish']['needs'],'validate')


if __name__ == '__main__': unittest.main()
