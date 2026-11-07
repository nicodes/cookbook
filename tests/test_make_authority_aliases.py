"""Every recognized root Make name conflicts with the sole recipe authority."""
from source_fixture import SDK_ROOT
from itertools import combinations
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import generate
import mise_entrypoint

ALIASES = ['Makefile', 'makefile', 'GNUmakefile']
REVISION = 'e' * 40


class MakeAuthorityAliases(unittest.TestCase):
    def prepare(self, root):
        (root / 'tests').mkdir()
        (root / 'tests/check.py').write_bytes(b'raise RuntimeError("native task must not execute")\n')
        (root / 'cookbook.toml').write_bytes(b'version=1\nrecipe="godot-addon"\n[[tests.unit]]\n'
                                           b'runner="python"\npath="tests/check.py"\n')
        (root / 'mise.toml').write_bytes(b'[tools]\npython="3.13.11"\n')
        for name, body in generate.entrypoint_files(root, REVISION).items():
            (root / name).write_bytes(body.encode())

    def refuses_before_recipe_read(self, root):
        with patch.object(mise_entrypoint, 'read', side_effect=AssertionError('recipe data was read')), \
                patch.object(generate, 'entrypoint_files', side_effect=AssertionError('recipe was rendered')), \
                self.assertRaisesRegex(ValueError, 'one task authority'):
            generate.check_entrypoint(root, REVISION)

    def test_canonical_two_file_source_passes_without_native_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            before = {name: (root / name).read_bytes() for name in ['mise.toml', 'cookbook.toml', '.gitattributes']}
            original = subprocess.run
            commands = []
            def source_only(argv, *args, **kwargs):
                self.assertEqual(str(argv[0]), 'git', 'source admission may not execute Make, Mise or native tasks')
                commands.append(list(argv))
                return original(argv, *args, **kwargs)
            with patch.object(subprocess, 'run', side_effect=source_only):
                generate.check_entrypoint(root, REVISION)
            self.assertEqual({name: (root / name).read_bytes() for name in before}, before)
            self.assertTrue(all(str(argv[0]) == 'git' for argv in commands))

    def test_each_regular_alias_refuses_before_recipe_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            for name in ALIASES:
                with self.subTest(name=name):
                    path = root / name
                    path.write_bytes(b'all:\n\t@false\n')
                    self.refuses_before_recipe_read(root)
                    self.assertEqual(path.read_bytes(), b'all:\n\t@false\n')
                    path.unlink()

    def test_each_dangling_alias_symlink_refuses_before_recipe_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            for name in ALIASES:
                with self.subTest(name=name):
                    path = root / name
                    path.symlink_to(root / 'missing-native-input')
                    self.assertFalse(path.exists())
                    self.assertTrue(path.is_symlink())
                    self.refuses_before_recipe_read(root)
                    path.unlink()

    def test_each_directory_and_linked_real_alias_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            target = root / 'retained-native-input'
            target.write_bytes(b'unchanged native bytes\n')
            for name in ALIASES:
                with self.subTest(name=name):
                    path = root / name
                    path.mkdir()
                    self.refuses_before_recipe_read(root)
                    path.rmdir()
                    path.symlink_to(target)
                    self.refuses_before_recipe_read(root)
                    self.assertEqual(target.read_bytes(), b'unchanged native bytes\n')
                    path.unlink()

    def test_colliding_aliases_never_select_an_alternate_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            for names in [*combinations(ALIASES, 2), tuple(ALIASES)]:
                with self.subTest(names=names):
                    for name in names:
                        (root / name).write_bytes(b'all:\n\t@false\n')
                    self.refuses_before_recipe_read(root)
                    for name in names:
                        (root / name).unlink(missing_ok=True)

    def test_alias_refusal_does_not_depend_on_recipe_data_or_a_format_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ALIASES:
                with self.subTest(name=name):
                    path = root / name
                    path.write_bytes(b'all:\n\t@false\n')
                    self.refuses_before_recipe_read(root)
                    path.unlink()


if __name__ == '__main__':
    unittest.main()
