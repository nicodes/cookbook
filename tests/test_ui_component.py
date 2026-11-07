from source_fixture import SDK_ROOT
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import ui_component


class EmbeddedUI(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='embedded UI ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = self.root / 'app'
        self.package.mkdir()
        (self.package / 'package.json').write_text('{"scripts":{"build":"vite build"}}')
        self.output = self.root / '.artifacts/ui'
        self.output.mkdir(parents=True)
        (self.output / 'index.html').write_text('native output')
        self.declaration = [{}]
        self.packages = [{'directory': 'app', 'build': 'build'}]
        self.commands = []

    def fake_run(self, argv, directory, environment):
        self.commands.append((argv, directory))
        self.assertEqual(argv[:5], ['npm', 'run', 'build', '--', '--outDir'])
        out = Path(argv[-1]);out.mkdir()
        (out / 'index.html').write_text('native output')

    def validate(self, run=None):
        ui_component.validate_build(self.root, self.declaration, self.packages, {}, run or self.fake_run)

    def test_build_verification_checks_exact_files_without_touching_the_first_output(self):
        self.validate()
        self.assertEqual(len(self.commands), 1)
        self.assertEqual((self.output / 'index.html').read_text(), 'native output')
        self.assertEqual(self.commands[0][1], self.package)

    def test_rebuild_difference_missing_first_build_or_required_asset_fails(self):
        (self.output / 'index.html').write_text('changed first build')
        with self.assertRaisesRegex(ValueError, 'different output bytes'):
            self.validate()
        (self.output / 'index.html').unlink()
        with self.assertRaisesRegex(ValueError, 'omitted'):
            self.validate()
        self.output.rmdir()
        with self.assertRaisesRegex(ValueError, 'build the UI'):
            self.validate()

    def test_symlink_output_and_undeclared_package_cannot_pass(self):
        (self.output / 'linked').symlink_to(self.output / 'index.html')
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            self.validate()
        self.packages = []
        with self.assertRaisesRegex(ValueError, 'declared native'):
            self.validate()
        self.assertEqual(self.commands, [])

    def test_source_output_and_required_index_cannot_be_replaced(self):
        for declaration in [{'package': 'other'}, {'output': '.artifacts/other'},
                            {'required_files': ['other.html']}, {'required_files': ['index.html', 'index.html']}]:
            with self.subTest(declaration=declaration), self.assertRaises(ValueError):
                ui_component.validate_build(self.root, [declaration], self.packages, {}, self.fake_run)
        self.assertEqual(self.commands, [])


if __name__ == '__main__':
    unittest.main()
