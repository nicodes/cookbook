"""Actual Mise metadata must retain the canonical root task authority."""
from source_fixture import SDK_ROOT
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import effective_tasks
import mise_entrypoint
import sdk_environment


class EffectiveTaskAuthority(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='effective Mise controls ')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'root caller'
        self.root.mkdir()
        self.global_config = self.base / 'global.toml'
        self.system_config = self.base / 'system.toml'
        self.global_config.write_text('')
        self.system_config.write_text('')
        self.source = mise_entrypoint.render('[tools]\npython="3.13.11"\n', 'c' * 40,
            'godot-addon', b'version=1\nrecipe="godot-addon"\n', ['lint', 'unit', 'security'], [])
        (self.root / 'mise.toml').write_text(self.source)
        self.before = hashlib.sha256((self.root / 'mise.toml').read_bytes()).hexdigest()
        self.environment = sdk_environment.preserve_mise()
        for key in ['MISE_CONFIG_FILE', 'MISE_ENV', 'MISE_OVERRIDE_CONFIG_FILENAMES']:
            self.environment.pop(key, None)
        self.environment.update(MISE_CONFIG_DIR=str(self.base / 'config'),
            MISE_GLOBAL_CONFIG_FILE=str(self.global_config), MISE_SYSTEM_CONFIG_FILE=str(self.system_config),
            MISE_CACHE_DIR=str(self.base / 'cache'), MISE_STATE_DIR=str(self.base / 'state'),
            MISE_TRUSTED_CONFIG_PATHS=os.pathsep.join([str(self.root),str(self.base)]),
            MISE_CEILING_PATHS=str(self.base), MISE_OFFLINE='true')

    def unchanged(self):
        self.assertEqual(hashlib.sha256((self.root / 'mise.toml').read_bytes()).hexdigest(), self.before)
        self.assertFalse((self.root / '.artifacts').exists())
        self.assertFalse((self.root / 'unexpected').exists())

    def test_actual_canonical_metadata_is_admitted_without_running_a_task(self):
        records = effective_tasks.check(self.root, self.environment)
        self.assertEqual({value['name'] for value in records},
            {'_sdk','help','setup','fmt','build','test','test:lint','test:unit','test:security','clean'})
        self.unchanged()

    def test_local_task_override_refuses_even_if_root_canonical_bytes_remain_exact(self):
        (self.root / 'mise.local.toml').write_text('[tasks.build]\nrun="touch unexpected"\n')
        with self.assertRaises(ValueError):
            effective_tasks.check(self.root, self.environment)
        self.unchanged()

    def test_automatic_local_task_and_parent_task_sources_cannot_extend_recipe(self):
        for path in [self.root / 'mise.local.toml', self.base / 'mise.toml']:
            path.write_text('[tasks.extra]\nrun="touch unexpected"\n')
            environment = {**self.environment, 'MISE_CEILING_PATHS': str(self.base.parent)}
            with self.subTest(path=path.name), self.assertRaises(ValueError):
                effective_tasks.check(self.root, environment)
            path.unlink()
        self.unchanged()

    def test_process_global_config_redirection_cannot_replace_root_task_authority(self):
        self.global_config.write_text(self.source)
        with self.assertRaises(ValueError):
            effective_tasks.check(self.root, {**self.environment, 'MISE_CONFIG_FILE': str(self.global_config),
                'MISE_OVERRIDE_CONFIG_FILENAMES': 'unowned.toml'})
        self.unchanged()

    def test_unrelated_global_tasks_and_inherited_auth_context_are_preserved(self):
        self.global_config.write_text('[tasks.personal]\nrun="touch unexpected"\n')
        before = self.global_config.read_bytes()
        environment = {**self.environment, 'GH_TOKEN':'finite-native-control', 'MISE_GITHUB_TOKEN':'finite-auth-control'}
        effective_tasks.check(self.root, environment)
        self.assertEqual(environment['GH_TOKEN'], 'finite-native-control')
        self.assertEqual(environment['MISE_GITHUB_TOKEN'], 'finite-auth-control')
        self.assertEqual(self.global_config.read_bytes(), before)
        self.unchanged()

    def test_authored_engine_and_derived_locator_overrides_refuse_in_pure_source(self):
        for key in ['MISE_CONFIG_FILE','MISE_TASK_RUN_AUTO_INSTALL','MISE_ENV','COOKBOOK_CALLER_ROOT']:
            prefix = '[tools]\npython="3.13.11"\n[env]\n' + key + '="unowned"\n'
            with self.subTest(key=key), self.assertRaises(ValueError):
                mise_entrypoint.render(prefix, 'c' * 40, 'godot-addon', b'', ['lint','unit','security'], [])
        self.unchanged()


if __name__ == '__main__':
    unittest.main()
