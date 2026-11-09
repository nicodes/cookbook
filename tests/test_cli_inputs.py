"""Keep downloaded release data outside executable source and provider roots."""
from source_fixture import SDK_ROOT
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import cli_inputs as inputs
import test_native_ci


class ReleaseInputs(unittest.TestCase):
    def setUp(self):
        fixture = test_native_ci.NativeMatrix(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        self.root, self.config = fixture.root, fixture.config
        self.config['version'] = 1
        entry = {'runner': 'python', 'path': 'tests/boundary.py', 'timeout': 10}
        self.config['publication'] = {'acceptance_timeout': 55,
            'acceptance': {'files': ['accepted.json'], 'checks': [entry], 'verification': [entry]}}
        selected = patch.object(inputs, 'load', return_value=self.config); selected.start(); self.addCleanup(selected.stop)
        self.names = inputs.native_names(self.root, self.config)
        self.source = self.root / '.artifacts/cli-input/full'
        for name in [inputs.PROOF, *['dist/' + item for item in self.names]]:
            target = self.source / name; target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_bytes(('original bytes: ' + name).encode())
        self.provider = self.root / '.artifacts/recipe-sdks/_cookbook/cookbook/cli_release.py'
        self.provider.parent.mkdir(parents=True); self.provider.write_bytes(b'trusted provider sentinel')

    def test_full_payload_stages_exact_original_bytes_and_never_overlays_provider_or_source(self):
        attack = self.source / '.artifacts/recipe-sdks/_cookbook/cookbook/cli_release.py'
        attack.parent.mkdir(parents=True); attack.write_text('raise RuntimeError("must remain data")')
        source = (self.root / 'tests/boundary.py').read_bytes()
        inputs.stage(self.root, 'full')
        self.assertEqual(self.provider.read_bytes(), b'trusted provider sentinel')
        self.assertEqual((self.root / 'tests/boundary.py').read_bytes(), source)
        for name in self.names:
            self.assertEqual((self.root / 'dist' / name).read_bytes(), (self.source / 'dist' / name).read_bytes())
        self.assertEqual((self.root / inputs.PROOF).read_bytes(), (self.source / inputs.PROOF).read_bytes())

    def test_product_handoff_is_closed_and_cannot_replace_original_proof(self):
        self.source.rename(self.source.with_name('archives'))
        product = self.root / '.artifacts/cli-input/product'; product.mkdir()
        for name in ['receipt.json', 'accepted.json']: (product / name).write_bytes(b'{}')
        (product / 'cookbook-validation.json').write_bytes(b'forged proof')
        with self.assertRaisesRegex(ValueError, 'closed evidence'):
            inputs.stage(self.root, 'archives', product=True)
        self.assertFalse((self.root / 'dist').exists())
        (product / 'cookbook-validation.json').unlink()
        inputs.stage(self.root, 'archives', product=True)
        self.assertEqual((self.root / inputs.PROOF).read_bytes(), b'original bytes: ' + inputs.PROOF.encode())
        self.assertEqual({p.name for p in (self.root / inputs.EVIDENCE).iterdir()}, {'receipt.json', 'accepted.json'})

    def test_combined_publication_retains_both_original_archives_and_admitted_evidence(self):
        directory = self.source.with_name('publication'); self.source.rename(directory)
        evidence = directory / inputs.EVIDENCE; evidence.mkdir(parents=True)
        for name in ['receipt.json', 'accepted.json']: (evidence / name).write_bytes(b'{}')
        (directory / 'dist/accepted.json').write_bytes(b'{}')
        inputs.stage(self.root, 'publication')
        self.assertEqual((self.root / 'dist/accepted.json').read_bytes(), b'{}')
        self.assertEqual(self.provider.read_bytes(), b'trusted provider sentinel')

    def test_native_only_publication_requires_no_product_receipt(self):
        self.config['publication'].pop('acceptance')
        self.source.rename(self.source.with_name('publication'))
        inputs.stage(self.root, 'publication')
        self.assertEqual({path.name for path in (self.root / 'dist').iterdir()}, set(self.names))
        self.assertFalse((self.root / inputs.EVIDENCE).exists())

    def test_linked_or_changed_source_and_unknown_archive_refuse_without_provider_effects(self):
        selected = self.source / 'dist' / self.names[0]
        original = selected.read_bytes(); selected.unlink(); selected.symlink_to(self.provider)
        with self.assertRaisesRegex(ValueError, 'link'):
            inputs.stage(self.root, 'full')
        selected.unlink(); selected.write_bytes(original)
        (self.source / 'dist/extra.json').write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError, 'closed release'):
            inputs.stage(self.root, 'full')
        self.assertFalse((self.root / 'dist').exists())
        self.assertEqual(self.provider.read_bytes(), b'trusted provider sentinel')

    def test_missing_or_oversized_member_preserves_existing_evidence_and_cleans_owned_partial_output(self):
        proof = self.source / inputs.PROOF; proof.unlink()
        with self.assertRaisesRegex(ValueError, 'missing'):
            inputs.stage(self.root, 'full')
        proof.write_bytes(b'proof')
        with patch.object(inputs, 'MAX_FILE', 1), self.assertRaisesRegex(ValueError, 'bound'):
            inputs.stage(self.root, 'full')
        self.assertFalse((self.root / 'dist').exists()); self.assertFalse((self.root / inputs.PROOF).exists())
        retained = self.root / 'dist'; retained.mkdir(); (retained / 'prior').write_bytes(b'keep')
        with self.assertRaisesRegex(ValueError, 'fresh'):
            inputs.stage(self.root, 'full')
        self.assertEqual((retained / 'prior').read_bytes(), b'keep')


if __name__ == '__main__': unittest.main()
