"""Refuse drift between operation names, generated callers and workflow paths."""
from pathlib import Path
import tempfile
import unittest

import workflow_names


class WorkflowNames(unittest.TestCase):
    def test_operation_title_determines_the_only_filename(self):
        for title, expected in [('Build and test', 'build-and-test.yml'),
                                ('Build and test (Godot)', 'build-and-test-godot.yml'),
                                ('Release addon (API key)', 'release-addon-api-key.yml'),
                                ('Merge PR', 'merge-pr.yml')]:
            with self.subTest(title=title):
                self.assertEqual(workflow_names.filename(title), expected)
                workflow_names.check({'.github/workflows/' + expected: 'name: ' + title + '\n'})

    def test_mismatched_and_nonliteral_titles_refuse_with_a_useful_error(self):
        with self.assertRaisesRegex(ValueError, 'expected build-and-test.yml'):
            workflow_names.check({'ci.yml': 'name: Build and test\n'})
        for source in ['name: Build & Test\n', 'name: ${{ inputs.title }}\n',
                       'name: Build and test\nname: Deploy\n', 'jobs: {}\n']:
            with self.subTest(source=source), self.assertRaises(ValueError):
                workflow_names.check({'build-and-test.yml': source})

    def test_source_ownership_finds_all_obsolete_generated_paths_without_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflows = root / '.github/workflows'
            workflows.mkdir(parents=True)
            for name, source in [('arbitrary-old.yml', workflow_names.GENERATED + '\nname: Deploy\n'),
                                 ('deploy.yml', workflow_names.GENERATED + '\nname: Deploy\n'),
                                 ('custom.yml', 'name: Custom\n')]:
                (workflows / name).write_text(source)
            self.assertEqual(workflow_names.obsolete_generated(root, {'.github/workflows/deploy.yml': ''}),
                             ['.github/workflows/arbitrary-old.yml'])


if __name__ == '__main__':
    unittest.main()
