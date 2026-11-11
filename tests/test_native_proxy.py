"""Native proxy source, compiler, byte, and architecture admission controls."""
import sys
sys.dont_write_bytecode=True
from source_fixture import SDK_ROOT
from pathlib import Path
import copy
import hashlib
import json
import os
import struct
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import native_proxy as proxy


def elf(platform):
    body=bytearray(120);body[:6]=b'\x7fELF\x02\x01'
    struct.pack_into('<HH',body,16,2,proxy.PLATFORMS[platform][1])
    struct.pack_into('<Q',body,32,64);struct.pack_into('<HH',body,54,56,1)
    struct.pack_into('<I',body,64,1)
    return bytes(body)+b'exact independently reviewed companion'


def metadata(platform):
    return ('/owned/caddy: go1.27.4\n\tpath\tgithub.com/caddyserver/caddy/v2/cmd/caddy\n'
        '\tmod\tgithub.com/nicodes/cicd/caddy\t(devel)\n'
        '\tdep\tgithub.com/caddyserver/caddy/v2\tv2.11.6\th1:reviewed\n'
        '\tbuild\t-trimpath=true\n\tbuild\tGOOS=linux\n\tbuild\tGOARCH='+proxy.PLATFORMS[platform][2]+
        '\n\tbuild\tCGO_ENABLED=0\n')


class ProxyCompanions(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='native-proxy-control-');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.platform='Linux/x86_64';self.expected=proxy.expected(self.platform)

    def accepted(self,platform=None):
        platform=platform or self.platform;path=proxy.output(self.root,platform)
        path.parent.mkdir(parents=True,exist_ok=True);body=elf(platform);path.write_bytes(body);path.chmod(0o755)
        receipt=proxy.canonical(proxy.expected(platform)|{'binary_sha256':'sha256:'+hashlib.sha256(body).hexdigest()})
        proxy.receipt_path(path).write_bytes(receipt)
        return path,body,receipt

    def test_direct_upstream_command_build_identifies_caddy_as_its_main_module(self):
        actual=metadata(self.platform).replace('\tmod\tgithub.com/nicodes/cicd/caddy\t(devel)\n','').replace(
            '\tdep\tgithub.com/caddyserver/caddy/v2\t','\tmod\tgithub.com/caddyserver/caddy/v2\t')
        self.assertEqual(proxy.buildinfo(actual,self.platform),'mod')
        with self.assertRaises(ValueError):proxy.buildinfo(actual+'\tdep\tgithub.com/caddyserver/caddy/v2\tv2.11.6\th1:duplicate\n',self.platform)

    def test_unknown_cli_version_requires_the_verified_direct_upstream_main_module(self):
        direct=metadata(self.platform).replace('\tmod\tgithub.com/nicodes/cicd/caddy\t(devel)\n','').replace(
            '\tdep\tgithub.com/caddyserver/caddy/v2\t','\tmod\tgithub.com/caddyserver/caddy/v2\t')
        def run(command,working,values,**options):(working/'caddy').write_bytes(elf(self.platform))
        def capture(command,**options):
            if command[-2:]==['env','GOVERSION']:return 'go1.27.4\n'
            if '-m' in command:return direct
            return 'unknown\n'
        with patch.object(proxy,'_capture',side_effect=capture),patch.object(proxy,'_audit'):
            proxy.build(self.root,self.platform,{},run)
        direct=metadata(self.platform)
        with patch.object(proxy,'_capture',side_effect=capture),patch.object(proxy,'_audit'):
            with self.assertRaisesRegex(ValueError,'upstream version'):proxy.build(self.root,self.platform,{},run)

    def test_expected_receipt_binds_exact_tools_git_blobs_compiler_and_upstream(self):
        self.assertEqual(set(self.expected),{'kind','version','tools_revision','platform','go_version','caddy_version','source_sha256'})
        self.assertEqual(self.expected['kind'],'komizo.proxy-build')
        self.assertEqual(self.expected['go_version'],'1.27.4');self.assertEqual(self.expected['caddy_version'],'2.11.6')
        self.assertRegex(self.expected['tools_revision'],'^[a-f0-9]{40}$')
        self.assertEqual(set(self.expected['source_sha256']),set(proxy.SOURCES))
        for digest in self.expected['source_sha256'].values():self.assertRegex(digest,'^sha256:[a-f0-9]{64}$')
        for platform in proxy.PLATFORMS:
            path,body,receipt=self.accepted(platform)
            self.assertEqual(proxy.verify(self.root,platform),(body,receipt))
            self.assertEqual(path.name,'caddy_'+platform.replace('/','_'))

    def test_read_time_source_substitution_cannot_change_the_committed_builder(self):
        original=proxy.artifact_packet.read_regular
        def substituted(path,maximum):
            body=original(path,maximum)
            return body+b'\n# substituted after SDK admission\n' if Path(path).name=='caddy-build.sh' else body
        with patch.object(proxy.artifact_packet,'read_regular',side_effect=substituted):
            with self.assertRaisesRegex(ValueError,'immutable Git blobs'):proxy.expected(self.platform)

    def test_only_explicit_linux_proxy_companions_enable_setup(self):
        config={'package':{'extra_files':['caddy_Linux_arm64'],'extra_file_sources':{
            'caddy_Linux_arm64':{'provider':'caddy','platform':'Linux/arm64'}}}}
        self.assertTrue(proxy.enabled(config));self.assertFalse(proxy.enabled({}))
        for source in [{'provider':'caddy','platform':'Windows/x86_64'},
            {'provider':'caddy','platform':'Linux/arm64','path':'/unreviewed'},
            {'provider':'caddy','platform':'Linux/x86_64'}]:
            bad=copy.deepcopy(config);bad['package']['extra_file_sources']['caddy_Linux_arm64']=source
            with self.subTest(source=source),self.assertRaises(ValueError):proxy.enabled(bad)
        with self.assertRaises(ValueError):proxy.output(self.root,'Linux/../arm64')
        calls=[];proxy.prepare(self.root,{}, {},lambda *a,**k:calls.append(a));self.assertEqual(calls,[])

    def test_proxy_scanner_install_uses_only_bare_declared_tools_and_provider_go(self):
        calls=[]
        def inspect(arguments,cwd,values,**options):
            declaration=__import__('tomllib').loads((cwd/'mise.toml').read_text())['tools']
            self.assertEqual(arguments[:2],['mise','install'])
            self.assertIn(arguments[2],declaration)
            self.assertFalse(proxy.dependencies.helper('rust_install_context').needs_rust(cwd,arguments[2]))
            self.assertEqual(declaration['go'],'1.27.4')
            if arguments[2].startswith('go:'):
                self.assertEqual(set(declaration),{'go','go:golang.org/x/vuln/cmd/govulncheck'})
                actual=json.loads(__import__('subprocess').check_output(['mise','ls','--json','--current'],
                    cwd=cwd,env=values,text=True,timeout=30))
                self.assertEqual(actual['go'][0]['requested_version'],'1.27.4')
                self.assertEqual(actual['go:golang.org/x/vuln/cmd/govulncheck'][0]['requested_version'],declaration[arguments[2]])
            self.assertEqual(values['MISE_NO_ENV'],'1');self.assertEqual(values['MISE_NO_HOOKS'],'1')
            self.assertEqual(values['MISE_NO_CONFIG'],'0');self.assertNotIn('MISE_CONFIG_FILE',values)
            calls.append(arguments[2])
        proxy._tools(self.root,dict(os.environ,MISE_CONFIG_FILE='/unreviewed',GOROOT='/unreviewed'),inspect)
        self.assertEqual(calls,['go','go:golang.org/x/vuln/cmd/govulncheck'])

    def test_source_role_version_platform_and_raw_receipt_substitution_refuse(self):
        path,body,raw=self.accepted();receipt=proxy.receipt_path(path)
        for change in [{'kind':'other'},{'version':2},{'tools_revision':'f'*40},{'platform':'linux/arm64'},
            {'go_version':'1.27.0'},{'caddy_version':'2.11.5'},{'source_sha256':{}},
            {'binary_sha256':'sha256:'+'f'*64},{'extra':'unreviewed'}]:
            receipt.write_bytes(proxy.canonical(json.loads(raw)|change))
            with self.subTest(change=change),self.assertRaises(ValueError):proxy.verify(self.root,self.platform)
        for malformed in [raw+b'\n',b'{"version":1,"version":1}',raw.replace(b'"version":1',b'"version":true')]:
            receipt.write_bytes(malformed)
            with self.assertRaises(ValueError):proxy.verify(self.root,self.platform)
        receipt.write_bytes(raw);path.write_bytes(body+b'changed')
        with self.assertRaises(ValueError):proxy.verify(self.root,self.platform)

    def test_wrong_architecture_dynamic_interpreter_and_malformed_program_table_refuse(self):
        for body in [elf('Linux/arm64'),b'#!unreviewed',elf(self.platform)[:80],
            elf(self.platform)[:64]+struct.pack('<I',3)+elf(self.platform)[68:]]:
            with self.assertRaises(ValueError):proxy.elf(body,self.platform)
        body=bytearray(elf(self.platform));struct.pack_into('<H',body,56,65535)
        with self.assertRaises(ValueError):proxy.elf(body,self.platform)

    def test_binary_links_writable_executable_and_receipt_links_are_refused(self):
        path,body,raw=self.accepted();path.chmod(0o777)
        with self.assertRaises(ValueError):proxy.verify(self.root,self.platform)
        path.chmod(0o755);other=self.root/'other';os.link(path,other)
        with self.assertRaises(ValueError):proxy.verify(self.root,self.platform)
        other.unlink();receipt=proxy.receipt_path(path);saved=self.root/'saved';receipt.rename(saved);receipt.symlink_to(saved)
        with self.assertRaises(ValueError):proxy.verify(self.root,self.platform)
        receipt.unlink();receipt.write_bytes(raw);path.unlink();path.symlink_to(saved)
        with self.assertRaises(ValueError):proxy.verify(self.root,self.platform)

    def test_build_metadata_proves_real_entrypoint_target_static_build_and_exact_module(self):
        proxy.buildinfo(metadata(self.platform),self.platform)
        for changed in [metadata(self.platform).replace('go1.27.4','go1.27.0'),
            metadata(self.platform).replace('go1.27.4','go1.27.3'),
            metadata(self.platform).replace('v2.11.6','v2.11.5'),
            metadata(self.platform).replace('GOARCH=amd64','GOARCH=arm64'),
            metadata(self.platform).replace('CGO_ENABLED=0','CGO_ENABLED=1'),
            metadata(self.platform).replace('-trimpath=true','-trimpath=false'),
            metadata(self.platform).replace('GOOS=linux','GOOS=darwin'),
            metadata(self.platform)+'\t=>\t/other/module\t(devel)\n',
            metadata(self.platform)+'\tbuild\tGOARCH=arm64\n',
            metadata(self.platform)+'\tpath\tother/entrypoint\n']:
            with self.assertRaises(ValueError):proxy.buildinfo(changed,self.platform)

    def test_compiler_selectors_and_loader_injection_are_removed(self):
        values=proxy.environment({key:'unreviewed' for key in ['GOENV','GOROOT','GOTOOLDIR','GOWORK',
            'GOFLAGS','GOTOOLCHAIN','CGO_ENABLED','GOCACHEPROG','GOOS','GOARCH','GOEXPERIMENT','LD_PRELOAD','GOVULNDB','GOVULNCHECK_CONFIG']},'Linux/arm64')
        self.assertEqual(values['GOOS'],'linux');self.assertEqual(values['GOARCH'],'arm64');self.assertEqual(values['CGO_ENABLED'],'0')
        self.assertEqual(values['GOWORK'],'off');self.assertEqual(values['GOENV'],'off')
        self.assertEqual(values['GOVULNDB'],'https://vuln.go.dev');self.assertNotIn('GOVULNCHECK_CONFIG',values)
        self.assertEqual(values['GOTOOLCHAIN'],'local');self.assertEqual(values['GOFLAGS'],'-buildvcs=false')
        self.assertFalse(set(['GOROOT','GOTOOLDIR','GOCACHEPROG','GOEXPERIMENT','LD_PRELOAD'])&set(values))

    def test_wrong_compiler_or_failed_audit_preserves_both_previously_accepted_files(self):
        path,body,raw=self.accepted();calls=[]
        with patch.object(proxy,'_capture',return_value='go1.27.0\n'):
            with self.assertRaises(ValueError):proxy.build(self.root,self.platform,{},lambda *a,**k:calls.append(a))
        self.assertEqual(calls,[]);self.assertEqual(path.read_bytes(),body)
        def run(command,working,values,**options):(working/'caddy').write_bytes(elf(self.platform))
        def capture(command,**options):
            if command[-2:]==['env','GOVERSION']:return 'go1.27.4\n'
            if '-m' in command:return metadata(self.platform)
            return 'v2.11.6 h1:reviewed\n'
        with patch.object(proxy,'_capture',side_effect=capture),patch.object(proxy,'_audit',side_effect=ValueError('vulnerable linked symbols')):
            with self.assertRaisesRegex(ValueError,'vulnerable'):proxy.build(self.root,self.platform,{},run)
        self.assertEqual(path.read_bytes(),body);self.assertEqual(proxy.receipt_path(path).read_bytes(),raw)
        self.assertFalse(any(child.name.startswith('.build-') for child in path.parent.iterdir()))

    def test_cross_build_admits_architecture_and_never_executes_foreign_binary(self):
        selected='Linux/arm64' if sys.platform=='linux' and __import__('platform').machine()=='x86_64' else 'Linux/x86_64'
        calls=[]
        def run(command,working,values,**options):
            self.assertEqual(values['GOARCH'],proxy.PLATFORMS[selected][2]);(working/'caddy').write_bytes(elf(selected))
        def capture(command,**options):
            calls.append(command)
            if command[-2:]==['env','GOVERSION']:return 'go1.27.4\n'
            if '-m' in command:return metadata(selected)
            self.fail('a cross-compiled executable was run on this host')
        with patch.object(proxy,'_capture',side_effect=capture),patch.object(proxy,'_audit') as audit:
            body,receipt=proxy.build(self.root,selected,{},run)
        self.assertEqual(body,elf(selected));self.assertEqual(json.loads(receipt)['platform'],proxy.PLATFORMS[selected][0]);audit.assert_called_once()
        self.assertEqual(len(calls),2)

    def test_post_compile_module_mutation_preserves_existing_companion(self):
        path,body,raw=self.accepted()
        def run(command,working,values,**options):
            (working/'caddy').write_bytes(elf(self.platform));(working/'go.sum').write_bytes(b'unreviewed source')
        def capture(command,**options):
            if command[-2:]==['env','GOVERSION']:return 'go1.27.4\n'
            if '-m' in command:return metadata(self.platform)
            return 'v2.11.6 h1:reviewed\n'
        with patch.object(proxy,'_capture',side_effect=capture),patch.object(proxy,'_audit'):
            with self.assertRaisesRegex(ValueError,'module closure'):proxy.build(self.root,self.platform,{},run)
        self.assertEqual(path.read_bytes(),body);self.assertEqual(proxy.receipt_path(path).read_bytes(),raw)

if __name__=='__main__':unittest.main()
