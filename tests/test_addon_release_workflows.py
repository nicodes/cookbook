"""Validate the permission ceilings GitHub applies before evaluating job conditions."""
from source_fixture import SDK_ROOT
import copy
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import addon_release_workflows
import publication_workflows
from test_transition_ci_dag import read_yaml


def require_permission_ceiling(caller, workflow):
    maximum = caller['permissions']
    levels = {'none': 0, 'read': 1, 'write': 2}
    for name, job in workflow['jobs'].items():
        # GitHub checks even jobs that cannot run under the selected auth policy.
        for scope, permission in job.get('permissions', workflow.get('permissions', {})).items():
            if levels[permission] > levels[maximum.get(scope, 'none')]:
                raise AssertionError(name + ' requests ' + scope + ' beyond its caller')


class AddonReleaseWorkflows(unittest.TestCase):
    def caller(self, authentication):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / 'addon').mkdir()
        (root / 'addon/plugin.cfg').write_text('[plugin]\nversion="1.0.0"\n')
        (root / 'cookbook.toml').write_text('version=1\nrecipe="godot-addon"\n'
            '[package]\narchive="dist/@owner_addon.zip"\n[publication]\n'
            'tag_prefix="v"\nregistry_auth="' + authentication + '"\n')
        files = publication_workflows.workflow_files(root, 'a' * 40)
        return root, files, read_yaml(files['.github/workflows/release.yml'])['jobs']['release']

    def test_committed_auth_workflows_match_their_single_canonical_source(self):
        self.assertFalse((SDK_ROOT / '.github/workflows/cookbook-addon-release.yml').exists())
        for name, expected in addon_release_workflows.workflow_files().items():
            self.assertEqual((SDK_ROOT / name).read_text(), expected)

    def test_actual_callers_and_all_declared_jobs_fit_selected_auth_permissions(self):
        for authentication in ['api-key', 'oidc']:
            with self.subTest(authentication=authentication):
                _, _, caller = self.caller(authentication)
                path = addon_release_workflows.workflow_path(authentication)
                self.assertEqual(caller['uses'], 'nicodes/cookbook/' + path + '@' + 'a' * 40)
                shared = read_yaml((SDK_ROOT / path).read_text())
                self.assertEqual(set(shared['jobs']), {'plan', 'validate', 'publish', 'publish-environment'})
                require_permission_ceiling(caller, shared)
                for job in ['publish', 'publish-environment']:
                    self.assertIn("registry_auth == '" + authentication + "'", shared['jobs'][job]['if'])
                plan = shared['jobs']['plan']['steps'][-1]
                self.assertEqual(plan['env']['EXPECTED_AUTH'], authentication)
                self.assertIn('test "$PLANNED_AUTH" = "$EXPECTED_AUTH"', plan['run'])
                if authentication == 'api-key':
                    self.assertNotIn('id-token', caller['permissions'])
                    for job in shared['jobs'].values():
                        self.assertNotIn('id-token', job['permissions'])
                else:
                    self.assertEqual(caller['permissions']['id-token'], 'write')
                    self.assertNotIn('secrets', shared['on']['workflow_call'])
                    for job in ['publish', 'publish-environment']:
                        self.assertEqual(shared['jobs'][job]['permissions']['id-token'], 'write')
                        self.assertNotIn('api-key', shared['jobs'][job]['steps'][-1]['with'])

    def test_skipped_oidc_job_still_reproduces_github_permission_refusal_for_api_key(self):
        _, _, caller = self.caller('api-key')
        workflow = read_yaml((SDK_ROOT / addon_release_workflows.workflow_path('api-key')).read_text())
        require_permission_ceiling(caller, workflow)
        adversarial = copy.deepcopy(workflow)
        adversarial['jobs']['unused-oidc'] = {'if': 'false', 'permissions': {'id-token': 'write'}}
        with self.assertRaisesRegex(AssertionError, 'unused-oidc requests id-token'):
            require_permission_ceiling(caller, adversarial)

    def test_actual_generated_callers_pass_actionlint_for_both_auth_modes(self):
        executable = shutil.which('actionlint')
        self.assertIsNotNone(executable, 'the source generator check requires the pinned actionlint SDK')
        for authentication in ['api-key', 'oidc']:
            with self.subTest(authentication=authentication):
                root, files, _ = self.caller(authentication)
                for name, text in files.items():
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text)
                result = subprocess.run([executable, *files], cwd=root, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
