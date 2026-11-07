from source_fixture import SDK_ROOT, write_entrypoint, run_task, run_unit_adapter
import hashlib
from source_fixture import write_source
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import unittest

TOOLS = SDK_ROOT
sys.path.insert(0, str(TOOLS / 'cookbook'))
spec = importlib.util.spec_from_file_location('rust_recipe_generation', TOOLS / 'cookbook/generate.py')
generate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generate)


@unittest.skipIf(os.name == 'nt', 'orchestration fixtures use POSIX executable stubs; native Windows jobs exercise the actual compiler')
class RustRecipe(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='rust recipe ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for directory in ['tests', '.github', 'commands']:
            (self.root / directory).mkdir()
        readme = '# Native recipe fixture\n'
        (self.root / 'README.md').write_text(readme)
        write_source(self.root / '.github/readme-source.toml', {'version': 1,
            'sha256': hashlib.sha256(readme.encode()).hexdigest()})
        (self.root / '.gitignore').write_text('/.artifacts/\n/dist/\n')
        (self.root / 'Cargo.toml').write_text('[package]\nname="example"\nversion="0.0.1"\n')
        (self.root / 'Cargo.lock').write_text('version=4\n')
        (self.root / 'tests/boundary.py').write_text('assert 1 + 1 == 2\n')
        (self.root / 'tests/other_boundary.py').write_text('assert 3 + 1 == 4\n')
        compiler = self.root / 'commands/cargo'
        compiler.write_text('#!' + sys.executable + '\n' + r'''
import json,os
from pathlib import Path
import platform
import sys
root=Path.cwd();output=root/'.artifacts';output.mkdir(exist_ok=True)
with (output/'commands.jsonl').open('a') as stream:stream.write(json.dumps(sys.argv[1:])+'\n')
if sys.argv[1]=='build':
    target=([sys.argv[sys.argv.index('--target')+1]] if '--target' in sys.argv else [])
    binary=Path(os.environ['CARGO_TARGET_DIR']).joinpath(*target,'release/example');binary.parent.mkdir(parents=True,exist_ok=True)
    binary.write_text('#!'+sys.executable+'\nprint('+repr('example '+os.environ['VERSION']+' ('+os.environ['COMMIT']+')')+')\n');binary.chmod(0o755)
''')
        compiler.chmod(0o755)
        (self.root / 'cookbook.toml').write_text('''version=1
recipe="rust-cli"
[[components.rust]]
name="cli"
manifest="Cargo.toml"
binaries=["example"]
[package]
binary="example"
files=["README.md"]
version_arguments=["--version"]
version_template="{binary} {version} ({commit})"
[[package.native_targets]]
platform="Linux"
arch="x86_64"
extension="tar.gz"
target="x86_64-unknown-linux-gnu"
checks=["tests/boundary.py"]
[[tests.integration]]
runner="python"
path="tests/boundary.py"
[[tests.integration]]
runner="python"
path="tests/other_boundary.py"
[clean]
paths=["dist", ".artifacts/build", ".artifacts/native-package"]
''')
        native_os = platform.system()
        native_arch = {'aarch64': 'arm64', 'amd64': 'x86_64'}.get(platform.machine().lower(), platform.machine().lower())
        data = self.root / 'cookbook.toml'
        data.write_text(data.read_text().replace('platform="Linux"', 'platform="' + native_os + '"').replace('arch="x86_64"', 'arch="' + native_arch + '"'))
        write_entrypoint(self.root)
        for command in [['init', '-q'], ['add', '.'], ['-c', 'user.name=Native Recipe',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture']]:
            subprocess.run(['git', '-C', str(self.root), *command], check=True)
        self.environment = {**os.environ, 'PATH': str(self.root / 'commands') + os.pathsep + os.environ['PATH'],
                            'CARGO_TARGET_DIR': str(self.root / '.artifacts/rust-target')}
        for key in ['COMMIT', 'VERSION', 'DATE', 'RUST_TARGET', 'COOKBOOK_TEST_PATHS', 'NATIVE_CLI_ARCHIVES']:
            self.environment.pop(key, None)

    def make(self, name, selection=None):
        environment = self.environment | ({'COOKBOOK_TEST_PATHS': json.dumps(selection)} if selection else {})
        return run_unit_adapter(self.root, name, environment=environment)

    def passed(self, name, selection=None):
        result = self.make(name, selection)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_build_runs_compiler_validation_and_binds_closed_outputs(self):
        self.passed('build')
        commands = [json.loads(line)[0] for line in (self.root / '.artifacts/commands.jsonl').read_text().splitlines()]
        self.assertEqual(commands, ['build', 'clippy'])
        self.assertTrue((self.root / '.artifacts/verification/build.json').is_file())
        self.assertEqual(len(list((self.root / '.artifacts/native-package').iterdir())), 2)

    def test_named_native_cargo_selection_never_claims_complete_unit_or_test_coverage(self):
        data = self.root / 'cookbook.toml'
        data.write_text(data.read_text() + '\n[[components.rust_tests]]\nname="native-library"\ncomponent="cli"\nselectors={lib=true}\n')
        self.environment['COOKBOOK_NATIVE_TESTS'] = '["native-library"]'
        directory = self.root / '.artifacts/verification'
        directory.mkdir(parents=True)
        (directory / 'test.json').write_text('{"old":"aggregate"}')
        self.passed('test:unit')
        slug = 'native-cargo-' + hashlib.sha256(b'native-library').hexdigest()[:24]
        self.assertTrue((directory / (slug + '.json')).is_file())
        self.assertFalse((directory / 'unit.json').exists())
        self.assertFalse((directory / 'test.json').exists())
        commands = [json.loads(line) for line in (self.root / '.artifacts/commands.jsonl').read_text().splitlines()]
        self.assertEqual(commands[0][0], 'test')
        self.assertIn('--lib', commands[0])
        self.assertNotEqual(self.make('test').returncode, 0)
        self.environment['COOKBOOK_NATIVE_TESTS'] = '["undeclared"]'
        self.assertNotEqual(self.make('test:unit').returncode, 0)
        self.assertEqual(len((self.root / '.artifacts/commands.jsonl').read_text().splitlines()), 1)

    def test_full_unit_executes_explicit_native_selectors_when_base_variant_is_disabled(self):
        data=self.root/'cookbook.toml'
        data.write_text(data.read_text().replace('[package]','[components.rust.test]\nenabled=false\n[package]',1)
            +'\n[[components.rust_tests]]\nname="native-owner"\ncomponent="cli"\nselectors={enabled=true,lib=true}\n')
        write_entrypoint(self.root)
        self.passed('test:unit')
        commands=[json.loads(line) for line in (self.root/'.artifacts/commands.jsonl').read_text().splitlines()]
        self.assertEqual(len(commands),1)
        self.assertEqual(commands[0][0],'test')
        self.assertIn('--lib',commands[0])
        self.assertTrue((self.root/'.artifacts/verification/unit.json').is_file())

    def test_disabled_base_without_real_native_unit_selection_still_refuses(self):
        data=self.root/'cookbook.toml'
        data.write_text(data.read_text().replace('[package]','[components.rust.test]\nenabled=false\n[package]',1))
        write_entrypoint(self.root)
        result=self.make('test:unit')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('no applicable native components',result.stderr)
        self.assertFalse((self.root/'.artifacts/verification/unit.json').exists())

    def test_api_wave_binds_only_its_outputs_and_cannot_claim_the_full_boundary_group(self):
        data = self.root / 'cookbook.toml'
        data.write_text(data.read_text() + '\n[[components.native]]\nname="api"\nrole="api"\ncomponent="cli"\nbinary="example"\noutput=".artifacts/native-api/example_{platform}_{arch}"\nchecks=["tests/boundary.py"]\n')
        write_entrypoint(self.root)
        self.environment['COOKBOOK_NATIVE_COMPONENTS'] = '["api"]'
        self.passed('build')
        receipt = json.loads((self.root / '.artifacts/verification/build.json').read_text())
        self.assertEqual(len(receipt['artifacts']), 2)
        self.assertTrue(all(name.startswith('.artifacts/native-api/') for name in receipt['artifacts']))
        self.assertFalse((self.root / 'dist').exists())
        result = self.make('test:integration')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('partial native role build', result.stderr)
        self.passed('test:integration', ['tests/boundary.py'])
        self.assertFalse((self.root / '.artifacts/verification/integration.json').exists())

    def test_boundary_tests_require_existing_outputs_without_building(self):
        self.assertNotEqual(self.make('test:integration').returncode, 0)
        self.assertFalse((self.root / '.artifacts/commands.jsonl').exists())
        self.passed('build')
        before = (self.root / '.artifacts/commands.jsonl').read_bytes()
        self.passed('test:integration')
        self.assertEqual(before, (self.root / '.artifacts/commands.jsonl').read_bytes())
        (self.root / '.artifacts/build/example').write_text('changed after build')
        self.assertNotEqual(self.make('test:integration').returncode, 0)

    def test_filtered_native_boundary_never_claims_full_group_or_aggregate(self):
        self.passed('build')
        self.passed('test')
        self.passed('test:integration', ['tests/boundary.py'])
        directory = self.root / '.artifacts/verification'
        self.assertFalse((directory / 'test.json').exists())
        name = 'native-' + hashlib.sha256(b'tests/boundary.py').hexdigest()[:24] + '.json'
        self.assertTrue((directory / name).is_file())
        self.assertNotEqual(self.make('test', ['tests/boundary.py']).returncode, 0)
        self.assertNotEqual(self.make('test:integration', ['tests/other_boundary.py']).returncode, 0)


if __name__ == '__main__':
    unittest.main()
