import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('sdk_identity_control', Path(__file__).parents[1] / 'cookbook/sdk_identity.py')
identity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(identity)


class SDKDiscovery(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='SDK source with spaces ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sdk = self.root / 'separate SDK'
        self.sdk.mkdir()
        (self.sdk / 'native.py').write_text('raise RuntimeError("source must never execute")\n')
        self.git('init', '-q')
        self.git('remote','add','origin','https://github.com/nicodes/cookbook.git')
        self.git('add', 'native.py')
        self.git('-c', 'user.name=SDK control', '-c', 'user.email=sdk-control@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '--quiet', '-m', 'finite source fixture')
        self.revision = self.git('rev-parse', 'HEAD').strip()
        self.mise = '[vars]\ncookbook_contract=1\ncookbook_ref="' + self.revision + '"\n'
        (self.root / 'mise.toml').write_text(self.mise)

    def git(self, *arguments):
        return subprocess.check_output(['git', '-c', 'core.hooksPath=' + os.devnull,
            '-C', str(self.sdk), *arguments], text=True, stderr=subprocess.PIPE)

    def test_data_identity_and_supplied_exact_clean_source_are_admitted_without_execution(self):
        self.assertEqual(identity.identity(self.mise), {'version': 1, 'repository':'nicodes/cookbook', 'revision': self.revision})
        self.assertEqual(identity.prepared(self.root, self.revision, self.sdk), self.sdk)

    def test_real_provider_origin_is_required_even_for_identical_commit_and_tree(self):
        for origin in ['git@github.com:nicodes/cookbook.git', 'ssh://git@github.com/nicodes/cookbook.git',
                       'https://github.com/nicodes/cookbook']:
            self.git('remote','set-url','origin',origin)
            self.assertEqual(identity.prepared(self.root,self.revision,self.sdk),self.sdk)
        self.git('remote','set-url','origin','https://github.com/foreign/tools.git')
        self.assertEqual(self.git('rev-parse','HEAD').strip(),self.revision)
        with self.assertRaisesRegex(ValueError,'origin differs'):
            identity.prepared(self.root,self.revision,self.sdk)
        self.git('remote','set-url','origin','https://github.com/nicodes/tools.git')
        with self.assertRaisesRegex(ValueError,'origin differs'):
            identity.prepared(self.root,self.revision,self.sdk)
        self.assertEqual(identity.verify(self.sdk,self.revision,repository='nicodes/tools'),self.sdk)
        for origin in ['http://github.com/nicodes/cookbook.git', 'https://github.com/../tools.git',
                       'https://github.com/nicodes/cookbook.git?other=1']:
            with self.assertRaises(ValueError):
                identity.canonical_origin(origin)

    def test_default_Cookbook_location_and_retired_Make_authority_refusal(self):
        destination = identity.location(self.root, self.revision)
        self.assertEqual(destination, self.root/'.artifacts/cookbook'/self.revision)
        destination.parent.mkdir(parents=True)
        shutil.copytree(self.sdk, destination)
        self.assertEqual(identity.prepared(self.root), destination)
        (self.root / 'mise.toml').write_text('[tools]\npython="3.13.11"\n')
        (self.root / 'Makefile').write_text('COOKBOOK_REF := ' + self.revision + '\n')
        with self.assertRaises(ValueError):
            identity.source_identity(self.root)
        with self.assertRaises(ValueError):
            identity.prepared(self.root)

    def test_wrong_identity_dirty_untracked_and_ignored_SDK_source_refused(self):
        with self.assertRaises(ValueError):
            identity.prepared(self.root, 'a' * 40, self.sdk)
        for path in ['native.py', 'untracked.py']:
            before = (self.sdk / path).read_bytes() if (self.sdk / path).exists() else None
            (self.sdk / path).write_text('changed\n')
            with self.subTest(path=path), self.assertRaises(ValueError):
                identity.prepared(self.root, supplied=self.sdk)
            if before is None:
                (self.sdk / path).unlink()
            else:
                (self.sdk / path).write_bytes(before)
        (self.sdk / '.git/info/exclude').write_text('ignored.py\n')
        (self.sdk / 'ignored.py').write_text('ignored native program\n')
        with self.assertRaises(ValueError):
            identity.prepared(self.root, supplied=self.sdk)

    def test_partial_future_identity_never_falls_back_to_existing_legacy_pin(self):
        (self.root / 'Makefile').write_text('COOKBOOK_REF := ' + self.revision + '\n')
        for source in ['[vars]\ncookbook_contract=1\n', '[vars]\ncookbook_contract=2\ncookbook_ref="' + self.revision + '"\n',
                       '[tasks.extra]\nrun="never execute"\n', self.mise+'tools_ref="'+'f'*40+'"\n']:
            (self.root / 'mise.toml').write_text(source)
            with self.subTest(source=source), self.assertRaises(ValueError):
                identity.prepared(self.root, supplied=self.sdk)

    def test_native_Mise_newlines_bind_fixed_Cookbook_identity_and_preserve_raw_bytes(self):
        for newline in ['\n', '\r\n', '\r']:
            raw = self.mise.replace('\n', newline).encode()
            (self.root / 'mise.toml').write_bytes(raw)
            with self.subTest(newline=repr(newline)):
                self.assertEqual(identity.source_identity(self.root),
                                 {'version':1,'repository':'nicodes/cookbook','revision':self.revision})
                self.assertEqual(identity.read_source(self.root), raw)
                self.assertEqual((self.root / 'mise.toml').read_bytes(), raw)
            for invalid in [raw + raw, self.mise.replace(self.revision,self.revision+'extra').encode()]:
                (self.root / 'mise.toml').write_bytes(invalid)
                with self.assertRaises(ValueError):
                    identity.source_identity(self.root)

    def test_missing_and_other_owning_git_root_refused_without_fetch(self):
        with patch.object(identity, 'git', side_effect=AssertionError('missing SDK must not call Git')):
            with self.assertRaises(ValueError):
                identity.prepared(self.root)
        nested = self.sdk / 'nested'
        nested.mkdir()
        with self.assertRaises(ValueError):
            identity.prepared(self.root, supplied=nested)

    def test_linked_source_and_linked_parent_refused(self):
        link = self.root / 'linked SDK'
        try:
            link.symlink_to(self.sdk, target_is_directory=True)
        except OSError:
            # Windows may deny creating new links. The refusal itself still
            # runs against the native path API; Linux exercises actual links.
            with patch.object(Path, 'is_symlink', return_value=True), self.assertRaises(ValueError):
                identity.prepared(self.root, supplied=link)
            with patch.object(Path, 'is_symlink', return_value=True), self.assertRaises(ValueError):
                identity.source_identity(self.root)
            return
        with self.assertRaises(ValueError):
            identity.prepared(self.root, supplied=link)
        (self.root / 'mise.toml').unlink()
        target = self.root / 'real-source.toml'
        target.write_text(self.mise)
        (self.root / 'mise.toml').symlink_to(target)
        with self.assertRaises(ValueError):
            identity.source_identity(self.root)

    def test_bounded_source_changed_metadata_and_duplicate_keys_refused(self):
        for source in [self.mise + 'x' * identity.MAX_SOURCE,
                       self.mise.replace('cookbook_contract=1', 'cookbook_contract=1\ncookbook_contract=1'),
                       self.mise.replace('cookbook_contract=1', 'cookbook_contract=true')]:
            with self.subTest(source=source[:100]), self.assertRaises(ValueError):
                identity.identity(source)
        original = identity._signature
        calls = 0

        def changed(info, *, cross=False):
            nonlocal calls
            calls += 1
            value = original(info, cross=cross)
            return value + (calls,) if not cross else value

        with patch.object(identity, '_signature', side_effect=changed), self.assertRaisesRegex(ValueError, 'changed while reading'):
            identity.read_source(self.root)


if __name__ == '__main__':
    unittest.main()
