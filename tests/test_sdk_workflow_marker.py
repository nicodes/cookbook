"""The common outer engine marker never mutates either immutable SDK root."""
from source_fixture import SDK_ROOT
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import sdk_source
import seed_transport
import workflow_bootstrap


class WorkflowSDKMarker(unittest.TestCase):
    def commit(self, directory, repository):
        for arguments in [('init', '-q'), ('remote', 'add', 'origin', 'https://github.com/' + repository + '.git'),
                ('add', '.'), ('-c', 'user.name=SDK marker control', '-c', 'user.email=control@example.invalid',
                             'commit', '-qm', 'finite source fixture')]:
            subprocess.run(['git', '-C', str(directory), *arguments], check=True)
        return sdk_source.git(directory, 'rev-parse', 'HEAD')

    def test_actual_common_outer_marker_preserves_both_real_git_sources_and_is_idempotent(self):
        with tempfile.TemporaryDirectory(prefix='SDK outer marker ') as directory:
            root = Path(directory)
            outer = root / '.artifacts/recipe-sdks'
            cookbook, tools = outer / '_cookbook', outer / '_tools'
            (tools / 'helpers').mkdir(parents=True)
            (tools / 'helpers/never_execute.py').write_text('raise RuntimeError("source data must not execute")\n')
            tools_ref = self.commit(tools, 'nicodes/tools')
            (cookbook / 'sdk-dependencies').mkdir(parents=True)
            (cookbook / 'sdk-dependencies/action.yml').write_bytes((SDK_ROOT / 'sdk-dependencies/action.yml').read_bytes())
            (cookbook / 'mise.toml').write_text('[vars]\ntools_contract=1\ntools_ref="' + tools_ref + '"\n')
            cookbook_ref = self.commit(cookbook, 'nicodes/cookbook')
            before = [sdk_source.git(source, 'rev-parse', 'HEAD^{tree}') for source in [cookbook, tools]]
            output = root / 'output.txt'
            source = seed_transport.seed_transport(workflow_bootstrap.SEED, encoded=workflow_bootstrap.ENCODED_SEED,
                length=workflow_bootstrap.SEED_LENGTH, sha256=workflow_bootstrap.SEED_SHA256)
            environment = {**os.environ, 'GITHUB_ACTION_PATH': str(cookbook / 'sdk-dependencies'),
                           'GITHUB_OUTPUT': str(output)}
            for _ in range(2):
                subprocess.run([sys.executable, '-I', '-B', '-c', source, cookbook_ref],
                    cwd=root, env=environment, check=True, capture_output=True, text=True, timeout=30)
                self.assertEqual((outer / '.gdignore').read_bytes(), b'')
                for path, revision, repository in [(cookbook, cookbook_ref, 'nicodes/cookbook'),
                                                   (tools, tools_ref, 'nicodes/tools')]:
                    self.assertFalse((path / '.gdignore').exists())
                    self.assertEqual(sdk_source.verify(path, revision, repository=repository), path)
                    self.assertEqual(sdk_source.git(path, 'status', '--porcelain', '--untracked-files=all', '--ignored=matching'), '')
                self.assertEqual([sdk_source.git(path, 'rev-parse', 'HEAD^{tree}') for path in [cookbook, tools]], before)
            self.assertEqual(output.read_text().splitlines(), ['tools-ref=' + tools_ref] * 2)

    def test_all_four_godot_and_three_native_sites_use_only_the_shared_outer_marker(self):
        for filename, count in [('cookbook-godot.yml', 4), ('cookbook-native.yml', 3)]:
            source = (SDK_ROOT / '.github/workflows' / filename).read_text()
            with self.subTest(filename=filename):
                self.assertEqual(source.count('uses: ./.artifacts/recipe-sdks/_cookbook/sdk-dependencies'), count)
                self.assertNotIn('.gdignore', source)
                self.assertNotIn("'/_cookbook/", source)


if __name__ == '__main__':
    unittest.main()
