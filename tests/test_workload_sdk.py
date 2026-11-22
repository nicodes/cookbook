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
    def test_published_proxy_is_selected_only_for_exact_reviewed_inputs(self):
        environment = {'GITHUB_ACTIONS': 'true'}
        with patch.object(fixture.sys, 'platform', 'linux'), \
             patch.object(fixture.platform, 'machine', return_value='aarch64'), \
             patch.object(fixture, 'go_version', return_value=fixture.CADDY_ASSET_GO), \
             patch.object(fixture, 'caddy_source', return_value=fixture.CADDY_ASSET_SOURCE) as source:
            self.assertEqual(fixture.published_caddy(environment), (
                fixture.CADDY_ASSET_RELEASE + 'caddy_Linux_arm64', fixture.CADDY_ASSET_DIGESTS['arm64']))
            source.return_value = fixture.CADDY_ASSET_SOURCE | {'caddy.go.mod': 'changed'}
            self.assertIsNone(fixture.published_caddy(environment))
            self.assertIsNone(fixture.published_caddy({}))

    def test_published_proxy_reuses_only_verified_bytes_and_never_installs_go(self):
        body = b'exact fixture proxy'
        digest = hashlib.sha256(body).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            url = 'https://example.invalid/caddy'
            with patch.object(fixture, 'published_caddy', return_value=(url, digest)), \
                 patch.object(fixture.komizo_core, 'install_tool') as install, \
                 patch.object(fixture.urllib.request, 'urlopen', return_value=io.BytesIO(body)) as fetch:
                fixture.prepare_caddy(root, {}, None)
                fixture.prepare_caddy(root, {}, None)
                install.assert_not_called()
                fetch.assert_called_once_with(url, timeout=60)
                binary = fixture.caddy_binary(root, {})
                self.assertEqual(binary.read_bytes(), body)
                binary.write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                    fixture.caddy_binary(root, {})
                fetch.return_value = io.BytesIO(b'corrupt download')
                with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                    fixture.prepare_caddy(root, {}, None)
                self.assertEqual(binary.read_bytes(), b'changed')

    def test_renderer_and_proxy_preparation_can_progress_independently(self):
        proxy_started = threading.Event()
        def renderer(root, environment, run):
            self.assertTrue(proxy_started.wait(5))
            raise ValueError('renderer rejected')

        with patch.object(fixture.komizo_core, 'prepare', side_effect=renderer), \
                patch.object(fixture, 'prepare_caddy', side_effect=lambda *_: proxy_started.set()):
            with self.assertRaisesRegex(ValueError, 'renderer rejected'):
                fixture.prepare(Path.cwd(), {}, None)

    def go_query(self, value):
        native=subprocess.check_output
        def query(arguments,*args,**options):
            if arguments[:2]==['mise','exec'] and arguments[-2:]==['env','GOVERSION']:
                if isinstance(value,Exception):raise value
                return value
            return native(arguments,*args,**options)
        return query

    def test_caddy_identity_uses_explicit_provider_go_without_ambient_build_hooks(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);version=fixture.go_version()
            environment={'GOENV':'/untrusted/goenv','GOROOT':'/untrusted/go','GOARCH':'arm','GOOS':'other',
                'GOCACHEPROG':'untrusted-wrapper','GOFLAGS':'-overlay=/untrusted/overlay','PATH':'retained'}
            with patch.object(fixture.subprocess,'check_output',side_effect=self.go_query('go'+version+'\n')) as query:
                identity=fixture.caddy_expected(root,environment)
            self.assertEqual(query.call_args.args[0],['mise','exec','go@'+version,'--','go','env','GOVERSION'])
            actual=query.call_args.kwargs['env']
            self.assertEqual(actual['GOENV'],'off');self.assertEqual(actual['GOTOOLCHAIN'],'local')
            self.assertEqual(actual['CGO_ENABLED'],'0');self.assertNotIn('GOCACHEPROG',actual)
            self.assertNotIn('GOROOT',actual);self.assertNotIn('GOARCH',actual);self.assertNotIn('GOOS',actual)
            self.assertEqual(actual['GOFLAGS'],'-buildvcs=false');self.assertEqual(identity['go'],version)
            self.assertEqual(set(identity['source_sha256']),{'caddy.go.mod','caddy.go.sum','caddy-build.sh'})

    def test_fresh_sdk_installs_provider_go_then_builds_with_that_exact_toolchain(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);calls=[];version=fixture.go_version()
            def run(arguments,cwd,environment,**options):
                calls.append((arguments,cwd,environment,options))
                if arguments==['mise','install','go']:
                    import tomllib
                    self.assertEqual(tomllib.loads((cwd/'mise.toml').read_text()),{'tools':{'go':version}})
                    self.assertFalse(dependencies.helper('rust_install_context').needs_rust(cwd,'go'))
                if arguments==['mise','exec','go@'+version,'--','sh','build.sh']:
                    (cwd/'caddy').write_bytes(b'fixture executable; actual HTTP execution is checked separately')
            with patch.object(fixture.komizo_core,'prepare'),patch.object(fixture.subprocess,'check_output',side_effect=self.go_query('go'+version+'\n')):
                fixture.prepare(root,{},run)
                accepted=fixture.caddy_binary(root,{})
            self.assertEqual(calls[0][0],['mise','install','go'])
            self.assertEqual(calls[0][3],{'timeout':300})
            self.assertEqual(calls[0][2]['MISE_NO_HOOKS'],'1')
            self.assertEqual(calls[0][2]['MISE_NO_ENV'],'1')
            self.assertEqual(calls[0][2]['MISE_NO_CONFIG'],'0')
            self.assertEqual(Path(calls[0][2]['MISE_GLOBAL_CONFIG_FILE']).name,'global.toml')
            self.assertEqual(calls[1][0],['mise','exec','go@'+version,'--','sh','build.sh'])
            self.assertEqual(calls[1][3],{'timeout':600})
            self.assertEqual(accepted,root/fixture.SDK/'caddy')
            with patch.object(fixture.subprocess,'check_output',side_effect=self.go_query(FileNotFoundError('missing provider Go'))):
                with self.assertRaises(FileNotFoundError):fixture.caddy_binary(root,{})
            self.assertEqual(calls[2][0],['mise','exec','go@'+version,'--','go','mod','verify'])
            self.assertEqual(calls[2][3],{'timeout':300})
            self.assertTrue(Path(calls[1][2]['GOMODCACHE']).is_relative_to(calls[1][1]))
            self.assertEqual(calls[1][2]['GOSUMDB'],'sum.golang.org')
            self.assertEqual(len(calls),3)

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
