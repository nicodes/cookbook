from source_fixture import TOOLS_ROOT
from source_fixture import SDK_ROOT
"""Prepare official SDK fixtures explicitly; native host checks stay offline."""
import hashlib
import importlib.util
import os
from pathlib import Path
import runpy
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = SDK_ROOT
sys.path.insert(0, str(ROOT/'cookbook'))
import godot_ci
import godot_game as game
import playwright_sdk
import sdk_source

spec = importlib.util.spec_from_file_location('host_template_installer', TOOLS_ROOT/'helpers/godot-setup.py')
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)
VERSION = '4.7.2'
EXPECTED_VERSION = '4.7.2.stable.official.ed1daf0bf'
BINARY_NAME = 'Godot_v4.7.2-stable_linux.x86_64.zip'
BINARY_PIN = 'sha256:cadd3204e728a35d3f13adb7fd0d7902636b79f6b95c40c265eb73b6c35329e4'
TEMPLATES_NAME = 'Godot_v4.7.2-stable_export_templates.tpz'
TEMPLATES_PIN = 'sha512:ca4d71c4d7b81dfc15d1a98baa07534aa95b03fdda78a0075b06672e1648d2e5f40980c9adc28d23e1b92e732ee7bf3461997aa804af74ec2fcd7a93ccb84079'
CACHE_VARIABLE = 'GODOT_HOST_TEMPLATE_CACHE'
MARKER = 'NATIVE_HOST_TEMPLATES_OK: pinned whole ELF hydrated offline; old host missing; corrupt archive refused; unit/browser unchanged'


def prepare(cache, binary_cache):
    """Called only by the explicit CI setup step, never a test method."""
    archives = cache/'.artifacts/godot/archives'
    archives.mkdir(parents=True, exist_ok=True)
    base = 'https://github.com/godotengine/godot-builds/releases/download/4.7.2-stable/'
    for name, pin in [(BINARY_NAME, BINARY_PIN), (TEMPLATES_NAME, TEMPLATES_PIN)]:
        target = archives/name
        existing = binary_cache/name
        if name == BINARY_NAME and engine.verified(existing, pin):
            shutil.copyfile(existing, target)
        engine.fetch(base+name, target, pin)
    print('Prepared exact official host SDK archives; native controls will not fetch.')


def pins():
    return {'GODOT_VERSION': VERSION, 'GODOT_EXPECTED_VERSION': EXPECTED_VERSION,
            'GODOT_BINARY_CHECKSUM': BINARY_PIN, 'GODOT_TEMPLATES_CHECKSUM': TEMPLATES_PIN}


def fixture(root):
    (root/'app').mkdir(exist_ok=True)
    (root/'app/project.godot').write_text('config_version=5\n')
    (root/'mise.toml').write_text(
        '[tools]\npython="3.13.11"\nactionlint="1.7.8"\n"github:aviorstudio/gdlint"="v1.0.0"\nnode="24.3.0"\n'
        '[env]\n'+''.join(key+'="'+value+'"\n' for key, value in pins().items()))
    return {'components': {'godot': {}}, 'tests': {}}


def worker(root, kind, execute=None, setup=None):
    """Exercise real worker selection; unrelated SDK effects are capture-only."""
    config = fixture(root)
    calls = []
    def run(command, directory, environment, timeout=None):
        calls.append(command)
        native = entry_command(command)
        if execute is not None and native[1:2] == [str(TOOLS_ROOT/'helpers/godot-setup.py')]:
            execute(native, directory, environment)
    entry = {'kind': kind, 'cases': []}
    if setup:
        entry['setup'] = setup
    with patch.object(game, 'run', side_effect=run), \
            patch.object(game, 'import_native') as imports, \
            patch.object(godot_ci, 'selected_entries', return_value=[]), \
            patch.object(playwright_sdk, 'install'):
        godot_ci.setup_worker(root, config, {}, entry)
    return calls, imports.call_count


def entry_command(command):
    """Retain every native argument while asserting SDK interpreter isolation."""
    if command[:1] != [sys.executable]:
        return command
    if command[1:5] != ['-I', '-B', '-c', sdk_source.SDK_ENTRY]:
        raise AssertionError('SDK helper requires the fixed isolated Python entrypoint')
    return [command[0], *command[5:]]


def setup_command(calls):
    matches = [entry_command(call) for call in calls
               if entry_command(call)[1:2] == [str(TOOLS_ROOT/'helpers/godot-setup.py')]]
    if len(matches) != 1:
        raise AssertionError('exactly one native engine setup command required')
    return matches[0]


class HostTemplateSetupControls(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_host_selects_existing_pinned_templates_only_in_explicit_setup(self):
        command = setup_command(worker(self.root, 'host')[0])
        self.assertEqual(command[-3:], ['--templates-checksum', TEMPLATES_PIN, '--templates'])
        self.assertEqual(command[command.index('--binary-checksum')+1], BINARY_PIN)
        self.assertEqual(command[command.index('--version')+1], VERSION)
        self.assertEqual(command[command.index('--root')+1], str(self.root/'.artifacts/godot'))

    def test_unit_engine_command_remains_exactly_the_prior_binary_only_command(self):
        command = setup_command(worker(self.root, 'unit')[0])
        self.assertEqual(command, [sys.executable, str(TOOLS_ROOT/'helpers/godot-setup.py'),
            '--version', VERSION, '--expected-version', EXPECTED_VERSION,
            '--binary-checksum', BINARY_PIN, '--root', str(self.root/'.artifacts/godot'),
            '--origin', 'godot'])

    def test_browser_capability_never_selects_engine_or_templates(self):
        calls, imports = worker(self.root, 'capability', setup='browser')
        self.assertEqual(calls, [['mise', 'install', 'node', 'python']])
        self.assertEqual(imports, 0)

    def test_native_auxiliary_stays_without_engine_or_templates(self):
        calls, imports = worker(self.root, 'test', setup='native')
        self.assertEqual(calls, [['mise', 'install', 'python']])
        self.assertEqual(imports, 0)

    def test_godot_auxiliary_engine_command_remains_binary_only(self):
        command = setup_command(worker(self.root, 'test', setup='godot')[0])
        self.assertNotIn('--templates', command)
        self.assertNotIn('--templates-checksum', command)

    @unittest.skipUnless(os.environ.get(CACHE_VARIABLE), 'explicit official SDK fixture setup is required')
    def test_actual_prepared_host_restores_exact_template_offline_and_rejects_corruption(self):
        root = Path(os.environ[CACHE_VARIABLE]).absolute()
        archives = root/'.artifacts/godot/archives'
        for name, pin in [(BINARY_NAME, BINARY_PIN), (TEMPLATES_NAME, TEMPLATES_PIN)]:
            archive = archives/name
            self.assertFalse(archive.is_symlink())
            self.assertTrue(archive.is_file())
            self.assertTrue(engine.verified(archive, pin), 'explicit setup must prepare exact official archives')
        with zipfile.ZipFile(archives/TEMPLATES_NAME) as archive:
            names = [name for name in archive.namelist() if Path(name).name == 'linux_release.x86_64']
            self.assertEqual(len(names), 1)
            with archive.open(names[0]) as source:
                prefix = source.read(20)
                self.assertEqual(prefix[:5], b'\x7fELF\x02')
                self.assertEqual(int.from_bytes(prefix[18:20], 'little'), 62)
            with archive.open(names[0]) as source:
                expected = hashlib.file_digest(source, 'sha256').hexdigest()
        with tempfile.TemporaryDirectory(prefix='host-sdk-data-') as temporary:
            data = Path(temporary)
            template = data/'godot/export_templates'/f'{VERSION}.stable/linux_release.x86_64'
            environment = {'XDG_DATA_HOME': str(data), 'PATH': os.defpath}
            def offline(command, directory, _environment):
                with patch.dict(os.environ, environment, clear=True), \
                        patch.object(engine.urllib.request, 'urlopen', side_effect=AssertionError('native test attempted network')), \
                        patch.object(sys, 'argv', command[1:]):
                    runpy.run_path(command[1], run_name='__main__')
            command = setup_command(worker(root, 'host')[0])
            # The old worker prepared the correct engine but never its template.
            offline(command[:-3], root, {})
            self.assertFalse(template.exists())
            with self.assertRaises(FileNotFoundError):
                template.open('rb')
            # Execute the actual selected setup command with only URL I/O denied.
            worker(root, 'host', execute=offline)
            with template.open('rb') as source:
                self.assertEqual(hashlib.file_digest(source, 'sha256').hexdigest(), expected)
            original = archives/TEMPLATES_NAME
            saved = original.with_suffix('.prepared')
            self.assertFalse(saved.exists())
            original.rename(saved)
            try:
                original.write_bytes(b'corrupted cached official archive')
                with self.assertRaisesRegex(AssertionError, 'attempted network'):
                    worker(root, 'host', execute=offline)
                with template.open('rb') as source:
                    self.assertEqual(hashlib.file_digest(source, 'sha256').hexdigest(), expected)
            finally:
                original.unlink()
                saved.rename(original)
            self.assertTrue(engine.verified(original, TEMPLATES_PIN))
        print(MARKER, flush=True)


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--prepare':
        prepare(Path(sys.argv[2]).absolute(), Path(sys.argv[3]).absolute())
    else:
        unittest.main()
