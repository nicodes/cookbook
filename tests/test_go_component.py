from source_fixture import SDK_ROOT
"""Owned Go compiler setup is explicit and all product operations are offline."""
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch
import zipfile
sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import go_component as go

class GoComponentControls(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        directory=self.root/'deploy/native';directory.mkdir(parents=True)
        for name in ['go.mod','go.sum']:(directory/name).touch()
        (self.root/'mise.toml').write_text('[tools]\ngo="1.27.3"\n')
        self.config={'components':{'go':[{'directory':'deploy/native','package':'example.invalid/native/cmd/server',
            'output':'.artifacts/native/server','test_packages':['example.invalid/native/security/...']}]}}
        self.calls=[]
    def execute(self,argv,cwd,env):
        self.calls.append((argv,cwd,env))
        if 'build' in argv:
            path=Path(argv[argv.index('-o')+1]);path.write_bytes(b'\x7fELF native fixture');path.chmod(0o755)
    def test_only_setup_resolves_dependencies_build_and_tests_keep_the_reviewed_scope_offline(self):
        with patch.object(go.subprocess,'check_output',return_value='go1.27.3\n'):
            go.setup(self.root,self.config,{},self.execute);go.build(self.root,self.config,{},self.execute);go.test(self.root,self.config,{},self.execute)
        self.assertEqual(self.calls[0][0],['go','mod','download'])
        self.assertNotIn('GOPROXY',self.calls[0][2])
        self.assertEqual(self.calls[1][0],['go','list','-mod=readonly','-deps','-test','example.invalid/native/security/...'])
        self.assertNotIn('GOPROXY',self.calls[1][2])
        self.assertEqual(self.calls[3][0],['go','test','-mod=readonly','example.invalid/native/security/...'])
        for _,cwd,env in self.calls[2:]:
            self.assertEqual(cwd,self.root/'deploy/native');self.assertEqual(env['GOPROXY'],'off');self.assertEqual(env['GOTOOLCHAIN'],'local')
            self.assertEqual(env['GOSUMDB'],'off')
        self.assertEqual(go.artifact_paths(self.root,self.config),['.artifacts/native/server'])
    def test_setup_does_not_select_undeclared_tests_or_build_the_shipping_package(self):
        self.config['components']['go'][0].pop('test_packages')
        with patch.object(go.subprocess,'check_output',return_value='go1.27.3\n'):
            go.setup(self.root,self.config,{},self.execute)
        self.assertEqual([call[0] for call in self.calls],[['go','mod','download']])
    def test_setup_preserves_multiple_declared_test_selectors_without_widening_them(self):
        selectors=['example.invalid/library/...','example.invalid/another/security']
        self.config['components']['go'][0]['test_packages']=selectors
        with patch.object(go.subprocess,'check_output',return_value='go1.27.3\n'):
            go.setup(self.root,self.config,{},self.execute)
        self.assertEqual(self.calls[1][0],['go','list','-mod=readonly','-deps','-test',*selectors])
        self.assertEqual(self.calls[0][2]['GOFLAGS'],'-mod=readonly')
    def test_wrong_sdk_never_runs_the_compiler_or_repairs_its_pin(self):
        with patch.object(go.subprocess,'check_output',return_value='go9.0.0\n'),self.assertRaisesRegex(ValueError,'pinned Go'):
            go.build(self.root,self.config,{},self.execute)
        self.assertEqual(self.calls,[])
    def test_source_or_environment_authority_cannot_be_replaced(self):
        record=self.config['components']['go'][0]
        for changes in [{'environment':{'GOTOOLCHAIN':'auto'}},{'environment':{'CGO_ENABLED':'1'}},
            {'package':'example.invalid/native;curl'},{'output':'../outside'}, {'test_packages':['native;sh']}]:
            self.config['components']['go']=[record|changes]
            with self.subTest(changes=changes),self.assertRaises(ValueError):go.declaration(self.root,self.config)


@unittest.skipUnless(sys.platform=='linux' and platform.machine().lower() in {'x86_64','amd64'},
                     'the native Go component targets Linux amd64')
class FrozenGoClosureControls(unittest.TestCase):
    """A file proxy supplies a synthetic external package's test-only module."""
    def setUp(self):
        executable=shutil.which('go')
        if not executable:self.skipTest('the pinned Go SDK is required for native closure controls')
        tools=tomllib.loads((SDK_ROOT/'mise.toml').read_text())['tools']
        self.pin=tools['go']
        actual=subprocess.check_output([executable,'env','GOVERSION'],env=os.environ|{'GOTOOLCHAIN':'local'},text=True,timeout=30).strip()
        if actual!='go'+self.pin:self.skipTest('the exact repository-pinned Go SDK is required')
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        self.module=self.root/'native';self.module.mkdir()
        (self.root/'mise.toml').write_text('[tools]\ngo='+json.dumps(self.pin)+'\n')
        self.proxy=self.root/'proxy';self.proxy.mkdir()
        self.dependency='example.invalid/test-only';self.version='v1.2.3'
        dependency_files={'go.mod':b'module example.invalid/test-only\n\ngo 1.12\n',
                          'equal.go':b'package helper\nfunc Equal(a, b int) bool { return a == b }\n'}
        prefix=self.dependency+'@'+self.version+'/'
        self.archive_files={prefix+name:content for name,content in dependency_files.items()}
        versions=self.proxy/self.dependency/'@v';versions.mkdir(parents=True)
        self.archive=versions/(self.version+'.zip');self.write_archive()
        (versions/(self.version+'.mod')).write_bytes(dependency_files['go.mod'])
        (versions/(self.version+'.info')).write_text(json.dumps({'Version':self.version,'Time':'2020-01-01T00:00:00Z'}))
        (versions/'list').write_text(self.version+'\n')
        library=self.module/'library';library.mkdir()
        (library/'go.mod').write_text('module example.invalid/library\n\ngo 1.12\n\nrequire '+self.dependency+' '+self.version+'\n')
        (library/'library.go').write_text('package library\nfunc Value() int { return 7 }\n')
        (library/'library_test.go').write_text('package library_test\nimport ("testing"; "example.invalid/library"; helper "example.invalid/test-only")\nfunc TestValue(t *testing.T) { if !helper.Equal(library.Value(), 7) { t.Fatal("native assertion") } }\n')
        (self.module/'go.mod').write_text('module example.invalid/owner\n\ngo '+self.pin+'\n\nrequire example.invalid/library v1.0.0\nreplace example.invalid/library => ./library\n')
        sums=[self.dependency+' '+self.version+' '+self.hash_files(self.archive_files),
              self.dependency+' '+self.version+'/go.mod '+self.hash_files({'go.mod':dependency_files['go.mod']})]
        (self.module/'go.sum').write_text('\n'.join(sums)+'\n')
        # The shipping program is deliberately unbuildable: setup may only load
        # the declared library metadata, and the unit stage may only test it.
        (self.module/'main.go').write_text('package main\nfunc main() { undefinedShippingSymbol() }\n')
        self.config={'components':{'go':[{'directory':'native','package':'example.invalid/owner/cmd/server',
            'output':'.artifacts/server','test_packages':['example.invalid/library/...']}]}}
        self.values=os.environ|{'GOENV':'off','GOWORK':'off','GOTOOLCHAIN':'local','GOSUMDB':'off',
            'GOPROXY':self.proxy.as_uri(),'GONOPROXY':'none','GONOSUMDB':'*','GOPRIVATE':'',
            'GOMODCACHE':str(self.root/'module-cache'),'GOCACHE':str(self.root/'build-cache'),'GOPATH':str(self.root/'gopath')}
        self.calls=[]
        self.frozen={path:path.read_bytes() for path in self.module.rglob('*') if path.is_file()}

    @staticmethod
    def hash_files(files):
        listing=''.join(hashlib.sha256(files[name]).hexdigest()+'  '+name+'\n' for name in sorted(files))
        return 'h1:'+base64.b64encode(hashlib.sha256(listing.encode()).digest()).decode()

    def write_archive(self):
        with zipfile.ZipFile(self.archive,'w') as archive:
            for name,content in self.archive_files.items():archive.writestr(name,content)

    def execute(self,argv,cwd,environment):
        self.calls.append((argv,cwd,environment))
        return subprocess.run(argv,cwd=cwd,env=environment,check=True,text=True,capture_output=True,timeout=120)

    def assert_frozen(self):
        self.assertEqual({path:path.read_bytes() for path in self.module.rglob('*') if path.is_file()},self.frozen)
        self.assertFalse((self.root/'.artifacts/server').exists())

    def test_setup_resolves_test_only_zip_then_original_native_unit_runs_offline(self):
        directory=self.root/'module-cache/cache/download'/self.dependency/'@v'
        prepared=go.environment(self.root,self.config['components']['go'][0],self.values,offline=False)
        self.execute(['go','mod','download'],self.module,prepared)
        self.assertFalse((directory/(self.version+'.zip')).exists())
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            go.test(self.root,self.config,self.values,self.execute)
        self.assertIn('module lookup disabled by GOPROXY=off',failure.exception.stderr)
        go.setup(self.root,self.config,self.values,self.execute)
        self.assertTrue((directory/(self.version+'.zip')).is_file())
        # Remove the only available proxy before executing the original unit
        # command: cached metadata alone cannot fake a successful native test.
        shutil.rmtree(self.proxy)
        go.test(self.root,self.config,self.values,self.execute)
        self.assertEqual(self.calls[-1][0],['go','test','-mod=readonly','example.invalid/library/...'])
        self.assertEqual(self.calls[-1][2]['GOPROXY'],'off')
        self.assertEqual(self.calls[-1][2]['GOSUMDB'],'off')
        self.assertEqual([call[0][1] for call in self.calls],['mod','test','mod','list','test'])
        self.assert_frozen()

    def test_setup_refuses_changed_dependency_bytes_instead_of_repairing_the_lock(self):
        self.archive_files[self.dependency+'@'+self.version+'/equal.go']+=b'// changed archive\n'
        self.write_archive()
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            go.setup(self.root,self.config,self.values,self.execute)
        self.assertIn('checksum mismatch',failure.exception.stderr)
        self.assert_frozen()

    def test_setup_loads_test_metadata_without_compiling_even_the_selected_library(self):
        source=self.module/'library/library.go'
        source.write_text(source.read_text()+'var NotCompilable = undefinedLibrarySymbol\n')
        self.frozen[source]=source.read_bytes()
        go.setup(self.root,self.config,self.values,self.execute)
        self.assertEqual([call[0][1] for call in self.calls],['mod','list'])
        offline=go.environment(self.root,self.config['components']['go'][0],self.values,offline=True)
        resolved=self.execute(['go','list','-mod=readonly','-deps','-test','example.invalid/library/...'],self.module,offline)
        self.assertIn('example.invalid/test-only',resolved.stdout)
        self.assert_frozen()

if __name__=='__main__':unittest.main()
