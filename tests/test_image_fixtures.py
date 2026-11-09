from source_fixture import SDK_ROOT, tool_fixture
"""Nonshipping OCI variants retain exact source and separate archive closure."""
from pathlib import Path
import json
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import image_fixtures as fixture
import dependencies

archive_fixture = tool_fixture('image_scan_fixture')


class FixtureImageControls(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / '.artifacts').mkdir()
        self.config = {'components': {'fixture_images': {'directory': '.artifacts/fixture-images'},
            'images': [{'name': 'api'}, {'name': 'api-localdev'}, {'name': 'jwks-counter'}]}}
        self.definition = {'image_base': 'ghcr.io/fixture/app',
            'inputs': {'images': ['ghcr.io/fixture/app-api:{revision}']}}
        self.identities = {}
        self.oci = dependencies.helper('oci_archive')
        self.exports = []
        self.imports = []
        def save(reference, path, **_kwargs):
            inspected, _, _ = archive_fixture.archive(path, reference=reference, source='a' * 40,
                                                      body=reference.encode())
            self.identities[reference] = inspected['Id']
            self.exports.append((reference, path))
            return self.oci.inspect_archive(path)
        self.save = patch.object(self.oci, 'save_image', side_effect=save)
        self.save.start()
        self.addCleanup(self.save.stop)
        self.load = patch.object(self.oci, 'load_image', side_effect=lambda path, **_kwargs: self.imports.append(path))
        self.load.start()
        self.addCleanup(self.load.stop)
        self.declaration = patch.object(fixture.images, 'definition', return_value=self.definition)
        self.declaration.start()
        self.addCleanup(self.declaration.stop)
        def inspect(references, *_args):
            self.assertEqual(set(references), set(self.identities.values()))
            return {identity: identity for identity in references}
        self.inspect = patch.object(fixture, 'inspect', side_effect=inspect)
        self.inspect.start()
        self.addCleanup(self.inspect.stop)

    def test_shipping_images_stay_separate_and_each_fixture_archive_is_closed(self):
        fixture.package(self.root, self.config, {'SOURCE_COMMIT': 'a' * 40}, None)
        self.assertEqual(fixture.verify(self.root, self.config, {'SOURCE_COMMIT': 'a' * 40}), self.identities)
        fixture.load(self.root, self.config, {'SOURCE_COMMIT': 'a' * 40},
                     {'image:' + key: value for key, value in self.identities.items()}, None)
        self.assertEqual(len(self.exports), 2)
        self.assertEqual(len(self.imports), 2)
        self.assertNotIn('ghcr.io/fixture/app-api:' + 'a' * 40, self.identities)
        self.assertEqual({path.name for path in self.imports}, {'api-localdev.oci.tar', 'jwks-counter.oci.tar'})
        directory = self.root / '.artifacts/fixture-images'
        (directory / 'unexpected.env').write_text('private fixture data')
        with self.assertRaisesRegex(ValueError, 'inventory'):
            fixture.verify(self.root, self.config, {'SOURCE_COMMIT': 'a' * 40})

    def test_wrong_source_archive_bytes_or_build_proof_fail_before_native_load(self):
        fixture.package(self.root, self.config, {'SOURCE_COMMIT': 'a' * 40}, None)
        for source, accepted in [('c' * 40, {}), ('a' * 40, {})]:
            with self.assertRaises(ValueError):
                fixture.load(self.root, self.config, {'SOURCE_COMMIT': source}, accepted, None)
        self.assertEqual(self.imports, [])
        (self.root / '.artifacts/fixture-images/api-localdev.oci.tar').write_bytes(b'changed archive')
        with self.assertRaises((ValueError, tarfile.ReadError)):
            fixture.verify(self.root, self.config, {'SOURCE_COMMIT': 'a' * 40})
        self.assertEqual(self.imports, [])

    def test_extra_declaration_requires_real_bounded_nonshipping_native_builds(self):
        for value in ['deploy/fixture-images', '.artifacts', '.artifacts/release/fixtures']:
            self.config['components']['fixture_images']['directory'] = value
            with self.assertRaises(ValueError):
                fixture.declaration(self.root, self.config)


if __name__ == '__main__':
    unittest.main()
