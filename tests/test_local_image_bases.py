from source_fixture import TOOLS_ROOT
from source_fixture import SDK_ROOT
"""Native image variants consume only already-built, exact source local bases."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS=SDK_ROOT
sys.path.insert(0,str(TOOLS/'cookbook'))
import images
spec=importlib.util.spec_from_file_location('native_local_image_builder',TOOLS_ROOT/'helpers/docker-build.py')
builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)


class LocalImageBases(unittest.TestCase):
    revision='a'*40
    image='ghcr.io/fixture/application-gate:'+revision

    def fixture(self,root,cache=False):
        for name in ['gate','variant']:(root/(name+'.Dockerfile')).write_text('FROM scratch\n')
        config={'components':{'images':[
            {'name':'gate','dockerfile':'gate.Dockerfile'},
            {'name':'variant','dockerfile':'variant.Dockerfile','pull':False,
             'build_arguments':{'BASE_IMAGE':'ghcr.io/fixture/application-gate:{revision}'}}]}}
        if cache:config['components']['images'][1]['cache_key']='variant'
        definition={'image_base':'ghcr.io/fixture/application','inputs':{'images':['ghcr.io/fixture/application-gate:{revision}']}}
        return config,definition

    def test_local_variant_inspects_after_its_base_build_and_uses_embedded_driver(self):
        for cached in [False,True]:
            with self.subTest(cached=cached),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);config,definition=self.fixture(root,cached);events=[]
                def run(command,*_args):events.append(('build',command))
                def inspect(command,**_options):
                    self.assertEqual(len(events),1);self.assertEqual(command[-1],self.image)
                    events.append(('inspect',command));return 'sha256:'+'b'*64
                with patch.object(images,'definition',return_value=definition),patch.object(images.subprocess,'check_output',side_effect=inspect):
                    images.build(root,config,{'APP_VERSION':self.revision},run)
                self.assertEqual([event[0] for event in events],['build','inspect','build'])
                self.assertNotIn('--builder',events[0][1])
                if cached:self.assertIn('--local-base',events[2][1]);self.assertIn(self.image,events[2][1])
                else:self.assertEqual(events[2][1][:6],['docker','buildx','build','--builder','default','--load'])

    def test_future_or_unknown_source_base_and_unloaded_image_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);config,definition=self.fixture(root);calls=[]
            with patch.object(images,'definition',return_value=definition):
                with self.assertRaisesRegex(ValueError,'earlier'):
                    images.build(root,{'components':{'images':list(reversed(config['components']['images']))}}, {'APP_VERSION':self.revision},lambda *_:calls.append('build'))
                self.assertEqual(calls,[])
                config['components']['images'][1]['build_arguments']['BASE_IMAGE']='ghcr.io/fixture/application-unknown:{revision}'
                with self.assertRaisesRegex(ValueError,'earlier'):
                    images.build(root,config,{'APP_VERSION':self.revision},lambda *_:calls.append('build'))
                self.assertEqual(calls,[])
                config['components']['images'][1]['build_arguments']['BASE_IMAGE']='ghcr.io/fixture/application-gate:{revision}'
                with patch.object(images.subprocess,'check_output',return_value='not-an-image'),self.assertRaisesRegex(ValueError,'immutable'):
                    images.build(root,config,{'APP_VERSION':self.revision},lambda *_:calls.append('build'))
                self.assertEqual(calls,['build'])

    def test_pull_policy_does_not_silently_select_local_driver(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);config,definition=self.fixture(root);config['components']['images'][1]['pull']=True;calls=[]
            with patch.object(images,'definition',return_value=definition),patch.object(images.subprocess,'check_output') as inspect:
                images.build(root,config,{'APP_VERSION':self.revision},lambda command,*_:calls.append(command))
            inspect.assert_not_called();self.assertNotIn('--builder',calls[1]);self.assertIn('--pull',calls[1])

    def test_cached_adapter_local_base_never_uses_isolated_cache_driver(self):
        args=['docker-build.py','--cache-key','variant','--local-base',self.image,'--','--build-arg','BASE_IMAGE='+self.image,'.']
        with tempfile.TemporaryDirectory() as temporary,patch.dict(os.environ,{'FLEET_BUILDKIT_CACHE':temporary}),patch.object(sys,'argv',args),patch.object(builder.subprocess,'check_output',return_value='sha256:'+'b'*64),patch.object(builder.subprocess,'run') as run:
            builder.main()
            self.assertEqual(run.call_args.args[0][:6],['docker','buildx','build','--builder','default','--load'])
            self.assertNotIn('--cache-to',run.call_args.args[0]);self.assertEqual(list(Path(temporary).iterdir()),[])

    def test_adapter_refuses_mutable_or_replaced_base_and_driver_override_before_build(self):
        for options in [ ['--local-base','ghcr.io/fixture/application-gate:latest','--','--build-arg','BASE_IMAGE='+self.image,'.'],
            ['--local-base',self.image,'--','--build-arg','BASE_IMAGE=other:latest','.'],
            ['--local-base',self.image,'--','--build-arg','BASE_IMAGE='+self.image,'--pull','.'],
            ['--local-base',self.image,'--','--build-arg','BASE_IMAGE='+self.image,'--builder','untrusted','.'],
            ['--local-base',self.image,'--','--build-arg','BASE_IMAGE='+self.image,'--build-arg=BASE_IMAGE=other:latest','.'] ]:
            with self.subTest(options=options),patch.object(sys,'argv',['docker-build.py','--cache-key','variant',*options]),patch.object(builder.subprocess,'run') as run,patch.object(builder.subprocess,'check_output') as inspect,contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                builder.main()
            run.assert_not_called();inspect.assert_not_called()

    def test_ordinary_cached_build_keeps_its_existing_driver_and_cache(self):
        with tempfile.TemporaryDirectory() as temporary,patch.dict(os.environ,{'FLEET_BUILDKIT_CACHE':temporary}),patch.object(sys,'argv',['docker-build.py','--cache-key','ordinary','--','.']),patch.object(builder.subprocess,'run') as run:
            builder.main();command=run.call_args.args[0]
            self.assertEqual(command[:4],['docker','buildx','build','--load']);self.assertIn('--cache-to',command);self.assertNotIn('--builder',command)


if __name__=='__main__':unittest.main()
