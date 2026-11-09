"""HTTP SDK preparation uses provider pins on fresh application workers."""
from source_fixture import SDK_ROOT
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import workload_fixture as fixture
import dependencies


class WorkloadSDK(unittest.TestCase):
    def test_caddy_identity_uses_explicit_provider_go_without_ambient_build_hooks(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);version=fixture.go_version()
            environment={'GOENV':'/untrusted/goenv','GOROOT':'/untrusted/go','GOARCH':'arm','GOOS':'other',
                'GOCACHEPROG':'untrusted-wrapper','GOFLAGS':'-overlay=/untrusted/overlay','PATH':'retained'}
            with patch.object(fixture.subprocess,'check_output',return_value='go'+version+'\n') as query:
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
            with patch.object(fixture.komizo_core,'prepare'),patch.object(fixture.subprocess,'check_output',return_value='go'+version+'\n'):
                fixture.prepare(root,{},run)
                accepted=fixture.caddy_binary(root,{})
            self.assertEqual(calls[0][0],['mise','install','go'])
            self.assertEqual(calls[0][3],{'timeout':300})
            self.assertEqual(calls[0][2]['MISE_NO_HOOKS'],'1')
            self.assertEqual(calls[0][2]['MISE_NO_ENV'],'1')
            self.assertEqual(calls[0][2]['MISE_NO_CONFIG'],'0')
            self.assertEqual(calls[0][2]['MISE_GLOBAL_CONFIG_FILE'],__import__('os').devnull)
            self.assertEqual(calls[1][0],['mise','exec','go@'+version,'--','sh','build.sh'])
            self.assertEqual(calls[1][3],{'timeout':600})
            self.assertEqual(accepted,root/fixture.SDK/'caddy')
            with patch.object(fixture.subprocess,'check_output',side_effect=FileNotFoundError('missing provider Go')):
                with self.assertRaises(FileNotFoundError):fixture.caddy_binary(root,{})
            self.assertEqual(len(calls),2)

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


if __name__=='__main__':unittest.main()
