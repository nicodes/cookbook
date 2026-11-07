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
        self.base = Path(temporary.name)
        self.root = self.base / 'caller with spaces'
        self.root.mkdir()
        self.source = self.base / 'Cookbook SDK with spaces'
        (self.source / 'cookbook').mkdir(parents=True)
        (self.source / 'mise.toml').write_text('[vars]\ntools_contract=1\ntools_ref="' + dependencies.tools_identity()['revision'] + '"\n')
        (self.source / 'cookbook/sibling.py').write_text('VALUE="trusted sibling"\n')
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


if __name__ == '__main__':
    unittest.main()
