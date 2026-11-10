"""Native build import and validation must share the final recipe environment."""
from source_fixture import SDK_ROOT
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import ci


class HostImportEnvironment(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ci-import-environment-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.expected = str(self.root / '.artifacts/godot-data')
        self.source, self.sdk = 'a' * 40, 'b' * 40
        self.events = []
        self.config = {'recipe': 'godot-game'}

    def configured(self, root, config):
        self.assertEqual(root, self.root)
        self.assertIs(config, self.config)
        self.assertEqual(os.environ['COOKBOOK_STAGE'], 'full')
        os.environ['XDG_DATA_HOME'] = self.expected
        self.events.append('configured')

    def prepare(self, root, config, source, sdk):
        self.assertEqual(os.environ['XDG_DATA_HOME'], self.expected,
                         'native image import selected an inherited engine store')
        self.events.append('import')
        os.environ['COOKBOOK_GODOT_HOST'] = str(root / 'matrix.json')
        return {'source': source}

    def revalidate(self, root, config, matrix, environment):
        self.assertEqual(environment['XDG_DATA_HOME'], self.expected)
        self.assertEqual(environment['COOKBOOK_GODOT_HOST'], str(root / 'matrix.json'))
        self.assertEqual(matrix, {'source': self.source})
        self.events.append('revalidate')

    def command(self, args, *, cwd, env, check):
        self.assertEqual(env['XDG_DATA_HOME'], self.expected)
        self.assertEqual(env['COOKBOOK_GODOT_HOST'], str(self.root / 'matrix.json'))
        self.events.append(args[-1])
        return SimpleNamespace(returncode=0)

    def contexts(self, matrix):
        stack = ExitStack()
        stack.enter_context(patch.dict(os.environ, {'XDG_DATA_HOME': str(self.root / 'inherited'),
                                                  'GITHUB_OUTPUT': ''}, clear=False))
        stack.enter_context(patch.object(ci, 'load', return_value=self.config))
        stack.enter_context(patch.object(ci.orchestration, 'model', return_value={'route': 'godot'}))
        stack.enter_context(patch.object(ci, 'identity', return_value='c' * 40))
        stack.enter_context(patch.object(ci.recipe, 'configure_environment', side_effect=self.configured))
        stack.enter_context(patch.object(ci.orchestration, 'matrix_module', return_value=matrix))
        stack.enter_context(patch.object(ci.recipe, 'adapter', return_value=SimpleNamespace(artifact_paths=lambda *_: [])))
        stack.enter_context(patch.object(ci, 'proof', return_value={'accepted': True}))
        stack.enter_context(patch.object(ci.subprocess, 'run', side_effect=self.command))
        return stack

    def test_full_host_import_setup_revalidation_and_tests_use_the_final_environment(self):
        matrix = SimpleNamespace(prepare_host=self.prepare, revalidate_host_build=self.revalidate,
                                 finalize_host=lambda accepted, _: accepted)
        with self.contexts(matrix):
            ci.run(self.root, 'full', self.source, self.sdk)
        self.assertEqual(self.events, ['configured', 'import', 'setup', 'revalidate', 'test'])
        self.assertEqual(json.loads((self.root / '.artifacts/cookbook-validation.json').read_text()), {'accepted': True})

    def test_configuration_refusal_prevents_any_image_import_or_setup(self):
        matrix = SimpleNamespace(prepare_host=self.prepare)
        with self.contexts(matrix), patch.object(ci.recipe, 'configure_environment', side_effect=ValueError('invalid recipe environment')):
            with self.assertRaisesRegex(ValueError, 'invalid recipe environment'):
                ci.run(self.root, 'full', self.source, self.sdk)
        self.assertEqual(self.events, [])
        self.assertFalse((self.root / '.artifacts/cookbook-validation.json').exists())


if __name__ == '__main__':
    unittest.main()
