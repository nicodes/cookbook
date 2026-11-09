from source_fixture import SDK_ROOT
"""Native variants consume only earlier exact-source local bases."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import images


class LocalImageBases(unittest.TestCase):
    revision='a'*40
    image='ghcr.io/example/application-fixture:'+revision

    def fixture(self,root,cached=False):
        for name in ['fixture','variant']:(root/(name+'.Dockerfile')).write_text('FROM scratch\n')
        records=[{'name':'fixture','dockerfile':'fixture.Dockerfile'},
            {'name':'variant','dockerfile':'variant.Dockerfile','pull':False,
             'build_arguments':{'BASE_IMAGE':'ghcr.io/example/application-fixture:{revision}'}}]
        if cached:records[1]['cache_key']='variant'
        return {'components':{'images':records}}, {'image_base':'ghcr.io/example/application','inputs':{'images':[]}}

    def test_cached_and_uncached_variants_inspect_after_base_build_and_never_pull_replacement(self):
        for cached in [False,True]:
            with self.subTest(cached=cached),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);config,definition=self.fixture(root,cached);events=[]
                def inspect(references,*_):
                    self.assertEqual(references,[self.image]);self.assertEqual(len(events),1)
                    events.append(('inspect',references))
                with patch.object(images,'definition',return_value=definition),patch.object(images.native_runtime,'inspect',side_effect=inspect):
                    images.build(root,config,{'APP_VERSION':self.revision},lambda command,*_:events.append(('build',command)))
                self.assertEqual([event[0] for event in events],['build','inspect','build'])
                base,variant=events[0][1],events[-1][1]
                self.assertEqual(base[:3],['podman','--remote=false','build'])
                self.assertIn('--format',base);self.assertIn('oci',base)
                self.assertNotIn('--builder',base)
                if cached:
                    self.assertIn('--local-base',variant);self.assertIn(self.image,variant)
                    self.assertTrue(any(value.endswith('/podman-build.py') for value in variant))
                else:
                    self.assertEqual(variant[:3],['podman','--remote=false','build'])
                    self.assertIn('--pull=never',variant)

    def test_future_unknown_or_missing_base_is_refused_before_variant_effect(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);config,definition=self.fixture(root);effects=[]
            with patch.object(images,'definition',return_value=definition):
                for records in [list(reversed(config['components']['images'])),
                    [config['components']['images'][0],config['components']['images'][1]|{'build_arguments':{'BASE_IMAGE':'ghcr.io/example/application-unknown:{revision}'}}]]:
                    with self.assertRaisesRegex(ValueError,'earlier'):
                        images.build(root,{'components':{'images':records}},{'APP_VERSION':self.revision},lambda *_:effects.append('build'))
                    self.assertEqual(effects,[])
                with patch.object(images.native_runtime,'inspect',side_effect=ValueError('missing owned base')),self.assertRaisesRegex(ValueError,'missing owned base'):
                    images.build(root,config,{'APP_VERSION':self.revision},lambda *_:effects.append('build'))
                self.assertEqual(effects,['build'])

    def test_explicit_upstream_pull_does_not_claim_local_base_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);config,definition=self.fixture(root);config['components']['images'][1]['pull']=True;calls=[]
            with patch.object(images,'definition',return_value=definition),patch.object(images.native_runtime,'inspect') as inspect:
                images.build(root,config,{'APP_VERSION':self.revision},lambda command,*_:calls.append(command))
            inspect.assert_not_called();self.assertIn('--pull=always',calls[1]);self.assertNotIn('--builder',calls[1])
