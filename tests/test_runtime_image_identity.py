"""Imported images keep provenance keys while executing only admitted config IDs."""
from source_fixture import SDK_ROOT
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import ci
import images
import image_fixtures


class RuntimeImageIdentity(unittest.TestCase):
    def test_complete_shipping_and_fixture_scope_selects_config_ids_without_tag_assignment(self):
        source='a'*40
        reference='ghcr.io/example/app-api:'+source
        fixture='ghcr.io/example/app-test:'+source
        release={'source':{'revision':source},'artifacts':{
            'api':{'kind':'image','config_digest':'sha256:'+'b'*64},
            'web':{'kind':'archive','digest':'sha256:'+'c'*64}}}
        with patch.object(images.deployment,'verify',return_value=release),\
             patch.object(images,'definition',return_value={'image_base':'ghcr.io/example/app'}),\
             patch.object(image_fixtures,'verify',return_value={fixture:'sha256:'+'d'*64}),\
             patch.object(images.native_runtime,'run') as run:
            accepted=images.runtime_identities(Path('/source'),{}, {'SOURCE_COMMIT':source})
        self.assertEqual(accepted,{reference:'sha256:'+'b'*64,fixture:'sha256:'+'d'*64})
        run.assert_not_called()
        generic=Mock();generic.resolve_images.return_value=list(accepted)
        generic.artifacts.return_value={'image:'+key:value for key,value in accepted.items()}
        scope={'images':[reference,fixture]}
        with patch.object(ci,'verification',return_value=generic),patch.object(ci,'git',return_value=source),\
             patch.object(images,'runtime_identities',return_value=accepted):
            result=ci.artifacts(Path('/source'),{},scope)
        self.assertEqual(result,{'image:'+key:value for key,value in accepted.items()})
        generic.artifacts.assert_called_once_with(scope,image_identities=accepted)

    def test_missing_or_extra_imported_component_fails_before_any_native_inspection(self):
        generic=Mock();generic.resolve_images.return_value=['ghcr.io/example/app-api:'+'a'*40]
        with patch.object(ci,'verification',return_value=generic),patch.object(ci,'git',return_value='a'*40),\
             patch.object(images,'runtime_identities',return_value={}):
            with self.assertRaisesRegex(ValueError,'complete declared build scope'):
                ci.artifacts(Path('/source'),{}, {'images':['api']})
        generic.artifacts.assert_not_called()


if __name__=='__main__':unittest.main()
