"""Execute the same immutable two-source bootstrap directly and through Mise."""
from source_fixture import SDK_ROOT, TOOLS_ROOT, run_task, write_entrypoint
import base64
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

import bootstrap
import dependencies
import seed_transport
import sdk_identity


def bash():
    if os.name != 'nt':
        return shutil.which('bash') or 'bash'
    # A bare Windows bash can resolve to the WSL launcher. Use actual Git Bash.
    git = Path(shutil.which('git') or '')
    candidates = [git.parent.parent / 'bin/bash.exe',
                  Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Git/bin/bash.exe']
    return str(next(path for path in candidates if path.is_file()))


class CookbookBootstrap(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='Cookbook bootstrap source ')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'caller with spaces'
        self.root.mkdir()
        self.source = self.base / 'Cookbook SDK with spaces'
        (self.source / 'cookbook').mkdir(parents=True)
        (self.source / 'mise.toml').write_text('[vars]\ntools_contract=1\ntools_ref="' + dependencies.tools_identity()['revision'] + '"\n')
        (self.source / 'cookbook/sibling.py').write_text('VALUE="trusted sibling"\n')
        for name in ['dependencies.py', 'sdk_source.py', 'sdk_identity.py', 'provider_dependency.py']:
            shutil.copyfile(Path(bootstrap.__file__).parent / name, self.source / 'cookbook' / name)
        (self.source / 'cookbook/check.py').write_text(
            'import json,os,sys\nfrom pathlib import Path\nimport sibling\n'
            'assert sys.flags.isolated and sys.dont_write_bytecode\n'
            'assert sibling.VALUE=="trusted sibling"\n'
            'assert os.environ["COOKBOOK_CALLER_ROOT"]==str(Path.cwd().resolve())\n'
            'assert os.environ["COOKBOOK_ROOT"]!=os.environ["TOOLS_ROOT"]\n'
            'Path("completed.json").write_text(json.dumps(sys.argv[1:]))\n'
            'print("bootstrap-child-completed",flush=True)\n'
            'if sys.argv[1:]==["exception"]: raise RuntimeError("native failure")\n'
            'raise SystemExit(23 if sys.argv[1:]==["exit23"] else 0)\n')
        for arguments in [('init', '-q'), ('remote', 'add', 'origin', 'https://github.com/nicodes/cookbook.git'),
                ('add', '.'), ('-c', 'user.name=Bootstrap control', '-c', 'user.email=control@example.invalid',
                             'commit', '-qm', 'immutable SDK fixture')]:
            subprocess.run(['git', '-C', str(self.source), *arguments], check=True)
        self.revision = sdk_identity.git(self.source, 'rev-parse', 'HEAD')
        (self.root / 'tests').mkdir()
        (self.root / 'tests/check.py').write_text('raise AssertionError("product must not run")\n')
        (self.root / 'cookbook.toml').write_text('version=1\nrecipe="godot-addon"\n[[tests.unit]]\nrunner="python"\npath="tests/check.py"\n')
        write_entrypoint(self.root, self.revision)
        self.environment = {**os.environ, 'COOKBOOK_ROOT': str(self.source), 'TOOLS_ROOT': str(TOOLS_ROOT),
            'COOKBOOK_CALLER_ROOT': str(self.base / 'foreign caller'), 'MISE_AUTO_INSTALL': 'false',
            'MISE_EXEC_AUTO_INSTALL': 'false', 'MISE_TASK_RUN_AUTO_INSTALL': 'false',
            'MISE_TRUSTED_CONFIG_PATHS': str(self.root), 'MISE_CEILING_PATHS': str(self.base),
            'MISE_OFFLINE': 'true', 'PYTHONDONTWRITEBYTECODE': '1'}
        for key in ['MISE_CONFIG_FILE', 'MISE_OVERRIDE_CONFIG_FILENAMES', 'MISE_ENV']:
            self.environment.pop(key, None)
        for name in ['global.toml', 'system.toml']:
            (self.base / name).write_text('')
        self.environment.update(MISE_CONFIG_DIR=str(self.base / 'config'),
            MISE_GLOBAL_CONFIG_FILE=str(self.base / 'global.toml'),
            MISE_SYSTEM_CONFIG_FILE=str(self.base / 'system.toml'),
            MISE_CACHE_DIR=str(self.base / 'cache'), MISE_STATE_DIR=str(self.base / 'state'))
        # Neither cwd nor PYTHONPATH may shadow a stdlib or trusted SDK import.
        (self.root / 'json.py').write_text('raise RuntimeError("untrusted cwd import")\n')
        (self.root / 'sibling.py').write_text('raise RuntimeError("untrusted cwd sibling")\n')
        self.environment['PYTHONPATH'] = str(self.root)

    def execute(self, arguments, *, mise=False):
        if mise:
            command = ['mise', 'run', '_sdk', '--', 'exec', 'cookbook/check.py', *arguments]
        else:
            source = bootstrap.command(self.revision) + ' exec cookbook/check.py'
            source += ''.join(' ' + shlex.quote(value) for value in arguments)
            self.assertNotIn('\n', source)
            self.assertLess(len(source.encode('ascii')), 7000)
            command = [bash(), '-euc', source]
        return subprocess.run(command, cwd=self.root, env=self.environment,
                              text=True, capture_output=True, timeout=30)

    def test_actual_bootstrap_and_mise_preserve_literal_argv_isolation_and_verified_caller_root(self):
        import json
        arguments = ['spaces in value', "single'quote", 'double"quote', r'path\with\backslashes']
        for mise in [False, True]:
            with self.subTest(mise=mise):
                result = self.execute(arguments, mise=mise)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('bootstrap-child-completed', result.stdout)
                self.assertEqual(json.loads((self.root / 'completed.json').read_text()), arguments)
                self.assertEqual(sdk_identity.verify(self.source, self.revision), self.source)
                self.assertFalse((self.root / '.artifacts').exists())

    def test_actual_bootstrap_and_mise_wait_for_failure_and_exception_status(self):
        for mise in [False, True]:
            for argument, expected in [('exit23', 23), ('exception', 1)]:
                with self.subTest(mise=mise, argument=argument):
                    (self.root / 'completed.json').unlink(missing_ok=True)
                    result = self.execute([argument], mise=mise)
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                    self.assertTrue((self.root / 'completed.json').is_file())
                    self.assertIn('bootstrap-child-completed', result.stdout)

    def test_actual_provider_windows_setup_preserves_nested_native_argv_separator(self):
        workflow = SDK_ROOT / '.github/workflows/windows-bootstrap.yml'
        commands = [line.strip() for line in workflow.read_text().splitlines()
                    if line.lstrip().startswith('mise run _tools ')]
        self.assertEqual(len(commands), 1)
        output = self.base / 'provider-source-environment'
        environment = {**self.environment, 'MISE_TRUSTED_CONFIG_PATHS': str(SDK_ROOT),
                       'MISE_CEILING_PATHS': str(SDK_ROOT.parent), 'GITHUB_ENV': str(output)}
        before = sdk_identity.git(SDK_ROOT, 'status', '--porcelain', '--ignored', '--untracked-files=all')
        result = subprocess.run([bash(), '-euc', commands[0]], cwd=SDK_ROOT, env=environment,
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(output.read_text().splitlines(), [
            'TOOLS_ROOT=' + str(dependencies.tools_root()),
            'TOOLS_REF=' + dependencies.tools_identity()['revision']])
        self.assertEqual(sdk_identity.git(SDK_ROOT, 'status', '--porcelain', '--ignored',
                                         '--untracked-files=all'), before)

    def test_fixed_transport_refuses_mutation_truncation_trailing_or_wrong_size_before_seed_execution(self):
        original = dict(encoded=bootstrap.ENCODED_SEED, length=bootstrap.SEED_LENGTH, sha256=bootstrap.SEED_SHA256)
        self.assertEqual(hashlib.sha256(bootstrap.SEED.encode()).hexdigest(), original['sha256'])
        raw = base64.b64decode(original['encoded'])
        for changed in [dict(encoded=original['encoded'][:-4]),
                dict(encoded=base64.b64encode(raw + b'trailing').decode()),
                dict(length=original['length'] - 1), dict(sha256='0' * 64)]:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                seed_transport.seed_transport(bootstrap.SEED, **(original | changed))
        self.assertFalse((self.root / 'completed.json').exists())

    def test_explicit_setup_prepares_private_parent_before_local_only_sdk_clones(self):
        environment = dict(self.environment)
        environment.pop('COOKBOOK_ROOT', None)
        environment.pop('TOOLS_ROOT', None)
        environment.update(GIT_CONFIG_COUNT='2',
            GIT_CONFIG_KEY_0='url.' + self.source.as_uri() + '.insteadOf',
            GIT_CONFIG_VALUE_0='https://github.com/nicodes/cookbook.git',
            GIT_CONFIG_KEY_1='url.' + Path(TOOLS_ROOT).as_uri() + '.insteadOf',
            GIT_CONFIG_VALUE_1='https://github.com/nicodes/tools.git', GIT_ALLOW_PROTOCOL='file')
        source = seed_transport.seed_transport(bootstrap.SEED, encoded=bootstrap.ENCODED_SEED,
            length=bootstrap.SEED_LENGTH, sha256=bootstrap.SEED_SHA256)
        result = subprocess.run([sys.executable, '-I', '-B', '-c', source, self.revision,
            'setup', 'cookbook/check.py', 'local-only-setup'], cwd=self.root, env=environment,
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        parent = self.root / '.artifacts'
        if os.name != 'nt':
            self.assertEqual(parent.stat().st_mode & 0o777, 0o700)
        sdk_identity.verify(parent / 'cookbook' / self.revision, self.revision)
        sdk_identity.verify(parent / 'tools' / dependencies.tools_identity()['revision'],
                            dependencies.tools_identity()['revision'], repository='nicodes/tools')
        self.assertTrue((self.root / 'completed.json').is_file())

    def test_existing_unsafe_parent_is_not_repaired_and_actual_tools_policy_refuses_before_child(self):
        parent = self.root / '.artifacts'
        parent.mkdir()
        if os.name == 'nt':
            subprocess.run(['icacls', str(parent), '/grant', '*S-1-1-0:(OI)(CI)M'], check=True,
                           capture_output=True, text=True, timeout=30)
            before = subprocess.check_output(['icacls', str(parent)], timeout=30)
        else:
            parent.chmod(0o777)
            before = parent.stat().st_mode
        source = seed_transport.seed_transport(bootstrap.SEED, encoded=bootstrap.ENCODED_SEED,
            length=bootstrap.SEED_LENGTH, sha256=bootstrap.SEED_SHA256)
        result = subprocess.run([sys.executable, '-I', '-B', '-c', source, self.revision,
            'setup', 'cookbook/check.py'], cwd=self.root, env=self.environment,
            capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stderr, 'not protected|grants write access to another Windows principal')
        self.assertFalse((self.root / 'completed.json').exists())
        if os.name == 'nt':
            self.assertEqual(subprocess.check_output(['icacls', str(parent)], timeout=30), before)
        else:
            self.assertEqual(parent.stat().st_mode, before)

    def test_older_windows_interpreter_refuses_before_creating_the_first_parent(self):
        import private_directory
        source = 'import os,sys\nos.name="nt"\nsys.version_info=(3,12,11)\nexec(' + repr(private_directory.SCRIPT) + ')'
        result = subprocess.run([sys.executable, '-I', '-B', '-c', source], cwd=self.root,
            env=self.environment, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('selected Python 3.13 or newer', result.stderr)
        self.assertFalse((self.root / '.artifacts').exists())

    def test_first_workflow_binding_uses_verified_tools_location_without_inherited_override(self):
        import private_directory
        import sdk_source
        subprocess.run([sys.executable, '-I', '-B', '-c', private_directory.argument()],
            cwd=self.root, env=self.environment, check=True, capture_output=True, text=True, timeout=30)
        common = self.root / '.artifacts/recipe-sdks'
        sources = [(self.source, common / '_cookbook', 'nicodes/cookbook', self.revision),
                   (TOOLS_ROOT, common / '_tools', 'nicodes/tools', dependencies.tools_identity()['revision'])]
        for original, checkout, repository, revision in sources:
            subprocess.run(['git', 'clone', '--quiet', '--no-hardlinks', str(original), str(checkout)],
                check=True, capture_output=True, text=True, timeout=30)
            subprocess.run(['git', '-C', str(checkout), 'remote', 'set-url', 'origin',
                            'https://github.com/' + repository + '.git'], check=True)
            sdk_identity.verify(checkout, revision, repository=repository)
        output = self.base / 'first-bind-environment'
        environment = {**self.environment, 'COOKBOOK_REF': self.revision,
                       'TOOLS_REF': dependencies.tools_identity()['revision'], 'GITHUB_ENV': str(output)}
        environment.pop('TOOLS_ROOT', None)
        environment.pop('COOKBOOK_ROOT', None)
        result = subprocess.run(sdk_source.python_entry(sys.executable,
            common / '_cookbook/cookbook/provider_dependency.py'), cwd=self.root, env=environment,
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(output.read_text().splitlines(), [
            'COOKBOOK_ROOT=' + str((common / '_cookbook').resolve()), 'COOKBOOK_REF=' + self.revision,
            'TOOLS_ROOT=' + str((common / '_tools').resolve()),
            'TOOLS_REF=' + dependencies.tools_identity()['revision'],
            'PYTHONDONTWRITEBYTECODE=1', 'MISE_TASK_RUN_AUTO_INSTALL=false',
            'MISE_AUTO_INSTALL=false', 'MISE_EXEC_AUTO_INSTALL=false'])
        self.assertFalse((self.root / '.artifacts/tools').exists())
        for _, checkout, repository, revision in sources:
            sdk_identity.verify(checkout, revision, repository=repository)


if __name__ == '__main__':
    unittest.main()
