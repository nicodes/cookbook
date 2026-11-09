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
                if arguments==['mise','exec','go@'+version,'--','sh','build.sh']:
                    (cwd/'caddy').write_bytes(b'fixture executable; actual HTTP execution is checked separately')
            with patch.object(fixture.komizo_core,'prepare'),patch.object(fixture.subprocess,'check_output',return_value='go'+version+'\n'):
                fixture.prepare(root,{},run)
                accepted=fixture.caddy_binary(root,{})
            self.assertEqual(calls[0][0],['mise','install','go@'+version])
            self.assertEqual(calls[0][3],{'timeout':300})
            self.assertEqual(calls[1][0],['mise','exec','go@'+version,'--','sh','build.sh'])
            self.assertEqual(calls[1][3],{'timeout':600})
            self.assertEqual(accepted,root/fixture.SDK/'caddy')
            with patch.object(fixture.subprocess,'check_output',side_effect=FileNotFoundError('missing provider Go')):
                with self.assertRaises(FileNotFoundError):fixture.caddy_binary(root,{})
            self.assertEqual(len(calls),2)


if __name__=='__main__':unittest.main()
