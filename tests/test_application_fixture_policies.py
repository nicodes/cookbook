from source_fixture import SDK_ROOT
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import application_fixtures
import buildkit


class IntrinsicApplicationFixtures(unittest.TestCase):
    def test_cache_release_never_reaches_local_default_or_unowned_builders(self):
        calls = []
        capture = lambda *args: calls.append(args)
        buildkit.release_cache(Path('.'), {'BUILDX_BUILDER':'default'}, capture)
        self.assertEqual(calls, [])
        for builder in ['', 'default', 'production', 'builder-ABCDEF00-0000-0000-0000-000000000001']:
            with self.subTest(builder=builder), self.assertRaises(ValueError):
                buildkit.release_cache(Path('.'), {'GITHUB_ACTIONS':'true', 'BUILDX_BUILDER':builder}, capture)
        self.assertEqual(calls, [])
        builder='builder-00000000-0000-0000-0000-000000000001'
        buildkit.release_cache(Path('.'), {'GITHUB_ACTIONS':'true', 'BUILDX_BUILDER':builder}, capture)
        self.assertEqual(calls[0][0], ['docker','buildx','prune','--builder',builder,'--all','--force'])

    def test_recovery_is_disposable_exact_engine_and_preserves_existing_pull_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'deploy').mkdir()
            compose=root/'deploy/compose.yml'
            image='postgres@sha256:'+'a'*64
            compose.write_text('services:\n  postgres:\n    image: '+image+'\n')
            config={'components':{'recovery_fixture':{'pull_timeout':300,'timeout':900}}}
            calls=[]
            with patch.object(application_fixtures.images,'definition',return_value={'project':'fixture'}):
                self.assertTrue(application_fixtures.recovery(root, config, {}, lambda *args:calls.append(args)))
            self.assertEqual(calls[0][0], ['docker','pull',image])
            self.assertEqual(calls[0][-1], 300)
            self.assertEqual(calls[1][0][-4:], ['--project','fixture','--engine',image])
            compose.write_text('services:\n  postgres:\n    image: postgres:latest\n')
            with self.assertRaises(ValueError):
                application_fixtures.recovery(root, config, {}, lambda *args:self.fail('floating fixture ran'))
