"""Use a real finite fake APT child; never install packages on the test host."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('ubuntu_ci', Path(__file__).parents[1] / 'cookbook/ubuntu_ci.py')
ubuntu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ubuntu)


class UbuntuPreparation(unittest.TestCase):
    def setUp(self):
        if os.name != 'posix' or not Path('/usr/bin/timeout').is_file():
            self.skipTest('real GNU timeout control runs on Linux')
        self.temporary = tempfile.TemporaryDirectory(prefix='Ubuntu CI control ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.release = self.root / 'os-release'
        self.release.write_text('ID=ubuntu\nVERSION_ID="24.04"\nVERSION_CODENAME=noble\n')
        self.keyring = self.root / 'existing-archive-keyring.gpg'
        self.keyring.write_bytes(b'existing fixture keyring, not imported or modified')
        self.arch = self.root / 'dpkg'
        self.arch.write_text('#!/usr/bin/python3\nprint("amd64")\n')
        self.arch.chmod(0o755)
        self.apt = self.root / 'apt-get'
        self.log = self.root / 'apt-calls.jsonl'
        self.fake_apt()
        for name, value in [('OS_RELEASE', self.release), ('KEYRING', self.keyring),
                ('DPKG', str(self.arch)), ('APT', str(self.apt))]:
            selected = patch.object(ubuntu, name, value)
            selected.start(); self.addCleanup(selected.stop)
        selected = patch.object(ubuntu.os, 'geteuid', return_value=0)
        selected.start(); self.addCleanup(selected.stop)

    def fake_apt(self, *, fail_update=False, sleep=0, install_sleep=0):
        self.apt.write_text('''#!/usr/bin/python3
import json,os,pathlib,sys,time
arguments=sys.argv[1:];options={}
while arguments and arguments[0]=='-o':
 key,value=arguments[1].split('=',1);options.setdefault(key,[]).append(value);arguments=arguments[2:]
config=pathlib.Path(os.environ['APT_CONFIG']);root=config.parent
assert os.environ['DEBIAN_FRONTEND']=='noninteractive'
assert 'http_proxy' not in os.environ and 'GITHUB_TOKEN' not in os.environ
assert root.stat().st_mode&0o777==0o755
assert config.stat().st_mode&0o777==0o644
for name in ['sourceparts','confparts','state','state/lists','cache','cache/archives','log']:
 assert (root/name).stat().st_mode&0o777==0o755
assert not list((root/'sourceparts').iterdir()) and not list((root/'confparts').iterdir())
source=pathlib.Path(options['Dir::Etc::sourcelist'][0])
assert source.stat().st_mode&0o777==0o644
row={'phase':arguments[0],'arguments':arguments,'options':options,'sources':source.read_text(),
 'config':config.read_text(),'root':str(root)}
with open(''' + repr(str(self.log)) + ''','a') as output:output.write(json.dumps(row)+'\\n')
if arguments[0]=='update':
 time.sleep(''' + repr(sleep) + ''')
 sys.exit(''' + ('9' if fail_update else '0') + ''')
time.sleep(''' + repr(install_sleep) + ''')
''')
        self.apt.chmod(0o755)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def prepare(self, profile='linker'):
        with contextlib.redirect_stdout(io.StringIO()):
            ubuntu.prepare(profile)

    def test_same_private_options_strict_signatures_and_closed_packages_in_both_phases(self):
        original = self.keyring.read_bytes()
        with patch.dict(os.environ, {'APT_CONFIG': '/forbidden/global.conf',
                'http_proxy': 'http://forbidden.invalid', 'GITHUB_TOKEN': 'private-marker'}):
            for profile in ubuntu.PROFILES:
                self.prepare(profile)
                update, install = self.calls()[-2:]
                self.assertEqual(update['options'], install['options'])
                self.assertEqual(update['arguments'], ['update', '--error-on=any'])
                self.assertEqual(install['arguments'], ['install', '--yes', '--no-install-recommends', *ubuntu.PROFILES[profile]])
                options = update['options']
                self.assertEqual(options['Dir::State::status'], ['/var/lib/dpkg/status'])
                self.assertEqual(options['Acquire::Check-Date'], ['true'])
                self.assertEqual(options['Acquire::Check-Valid-Until'], ['true'])
                self.assertEqual(options['APT::Get::AllowUnauthenticated'], ['false'])
                self.assertEqual(options['Acquire::AllowInsecureRepositories'], ['false'])
                self.assertEqual(options['Acquire::AllowDowngradeToInsecureRepositories'], ['false'])
                self.assertEqual(options['Acquire::http::Timeout'], ['20'])
                self.assertEqual(options['Acquire::https::Timeout'], ['20'])
                self.assertIn('Signed-By: '+str(self.keyring), update['sources'])
                self.assertIn('Architectures: amd64', update['sources'])
                self.assertIn('Suites: noble noble-updates noble-security noble-backports', update['sources'])
                self.assertNotIn('azure.archive', update['sources'])
                self.assertFalse(Path(update['root']).exists())
        self.assertEqual(self.keyring.read_bytes(), original)

    def test_all_supported_routes_and_umask_keep_public_state_traversable(self):
        for version, codename, arch in [('22.04','jammy','amd64'),('24.04','noble','amd64'),('24.04','noble','arm64')]:
            self.release.write_text(f'ID=ubuntu\nVERSION_ID={version}\nVERSION_CODENAME={codename}\n')
            self.arch.write_text('#!/usr/bin/python3\nprint('+repr(arch)+')\n')
            old = os.umask(0o077)
            try:self.prepare()
            finally:os.umask(old)
            self.assertIn('URIs: '+ubuntu.MIRRORS[codename, arch], self.calls()[-1]['sources'])
            self.assertIn('Architectures: '+arch, self.calls()[-1]['sources'])

    def test_failed_update_never_installs_or_retries_and_removes_only_owned_state(self):
        self.fake_apt(fail_update=True)
        sentinel = self.root / 'unrelated';sentinel.write_text('preserve')
        with self.assertRaises(subprocess.CalledProcessError):self.prepare()
        self.assertEqual([row['phase'] for row in self.calls()], ['update'])
        self.assertFalse(Path(self.calls()[0]['root']).exists())
        self.assertEqual(sentinel.read_text(), 'preserve')

    def test_one_original_deadline_terminates_the_actual_fake_apt_child(self):
        self.fake_apt(sleep=10)
        start = time.monotonic()
        with patch.object(ubuntu, 'PREPARATION_SECONDS', 3.3), self.assertRaises(subprocess.CalledProcessError):
            self.prepare()
        self.assertLess(time.monotonic()-start, 2)
        self.assertEqual([row['phase'] for row in self.calls()], ['update'])
        self.assertFalse(Path(self.calls()[0]['root']).exists())

    def test_unknown_profiles_hosts_architectures_and_missing_keyring_refuse_before_apt(self):
        with self.assertRaises(ValueError):self.prepare('arbitrary-package')
        for release in ['ID=debian\nVERSION_ID=24.04\nVERSION_CODENAME=noble\n',
                'ID=ubuntu\nVERSION_ID=24.04\nVERSION_CODENAME=jammy\n',
                'ID=ubuntu\nID=ubuntu\nVERSION_ID=24.04\nVERSION_CODENAME=noble\n']:
            self.release.write_text(release)
            with self.assertRaises(ValueError):self.prepare()
        self.release.write_text('ID=ubuntu\nVERSION_ID=24.04\nVERSION_CODENAME=noble\n')
        self.arch.write_text('#!/usr/bin/python3\nprint("amd64\\narm64")\n')
        with self.assertRaises(ValueError):self.prepare()
        self.arch.write_text('#!/usr/bin/python3\nprint("amd64")\n')
        self.keyring.unlink()
        with self.assertRaises(OSError):self.prepare()
        self.assertEqual(self.calls(), [])

    def test_install_consumes_update_elapsed_time_instead_of_receiving_a_fresh_deadline(self):
        self.fake_apt(sleep=1, install_sleep=2.5)
        start = time.monotonic()
        with patch.object(ubuntu, 'PREPARATION_SECONDS', 6), self.assertRaises(subprocess.CalledProcessError):
            self.prepare()
        self.assertEqual([row['phase'] for row in self.calls()], ['update', 'install'])
        self.assertLess(time.monotonic()-start, 5)

    def test_root_requirement_and_exhausted_deadline_do_not_launch_children(self):
        with patch.object(ubuntu.os, 'geteuid', return_value=1000), self.assertRaises(ValueError):
            self.prepare()
        with patch.object(ubuntu.subprocess, 'run', side_effect=AssertionError('expired command launched')):
            with self.assertRaises(TimeoutError):ubuntu.run(['forbidden'], time.monotonic(), {})


if __name__ == '__main__':
    unittest.main()
