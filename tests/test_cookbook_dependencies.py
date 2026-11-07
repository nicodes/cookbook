"""Separate provider identities and source-checked generic helper imports."""
from source_fixture import SDK_ROOT
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import dependencies


class CookbookDependencies(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='two SDK identities ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.provider = self.root / 'cookbook'
        self.provider.mkdir()
        self.tools = self.root / 'tools'
        (self.tools / 'helpers').mkdir(parents=True)
        (self.tools / 'helpers/finite_fixture.py').write_text('VALUE = 17\n')
        self.git('init', '-q')
        self.git('remote', 'add', 'origin', 'https://github.com/nicodes/tools.git')
        self.git('add', '.')
        self.git('-c', 'user.name=SDK fixture', '-c', 'user.email=fixture@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '-qm', 'finite generic source')
        self.revision = self.git('rev-parse', 'HEAD')
        (self.provider / 'mise.toml').write_text('[vars]\ntools_contract=1\ntools_ref="' + self.revision + '"\n')
        self.context = patch.object(dependencies, 'ROOT', self.provider)
        self.context.start()
        self.addCleanup(self.context.stop)
        self.environment = patch.dict(os.environ, {'TOOLS_ROOT': str(self.tools)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(sys.modules.pop, 'cookbook_tools_finite_fixture', None)
        self.addCleanup(dependencies._LOADED_HELPERS.pop, 'cookbook_tools_finite_fixture', None)

    def git(self, *arguments):
        return subprocess.check_output(['git', '-c', 'core.hooksPath=' + os.devnull, '-C', str(self.tools),
                                        *arguments], text=True, stderr=subprocess.PIPE).strip()

    def test_literal_provider_dependency_and_supplied_location_keep_tools_identity(self):
        self.assertEqual(dependencies.tools_identity(), {'repository': 'nicodes/tools', 'revision': self.revision})
        self.assertEqual(dependencies.tools_root(), self.tools)
        self.assertEqual(dependencies.tools_root(self.tools), self.tools)
        self.assertEqual(dependencies.helper('finite_fixture').VALUE, 17)

    def test_foreign_origin_or_dirty_source_cannot_execute_a_new_helper(self):
        for mutation in ['foreign', 'dirty']:
            if mutation == 'foreign':
                self.git('remote', 'set-url', 'origin', 'https://github.com/nicodes/cookbook.git')
            else:
                self.git('remote', 'set-url', 'origin', 'https://github.com/nicodes/tools.git')
                (self.tools / 'helpers/finite_fixture.py').write_text('raise RuntimeError("must never load")\n')
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                dependencies.helper('finite_fixture')
        self.assertNotIn('cookbook_tools_finite_fixture', sys.modules)

    def test_cached_module_must_be_the_actual_source_checked_object(self):
        actual = dependencies.helper('finite_fixture')
        self.assertIs(dependencies.helper('finite_fixture'), actual)
        foreign = types.ModuleType('cookbook_tools_finite_fixture')
        foreign.__file__ = actual.__file__
        with patch.dict(sys.modules, {'cookbook_tools_finite_fixture': foreign}), self.assertRaises(ValueError):
            dependencies.helper('finite_fixture')

    def test_explicit_cross_version_evidence_is_data_only_and_refuses_identity_aliases(self):
        with patch.object(dependencies, 'tools_identity', side_effect=AssertionError('read own template dependency')):
            record = dependencies.binding('c' * 40, 'd' * 40)
            self.assertEqual(dependencies.validate_record(record), record)
            self.assertEqual(dependencies.validate_binding(record, 'c' * 40, 'd' * 40), record)
        for change in [{'cookbook_repository': 'nicodes/tools'}, {'tools_repository': 'nicodes/cookbook'},
                       {'tools_commit': 'e' * 40}, {'entrypoint_contract': 'make-v0'}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                dependencies.validate_binding(record | change, 'c' * 40, 'd' * 40)
        for key in dependencies.BINDING_KEYS:
            incomplete = {name: value for name, value in record.items() if name != key}
            with self.subTest(missing=key), self.assertRaises(ValueError):
                dependencies.validate_record(incomplete)


if __name__ == '__main__':
    unittest.main()
