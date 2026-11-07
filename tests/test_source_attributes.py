"""Keep raw recipe/SDK source bytes through ordinary Git checkout policies."""
from source_fixture import SDK_ROOT
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import generate
import mise_entrypoint


class RawSourceAttributes(unittest.TestCase):
    prefix = ('# Native policies remain owned here.\n*.gd text eol=lf\n*.cfg text eol=crlf\n'
              '*.asset filter=lfs diff=lfs merge=lfs -text\n/cookbook.toml text eol=crlf\n')

    def prepare(self, root, *, prefix='', newline='\n'):
        (root / 'tests').mkdir()
        (root / 'tests/check.py').write_bytes(b'raise RuntimeError("must not execute")\n')
        raw = ('version=1\nrecipe="godot-addon"\n[[tests.unit]]\nrunner="python"\n'
               'path="tests/check.py"\n').replace('\n', newline).encode()
        (root / 'cookbook.toml').write_bytes(raw)
        (root / 'mise.toml').write_bytes(b'[tools]\npython="3.13.11"\n')
        if prefix:
            (root / '.gitattributes').write_bytes(prefix.encode())
        files = generate.entrypoint_files(root, 'e' * 40)
        for name, body in files.items():
            (root / name).write_bytes(body.encode())
        return files

    def test_exact_two_source_files_preserve_native_attribute_prefix(self):
        for prefix in ['', self.prefix, self.prefix.replace('\n', '\r\n'), '*.gd text']:
            with self.subTest(prefix=repr(prefix)), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                files = self.prepare(root, prefix=prefix)
                self.assertEqual(set(files), {'mise.toml', '.gitattributes'})
                self.assertTrue(files['.gitattributes'].startswith(prefix))
                self.assertEqual(files['.gitattributes'],
                                 prefix + ('\n' if prefix and not prefix.endswith('\n') else '') + generate.ATTRIBUTES_BLOCK)
                self.assertEqual(generate.entrypoint_files(root, 'e' * 40), files)
                generate.check_entrypoint(root, 'e' * 40)

    def test_missing_tampered_or_overridden_attributes_are_not_admitted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = self.prepare(root)
            path = root / '.gitattributes'
            path.unlink()
            with self.assertRaises((OSError, ValueError)):
                generate.check_entrypoint(root, 'e' * 40)
            for mutation in [files['.gitattributes'].replace('/cookbook.toml -text', '/cookbook.toml text'),
                             files['.gitattributes'].replace('/mise.toml -text', '*.toml -text'),
                             files['.gitattributes'] + '/cookbook.toml text\n']:
                path.write_bytes(mutation.encode())
                with self.assertRaises(ValueError):
                    generate.check_entrypoint(root, 'e' * 40)

    def test_partial_duplicate_nonregular_and_oversized_attributes_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            path = root / '.gitattributes'
            for mutation in [generate.ATTRIBUTES_START, generate.ATTRIBUTES_END,
                             generate.ATTRIBUTES_BLOCK * 2, '#' * mise_entrypoint.MAX_SOURCE,
                             '\0invalid-source']:
                path.write_bytes(mutation.encode())
                with self.assertRaises(ValueError):
                    generate.source_attributes(root)
            path.unlink(); path.mkdir()
            with self.assertRaises(ValueError):
                generate.source_attributes(root)
            path.rmdir()
            target = root / 'native-attributes'; target.write_bytes(b'*.gd text\n')
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                generate.source_attributes(root)

    def test_raw_configuration_hash_still_refuses_newline_conversion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            original = (root / 'cookbook.toml').read_bytes()
            converted = original.replace(b'\n', b'\r\n')
            self.assertEqual(tomllib.loads(original.decode()), tomllib.loads(converted.decode()))
            self.assertNotEqual(hashlib.sha256(original).digest(), hashlib.sha256(converted).digest())
            (root / 'cookbook.toml').write_bytes(converted)
            with self.assertRaisesRegex(ValueError, 'exact native recipe declaration'):
                generate.check_entrypoint(root, 'e' * 40)

    def test_intentionally_authored_CRLF_is_preserved_and_bound_as_raw_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root, newline='\r\n')
            raw = (root / 'cookbook.toml').read_bytes()
            generate.check_entrypoint(root, 'e' * 40)
            record = mise_entrypoint.decode((root / 'mise.toml').read_bytes())
            self.assertEqual(record['configuration_sha256'], hashlib.sha256(raw).hexdigest())
            self.assertIn(b'\r\n', raw)

    def test_source_only_CLI_check_retains_native_attribute_newlines(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root, prefix=self.prefix.replace('\n', '\r\n'))
            path = root / '.gitattributes'; original = path.read_bytes()
            with patch.object(sys, 'argv', ['generate', '--root', str(root), '--revision', 'e' * 40, '--check']):
                generate.main()
            self.assertEqual(path.read_bytes(), original)

    def git(self, root, *args):
        environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
        return subprocess.check_output(['git', '-C', str(root), *map(str, args)],
                                       env=environment, stderr=subprocess.PIPE, timeout=30)

    def commit(self, root):
        self.git(root, 'init', '-q')
        self.git(root, 'config', '--local', 'core.autocrlf', 'false')
        self.git(root, 'add', '.')
        self.git(root, '-c', 'user.name=Raw source fixture', '-c', 'user.email=fixture@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Raw byte source fixture only')

    def checkout(self, parent, source, name, route):
        root = parent / name
        if route == 'clone':
            self.git(parent, 'clone', '-q', '--no-hardlinks', '-c', 'core.autocrlf=true', source, root)
        else:
            root.mkdir(); self.git(root, 'init', '-q')
            self.git(root, 'config', '--local', 'core.autocrlf', 'true')
            self.git(root, 'fetch', '-q', source, 'HEAD')
            self.git(root, 'checkout', '-q', '--detach', 'FETCH_HEAD')
        self.assertEqual(self.git(root, 'status', '--porcelain'), b'')
        return root

    def test_real_clone_and_checkout_preserve_only_the_two_recipe_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            observed = {}
            for fixed in [False, True]:
                source = parent / ('source-' + str(fixed)); source.mkdir()
                self.prepare(source, prefix=self.prefix)
                if not fixed:
                    (source / '.gitattributes').write_bytes(self.prefix.encode())
                (source / 'app').mkdir(); (source / 'art').mkdir()
                (source / 'app/main.gd').write_bytes(b'extends Node\n# native source\n')
                (source / 'app/config.cfg').write_bytes(b'[native]\nvalue=1\n')
                (source / 'art/image.asset').write_bytes(b'\0\xffnative-resource\n')
                self.commit(source)
                for route in ['clone', 'init-fetch-checkout']:
                    with self.subTest(fixed=fixed, route=route):
                        root = self.checkout(parent, source, str(fixed) + '-' + route, route)
                        if fixed:
                            generate.check_entrypoint(root, 'e' * 40)
                        else:
                            with self.assertRaisesRegex(ValueError, 'exact native recipe declaration'):
                                generate.check_entrypoint(root, 'e' * 40)
                        for name in ['mise.toml', 'cookbook.toml']:
                            self.assertEqual((root / name).read_bytes() == self.git(root, 'show', 'HEAD:' + name), fixed)
                        paths = ['app/main.gd', 'app/config.cfg', 'art/image.asset']
                        native = {name: (root / name).read_bytes() for name in paths}
                        attributes = self.git(root, 'check-attr', '-a', '--', *paths)
                        if fixed:
                            self.assertEqual((native, attributes), observed[route])
                        else:
                            observed[route] = native, attributes

    def test_SDK_own_raw_policy_preserves_actual_trusted_module_and_deliberate_CRLF(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            actual = (SDK_ROOT / 'cookbook/sdk_source.py').read_bytes()
            for fixed in [False, True]:
                source = parent / ('source-' + str(fixed)); source.mkdir()
                (source / 'sdk_source.py').write_bytes(actual)
                (source / 'deliberate.txt').write_bytes(b'first\r\nsecond\r\n')
                if fixed:
                    (source / '.gitattributes').write_bytes((SDK_ROOT / '.gitattributes').read_bytes())
                self.commit(source)
                for route in ['clone', 'init-fetch-checkout']:
                    with self.subTest(fixed=fixed, route=route):
                        root = self.checkout(parent, source, str(fixed) + '-' + route, route)
                        self.assertEqual((root / 'sdk_source.py').read_bytes() == actual, fixed)
                        self.assertEqual((root / 'deliberate.txt').read_bytes(), b'first\r\nsecond\r\n')


if __name__ == '__main__':
    unittest.main()
