from source_fixture import SDK_ROOT
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import rust_staging


class NativeVariants(unittest.TestCase):
    def test_shared_compiler_cache_cannot_alias_distinct_fixture_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            compiled = root / '.artifacts/target/debug'
            compiled.mkdir(parents=True)
            records = [dict(manifest='api/Cargo.toml', binaries=['api'], profile='debug',
                            target_directory='.artifacts/target', features=[feature])
                       for feature in ['localdev', 'engineering']]
            declared = [record | {'output_directory': '.artifacts/fixtures/' + record['features'][0]} for record in records]
            def compiler(directory, selected, environment, run):
                self.assertEqual(len(selected), 1)
                (compiled / 'api').write_bytes(selected[0]['features'][0].encode())
            with patch.object(rust_staging.rust_component, 'build', side_effect=compiler):
                rust_staging.compile_and_stage(root, declared, records, {}, None)
            self.assertEqual((root / '.artifacts/fixtures/localdev/api').read_bytes(), b'localdev')
            self.assertEqual((root / '.artifacts/fixtures/engineering/api').read_bytes(), b'engineering')
            self.assertEqual(rust_staging.artifact_paths(declared), [
                '.artifacts/fixtures/localdev/api', '.artifacts/fixtures/engineering/api'])

    def test_installed_filename_mapping_preserves_native_binary_identity_and_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); compiled=root/'target/debug'; compiled.mkdir(parents=True)
            (compiled/'native-api').write_bytes(b'actual compiled API')
            record=dict(manifest='Cargo.toml', binaries=['native-api'], profile='debug')
            declared=record|{'output_directory':'api/bin', 'output_names':{'native-api':'server'}}
            with patch.object(rust_staging.rust_component,'build'):
                rust_staging.compile_and_stage(root,[declared],[record],{},None)
            self.assertEqual((root/'api/bin/server').read_bytes(),b'actual compiled API')
            self.assertEqual(rust_staging.artifact_paths([declared]),['api/bin/server'])
            for mapping in [{'native-api':'../source'}, {'native-api':'/bin/server'}, {'other':'server'},
                            {'native-api':'..'}, {'native-api':False}]:
                with self.subTest(mapping=mapping),self.assertRaises(ValueError):
                    rust_staging.artifact_paths([declared|{'output_names':mapping}])
            (root/'api/bin/server').unlink();(root/'api/bin/server').symlink_to(compiled/'native-api')
            with patch.object(rust_staging.rust_component,'build'),self.assertRaises(ValueError):
                rust_staging.compile_and_stage(root,[declared],[record],{},None)

    def test_variant_outputs_cannot_overwrite_each_other_or_follow_compiler_links(self):
        with self.assertRaisesRegex(ValueError, 'distinct'):
            rust_staging.artifact_paths([dict(binaries=['api']), dict(binaries=['api'])])
        self.assertEqual(rust_staging.artifact_paths([dict(binaries=['api'], build={'enabled': False})]), [])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'target/debug'
            path.mkdir(parents=True)
            (root / 'other').write_bytes(b'not a compiled fixture')
            (path / 'api').symlink_to(root / 'other')
            record = dict(manifest='Cargo.toml', binaries=['api'], profile='debug')
            with patch.object(rust_staging.rust_component, 'build'), self.assertRaisesRegex(ValueError, 'regular'):
                rust_staging.compile_and_stage(root, [record], [record], {}, None)


if __name__ == '__main__':
    unittest.main()
