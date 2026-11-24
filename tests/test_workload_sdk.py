"""HTTP SDK preparation uses provider pins on fresh application workers."""
from source_fixture import SDK_ROOT
import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import workload_fixture as fixture
import dependencies


class WorkloadSDK(unittest.TestCase):
    def test_actions_renderer_uses_its_attested_release_and_keeps_the_pinned_source(self):
        core = fixture.komizo_core
        selected = core.pin()
        environment = {'GITHUB_ACTIONS': 'true'}
        host = 'x86_64-unknown-linux-gnu'
        body = b'expected renderer bytes'
        digest = hashlib.sha256(body).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, source, _ = core.locations(root, selected, '1.94.0', host)
            source.mkdir(parents=True)
            with patch.object(core.sys, 'platform', 'linux'), \
                 patch.object(core, 'RENDERER_DIGESTS', {'x86_64': digest}), \
                 patch.object(core, 'install_tool'), patch.object(core, 'rust', return_value=('1.94.0', host)), \
                 patch.object(core.subprocess, 'check_output', return_value='ahead\n'), \
                 patch.object(core.sdk_source, 'verify') as verify, \
                 patch.object(fixture.urllib.request, 'urlopen', return_value=io.BytesIO(body)) as fetch:
                self.assertIsNone(core.published_renderer(selected, {}, '1.94.0', host))
                self.assertIsNone(core.published_renderer(selected, environment, '1.95.0', host))
                path = core.prepare(root, environment, None)
                self.assertEqual(path.read_bytes(), body)
                fetch.assert_called_once_with(core.RENDERER_RELEASE + 'komizo-box_Linux_x86_64', timeout=60)
                verify.assert_called_with(source, core.RENDERER_REVISION, repository=selected['repository'])
                path.write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                    core.binary(root, environment)

    def test_fixture_proxy_is_the_pinned_official_binary_at_the_companion_location(self):
        from test_native_proxy import pins
        native = fixture.native_proxy
        table, body, packed = pins('Linux/arm64')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(fixture.sys, 'platform', 'linux'), \
                 patch.object(fixture.platform, 'machine', return_value='aarch64'), \
                 patch.dict(native.PLATFORMS, table), \
                 patch.object(native.artifact_packet.urllib.request, 'urlopen', return_value=io.BytesIO(packed)) as fetch:
                self.assertEqual(fixture.proxy_platform(), 'Linux/arm64')
                fixture.prepare_caddy(root, {'GITHUB_ACTIONS': 'true'}, None)
                fixture.prepare_caddy(root, {}, None)
                fetch.assert_called_once_with(native.RELEASE + 'caddy_2.11.9_linux_arm64.tar.gz', timeout=60)
                binary = fixture.caddy_binary(root, {})
                self.assertEqual(binary, native.output(root, 'Linux/arm64'))
                self.assertEqual(binary.read_bytes(), body)
                binary.write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, 'changed'):
                    fixture.caddy_binary(root, {})
                fetch.return_value = io.BytesIO(packed + b'corrupt download')
                with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                    fixture.prepare_caddy(root, {}, None)
                self.assertEqual(binary.read_bytes(), b'changed')

    def test_fixture_proxy_needs_a_reviewed_platform_or_an_explicit_local_staging(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(fixture.sys, 'platform', 'darwin'), \
                patch.object(fixture.native_proxy.artifact_packet.urllib.request, 'urlopen') as fetch:
            with self.assertRaisesRegex(ValueError, 'reviewed Linux Caddy'):
                fixture.prepare_caddy(Path(directory), {}, None)
            fetch.assert_not_called()
            staged = Path(directory) / 'caddy'
            staged.write_bytes(b'staged')
            self.assertEqual(fixture.caddy_binary(Path(directory), {'KOMIZO_CADDY': str(staged)}), staged)
            with self.assertRaisesRegex(ValueError, 'immutable prepared'):
                fixture.caddy_binary(Path(directory), {'KOMIZO_CADDY': str(staged), 'GITHUB_ACTIONS': 'true'})

    def test_renderer_and_proxy_preparation_can_progress_independently(self):
        proxy_started = threading.Event()
        def renderer(root, environment, run):
            self.assertTrue(proxy_started.wait(5))
            raise ValueError('renderer rejected')

        with patch.object(fixture.komizo_core, 'prepare', side_effect=renderer), \
                patch.object(fixture, 'prepare_caddy', side_effect=lambda *_: proxy_started.set()):
            with self.assertRaisesRegex(ValueError, 'renderer rejected'):
                fixture.prepare(Path.cwd(), {}, None)

    def test_provider_rust_installer_keeps_the_existing_declared_tool_guard(self):
        version=fixture.komizo_core.rust_version();calls=[]
        def install(arguments,cwd,environment,**options):
            self.assertEqual(arguments,['mise','install','rust'])
            self.assertTrue(dependencies.helper('rust_install_context').needs_rust(cwd,'rust'))
            self.assertEqual(__import__('tomllib').loads((cwd/'mise.toml').read_text()),{'tools':{'rust':version}})
            self.assertNotIn('MISE_CONFIG_FILE',environment)
            self.assertNotIn('MISE_ENV',environment)
            self.assertEqual(environment['MISE_NO_HOOKS'],'1')
            calls.append(True)
        fixture.komizo_core.install_tool('rust',version,{'MISE_CONFIG_FILE':'/untrusted','MISE_ENV':'production'},install)
        self.assertEqual(calls,[True])

    def test_actual_mise_parses_only_the_provider_tool_context_without_installation(self):
        import json
        version=fixture.komizo_core.rust_version()
        def inspect(_arguments,cwd,environment,**_options):
            for name in ['MISE_GLOBAL_CONFIG_FILE','MISE_SYSTEM_CONFIG_FILE']:
                self.assertEqual(Path(environment[name]).read_bytes(),b'')
            raw=subprocess.check_output(['mise','ls','--json','--current'],cwd=cwd,env=environment,text=True,timeout=30)
            record=json.loads(raw)
            self.assertEqual(set(record),{'rust'})
            self.assertEqual(record['rust'][0]['requested_version'],version)
        fixture.komizo_core.install_tool('rust',version,dict(__import__('os').environ),inspect)


if __name__=='__main__':unittest.main()
