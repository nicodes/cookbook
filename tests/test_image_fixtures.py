from source_fixture import SDK_ROOT
"""Nonshipping variants retain a separate exact source and image/archive closure."""
from pathlib import Path
import sys,tempfile,unittest,json
from unittest.mock import patch
sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import image_fixtures as fixture

class FixtureImageControls(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        (self.root/'.artifacts').mkdir();self.config={'components':{'fixture_images':{'directory':'.artifacts/fixture-images'},
            'images':[{'name':'gate'},{'name':'api-localdev'},{'name':'jwks-counter'}]}}
        self.definition={'image_base':'ghcr.io/fixture/app','inputs':{'images':['ghcr.io/fixture/app-gate:{revision}']}}
        self.refs={f'ghcr.io/fixture/app-{name}:'+ 'a'*40:'sha256:'+'b'*64 for name in ['api-localdev','jwks-counter']}
        self.calls=[]
    def execute(self,argv,cwd,env,**kw):
        self.calls.append(argv)
        if 'save' in argv:Path(argv[argv.index('--output')+1]).write_bytes(b'actual fixture archive bytes')
    def test_shipping_images_stay_separate_and_fixture_archive_closure_is_exact(self):
        with patch.object(fixture.images,'definition',return_value=self.definition),patch.object(fixture,'inspect',return_value=self.refs):
            fixture.package(self.root,self.config,{'SOURCE_COMMIT':'a'*40},self.execute)
            self.assertEqual(fixture.verify(self.root,self.config,{'SOURCE_COMMIT':'a'*40}),self.refs)
            fixture.load(self.root,self.config,{'SOURCE_COMMIT':'a'*40},{'image:'+key:value for key,value in self.refs.items()},self.execute)
            self.assertNotIn('ghcr.io/fixture/app-gate:'+'a'*40,self.calls[0])
            self.assertEqual(len(fixture.references(self.root,self.config)),2)
        self.assertEqual(len(self.calls),2)
    def test_missing_or_changed_source_variant_archive_and_proof_fail_before_docker_load(self):
        with patch.object(fixture.images,'definition',return_value=self.definition),patch.object(fixture,'inspect',return_value=self.refs):
            fixture.package(self.root,self.config,{'SOURCE_COMMIT':'a'*40},self.execute)
            for source,accepted in [('c'*40,{}),('a'*40,{})]:
                with self.assertRaises(ValueError):fixture.load(self.root,self.config,{'SOURCE_COMMIT':source},accepted,self.execute)
            self.assertEqual(len(self.calls),1)
            (self.root/'.artifacts/fixture-images/images.tar.gz').write_bytes(b'changed archive')
            with self.assertRaises(ValueError):fixture.verify(self.root,self.config,{'SOURCE_COMMIT':'a'*40})
    def test_extra_declaration_requires_real_bounded_nonshipping_native_builds(self):
        with patch.object(fixture.images,'definition',return_value=self.definition):
            for value in ['deploy/fixture-images','.artifacts','.artifacts/release/fixtures']:
                self.config['components']['fixture_images']['directory']=value
                with self.assertRaises(ValueError):fixture.declaration(self.root,self.config)

if __name__=='__main__':unittest.main()
