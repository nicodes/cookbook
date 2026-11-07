from source_fixture import SDK_ROOT
"""Keep SDK setup explicit, checksum-closed and outside build/test operations."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import godot_sdk


class SdkControls(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='native SDK ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {'components': {'godot': {'rust_web': {}}}}
        self.pins = {'RUST_NIGHTLY': 'nightly-2026-03-06', 'EMSCRIPTEN_VERSION': '3.1.74',
                     'EMSDK_SOURCE_COMMIT': 'a' * 40, 'EMSDK_ARCHIVE_SHA256': 'b' * 64,
                     'LIBCLANG_VERSION': '18.1.1'}
        self.write_pins()

    def write_pins(self):
        (self.root / 'mise.toml').write_text('[env]\n' + '\n'.join(key + ' = ' + json.dumps(value) for key, value in self.pins.items()) + '\n')

    def archive(self, values):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w:gz') as archive:
            for name, value in values.items():
                info = tarfile.TarInfo('emsdk-fixture/' + name)
                info.size = len(value); info.mode = 0o755
                archive.addfile(info, io.BytesIO(value))
        return data.getvalue()

    def fixture(self):
        directory, _ = godot_sdk.inputs(self.root, self.config)
        directory.mkdir(parents=True)
        native = directory / 'clang-venv/native'; native.mkdir(parents=True)
        values = {'emsdk': b'#!/bin/sh\nexit 0\n',
                  'emsdk_env.sh': b'export GITHUB_SHA=wrong\nexport PATH="$(dirname "${BASH_SOURCE[0]}")/bin:$PATH"\n',
                  'bin/emcc': b'#!/bin/sh\necho "emcc (Emscripten gcc/clang-like replacement) 3.1.74"\n'}
        archive = directory / 'emsdk-source.tar.gz'; archive.write_bytes(self.archive(values))
        self.pins['EMSDK_ARCHIVE_SHA256'] = hashlib.sha256(archive.read_bytes()).hexdigest(); self.write_pins()
        sdk = directory / 'emsdk-3.1.74'; godot_sdk.bootstrap(archive, sdk, self.pins, restore=True)
        python = directory / 'clang-venv/bin/python'; python.parent.mkdir(parents=True)
        probe = {'version': '18.1.1', 'native': str(native)}
        python.write_text('#!' + sys.executable + '\nprint(' + repr(json.dumps(probe)) + ')\n'); python.chmod(0o755)
        (directory / 'sdk.json').write_text(json.dumps({'schema': 1, 'pins': self.pins}))
        return directory, archive, sdk

    def test_reviewed_bootstrap_rejects_missing_modified_or_escaping_members(self):
        directory, archive, sdk = self.fixture()
        godot_sdk.bootstrap(archive, sdk, self.pins)
        target = sdk / 'emsdk'; original = target.read_bytes(); target.write_bytes(b'modified')
        with self.assertRaisesRegex(ValueError, 'modified'):
            godot_sdk.bootstrap(archive, sdk, self.pins, restore=True)
        target.write_bytes(original); target.unlink()
        with self.assertRaisesRegex(ValueError, 'missing'):
            godot_sdk.bootstrap(archive, sdk, self.pins)
        godot_sdk.bootstrap(archive, sdk, self.pins, restore=True)
        archive.write_bytes(self.archive({'../escape': b'rejected'}))
        self.pins['EMSDK_ARCHIVE_SHA256'] = hashlib.sha256(archive.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'unsafe'):
            godot_sdk.bootstrap(archive, sdk, self.pins, restore=True)
        self.assertFalse((directory / 'escape').exists())

    def test_activation_preserves_source_context_and_never_installs_tools(self):
        self.fixture()
        env = {**os.environ, 'GITHUB_SHA': 'c' * 40}
        with patch.object(godot_sdk, 'setup', side_effect=AssertionError('hidden setup')):
            activated = godot_sdk.activate(self.root, self.config, env)
        self.assertEqual(activated['GITHUB_SHA'], 'c' * 40)
        self.assertEqual(activated['RUST_NIGHTLY'], self.pins['RUST_NIGHTLY'])
        self.assertTrue(Path(activated['LIBCLANG_PATH']).is_dir())
        with self.assertRaisesRegex(ValueError, 'ambient SDK selection'):
            godot_sdk.activate(self.root, self.config, {**env, 'RUST_NIGHTLY': 'nightly-2026-03-07'})

    def test_explicit_setup_uses_reviewed_cached_bytes_and_preserves_command_deadlines(self):
        self.fixture()
        calls = []
        execute = lambda command, root, environment, timeout: calls.append((command, timeout))
        with patch.object(godot_sdk.urllib.request, 'urlopen', side_effect=AssertionError('unexpected download')):
            godot_sdk.setup(self.root, self.config, os.environ.copy(), execute)
        self.assertEqual([deadline for _, deadline in calls], [600, 600, 180, 180])
        self.assertEqual([calls[i][0][1:] for i in [0, 1]], [['install', '3.1.74'], ['activate', '3.1.74']])
        self.assertIn('--copies', calls[2][0])
        self.assertEqual(calls[3][0][-1], 'libclang==18.1.1')
        self.assertFalse(any('rustup' in call for call, _ in calls), 'common Cargo setup owns the nightly toolchain')

    def test_stale_or_unpinned_toolchains_fail_without_repair(self):
        directory, archive, sdk = self.fixture()
        (directory / 'sdk.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'stale'):
            godot_sdk.activate(self.root, self.config, os.environ.copy())
        self.pins['RUST_NIGHTLY'] = 'nightly'; self.write_pins()
        with self.assertRaisesRegex(ValueError, 'exact caller Mise pin'):
            godot_sdk.inputs(self.root, self.config)
        for directory in ['app/toolchain', '.artifacts/toolchains/rust-web']:
            self.config['components']['godot']['rust_web'] = {'directory': directory}
            with self.subTest(directory=directory), self.assertRaisesRegex(ValueError, 'canonical directory'):
                godot_sdk.inputs(self.root, self.config)


if __name__ == '__main__':
    unittest.main()
