from source_fixture import SDK_ROOT
import ast
import copy
import os
import json
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import compiler_cache as cache
import mise_entrypoint
import sdk_source


class CompilerCaches(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'api/.cargo').mkdir(parents=True)
        for directory in [self.root, self.root / 'api']:
            (directory / 'Cargo.toml').write_text('[package]\nname="fixture"\nversion="0.0.1"\n')
            (directory / 'Cargo.lock').write_text('version=4\n')
        (self.root / 'api/.cargo/config.toml').write_text('[build]\nrustflags=["-C", "debuginfo=0"]\n')
        (self.root / 'mise.toml').write_text('[tools]\nrust="1.94.0"\npython="3.13.11"\n')
        self.config = {'components': {'rust': [{'manifest': 'Cargo.toml'},
            {'manifest': 'api/Cargo.toml', 'cwd': 'api', 'target_directory': '.artifacts/api-target'}]}}
        self.environment = {'CARGO_TARGET_DIR': str(self.root / '.artifacts/rust-target'),
                            'CARGO_HOME': str(self.root / '.cargo'), 'GITHUB_JOB': 'native_one'}

    def plan(self, source='a' * 40, target='x86_64-unknown-linux-musl', image='ubuntu24-2.39'):
        return cache.plan(self.root, self.config, self.environment, source, 'Linux', 'X64', target, image=image)

    def test_two_runner_images_never_share_a_key_or_a_restore_key(self):
        old, new = self.plan(image='ubuntu22-2.35'), self.plan(image='ubuntu24-2.39')
        self.assertTrue(old['key'].startswith('compiler-v2-Linux-X64-ubuntu22-2.35-'))
        self.assertTrue(new['key'].startswith('compiler-v2-Linux-X64-ubuntu24-2.39-'))
        self.assertNotEqual(old['key'], new['key'])
        for restore in old['restore_keys']:
            self.assertFalse(any(other.startswith(restore) or restore.startswith(other)
                                 for other in [new['key'], *new['restore_keys']]), restore)
        for value in [old, new]:
            self.assertTrue(all(key.startswith('compiler-v2-') for key in [value['key'], *value['restore_keys']]))

    def test_image_identity_distinguishes_releases_without_imageos(self):
        def release(version):
            path = self.root / ('os-release-' + version)
            path.write_text('ID=ubuntu\nVERSION_ID="' + version + '"\n')
            return path
        old = cache.image_identity({}, 'Linux', release('22.04'), '2.35')
        new = cache.image_identity({}, 'Linux', release('24.04'), '2.39')
        self.assertEqual((old, new), ('ubuntu22.04-2.35', 'ubuntu24.04-2.39'))
        self.assertEqual(cache.image_identity({'ImageOS': 'ubuntu22'}, 'Linux', release('24.04'), '2.35'), 'ubuntu22-2.35')
        self.assertEqual(cache.image_identity({'ImageOS': 'macos15', 'ImageVersion': '20260101.1'}, 'Darwin'), 'macos15')
        self.assertEqual(cache.image_identity({'ImageOS': 'win22'}, 'Windows'), 'win22')
        self.assertNotIn('20260101', cache.image_identity({'ImageOS': 'ubuntu24', 'ImageVersion': '20260101.1'}, 'Linux', glibc='2.39'))
        self.assertEqual(cache.image_identity({}, 'Linux', self.root / 'missing', '2.31'), 'linux-2.31')

    def test_shell_wrapper_preserves_empty_and_save_arguments_with_nounset(self):
        action = SDK_ROOT / 'compiler-cache/action.yml'
        source = action.read_text().split('      run: |\n', 1)[1].split('    - name:', 1)[0]
        program = '\n'.join(line[8:] for line in source.splitlines())
        self.assertNotIn('arguments[@]', program)  # Bash 3.2 treats an empty array as unset.
        commands = self.root / 'bin'; commands.mkdir()
        python = commands / 'python3'
        python.write_text('#!' + sys.executable + '\nimport json,os,sys\nfrom pathlib import Path\nPath(os.environ["CALLS"]).write_text(json.dumps(sys.argv[1:]))\n')
        python.chmod(0o755)
        for mode, target in [('restore', ''), ('restore', 'aarch64-apple-darwin'), ('save', ''), ('save', 'aarch64-apple-darwin')]:
            environment = {**os.environ, 'PATH': str(commands) + os.pathsep + os.environ['PATH'],
                'GITHUB_ACTION_PATH': '/fixture with spaces/compiler-cache', 'CACHE_MODE': mode,
                'CACHE_TARGET': target, 'CALLS': str(self.root / 'calls.json')}
            result = subprocess.run([os.environ.get('CACHE_TEST_SHELL', 'bash'), '-c', program], env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            expected = ['/fixture with spaces/compiler-cache/../cookbook/compiler_cache.py', '--target', target]
            actual = json.loads((self.root / 'calls.json').read_text())
            self.assertEqual(actual[:3], ['-I', '-B', '-c'])
            self.assertEqual(ast.dump(ast.parse(actual[3]), include_attributes=False),
                             ast.dump(ast.parse(sdk_source.SDK_ENTRY), include_attributes=False))
            self.assertEqual(actual[4:], expected + (['--save'] if mode == 'save' else []))

    def test_only_actual_compiler_and_cargo_download_directories_are_selected(self):
        value = self.plan()
        paths = set(value['paths'])
        self.assertIn(str(self.root / '.artifacts/rust-target'), paths)
        self.assertIn(str(self.root / '.artifacts/api-target'), paths)
        self.assertIn(str(self.root / '.cargo/git/db'), paths)
        self.assertNotIn(str(self.root / '.cargo/credentials.toml'), paths)
        self.assertFalse(any('/verification' in value or '/dist' in value or 'godot' in value for value in paths))
        self.assertEqual(len(paths), 7)

    def test_source_changes_keep_safe_restore_prefix_but_sdk_lock_flags_and_target_change_keys(self):
        original = self.plan()
        next_source = self.plan('b' * 40)
        self.assertNotEqual(original['key'], next_source['key'])
        self.assertEqual(original['restore_keys'], next_source['restore_keys'])
        for change in ['sdk', 'lock', 'flags', 'target', 'job']:
            with self.subTest(change=change):
                old_environment = dict(self.environment)
                if change == 'sdk': (self.root / 'mise.toml').write_text('[tools]\nrust="1.95.0"\n')
                if change == 'lock': (self.root / 'Cargo.lock').write_text('version=4\n# changed dependencies\n')
                if change == 'flags': self.environment['RUSTFLAGS'] = '-C debuginfo=0'
                if change == 'job': self.environment['GITHUB_JOB'] = 'native_two'
                selected = self.plan(target='aarch64-unknown-linux-musl' if change == 'target' else 'x86_64-unknown-linux-musl')
                self.assertNotEqual(original['key'], selected['key'])
                self.environment = old_environment

    def test_regenerated_tasks_keep_compiler_inputs(self):
        path = self.root / 'mise.toml'
        sdk_settings = path.read_text()
        initial = self.plan()
        for revision, declaration in [('a' * 40, b'first recipe'), ('b' * 40, b'second recipe')]:
            path.write_text(mise_entrypoint.render(sdk_settings, revision, 'rust-cli', declaration,
                                                 ['lint', 'unit', 'security'], []))
            self.assertEqual(initial, self.plan())
        path.write_text(mise_entrypoint.render(sdk_settings + '\n[env]\nRUSTFLAGS="-C debuginfo=0"\n',
                                             'b' * 40, 'rust-cli', b'second recipe',
                                             ['lint', 'unit', 'security'], []))
        self.assertNotEqual(initial['restore_keys'], self.plan()['restore_keys'])

    def test_cache_paths_cannot_include_product_evidence_or_escape_the_checkout(self):
        for path in ['.artifacts/verification', 'dist', '../outside-target', '.artifacts/*/target']:
            self.config['components']['rust'][0]['target_directory'] = path
            with self.subTest(path=path), self.assertRaises(ValueError): self.plan()
        self.config['components']['rust'][0].pop('target_directory')
        (self.root / '.cargo').symlink_to(self.root / 'api', target_is_directory=True)
        with self.assertRaises(ValueError): self.plan()

    def test_staged_recipe_components_use_their_native_cargo_adapter_and_keep_raw_identity(self):
        for recipe in ['react-native', 'godot-game']:
            with self.subTest(recipe=recipe):
                self.config['recipe'] = recipe
                component = self.config['components']['rust'][0]
                component.update(output_directory='.artifacts/bin', output_names={'fixture':'fixture'},
                                 environment={'BUILD_REVISION':'{revision}'})
                first = self.plan()
                component['output_directory'] = '.artifacts/other-bin'
                self.assertNotEqual(first['key'], self.plan()['key'])
                component['environment'] = {'GITHUB_SHA':'untrusted'}
                with self.assertRaisesRegex(ValueError, 'authority'):self.plan()
                component['environment'] = {}
                component['unknown_cargo_option'] = True
                with self.assertRaisesRegex(ValueError, 'unsupported Cargo'):self.plan()
                component.pop('unknown_cargo_option')

    def test_workspace_member_without_a_duplicate_lock_keeps_enclosing_manifest_and_lock_identity(self):
        (self.root/'api/Cargo.lock').unlink()
        self.config['components']['rust'] = [self.config['components']['rust'][1]]
        original = self.plan()
        (self.root/'Cargo.toml').write_text('[workspace]\nmembers=["api"]\n')
        changed = self.plan()
        self.assertNotEqual(original['key'], changed['key'])
        (self.root/'Cargo.lock').write_text('version=4\n# changed workspace dependencies\n')
        self.assertNotEqual(changed['key'], self.plan()['key'])
        (self.root/'Cargo.lock').unlink()
        with self.assertRaisesRegex(ValueError, 'lockfile'):self.plan()

    def test_save_is_bounded_and_skips_empty_or_linked_payloads_without_breaking_validation(self):
        directory = self.root / 'target'; directory.mkdir()
        self.assertFalse(cache.within_budget([str(directory)]))
        (directory / 'intermediate').write_bytes(b'compiled')
        self.assertTrue(cache.within_budget([str(directory)]))
        with patch.object(cache, 'MAX_BYTES', 1): self.assertFalse(cache.within_budget([str(directory)]))
        (directory / 'linked').symlink_to(self.root / 'Cargo.lock')
        self.assertFalse(cache.within_budget([str(directory)]))


if __name__ == '__main__': unittest.main()
