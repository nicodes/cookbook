from source_fixture import SDK_ROOT
from pathlib import Path
import sys
import unittest
import os
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import sdk_environment
import godot_addon
import godot_game


class SDKDataHomes(unittest.TestCase):
    def test_existing_mise_store_survives_application_xdg_isolation(self):
        original = {'HOME': '/runner', 'XDG_DATA_HOME': '/cache/data', 'MISE_DATA_DIR': '/reviewed/sdk-store'}
        values = sdk_environment.preserve_mise(original, system='Linux')
        values['XDG_DATA_HOME'] = '/checkout/.artifacts/godot-data'
        self.assertEqual(values['MISE_DATA_DIR'], '/reviewed/sdk-store')
        self.assertEqual(original['XDG_DATA_HOME'], '/cache/data')

    def test_original_xdg_default_is_captured_once_without_changing_godot_isolation(self):
        values = sdk_environment.preserve_mise({'HOME': '/runner', 'XDG_DATA_HOME': '/cache/data'}, system='Linux')
        self.assertEqual(values['MISE_DATA_DIR'], '/cache/data/mise')
        values['XDG_DATA_HOME'] = '/checkout/.artifacts/godot-data'
        self.assertEqual(sdk_environment.preserve_mise(values)['MISE_DATA_DIR'], '/cache/data/mise')
        self.assertEqual(values['XDG_DATA_HOME'], '/checkout/.artifacts/godot-data')

    def test_both_godot_recipe_drivers_preserve_store_before_setting_their_data_home(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'HOME': '/runner', 'XDG_DATA_HOME': '/cache/data'}, clear=True):
            root = Path(directory)
            addon = godot_addon.environment(root)
            with patch.object(godot_game.subprocess, 'check_output', return_value='a' * 40):
                game = godot_game.environment(root, {})
            for values in [addon, game]:
                self.assertEqual(values['MISE_DATA_DIR'], '/cache/data/mise')
                self.assertEqual(values['XDG_DATA_HOME'], str(root / '.artifacts/godot-data'))

    def test_unix_and_windows_defaults_match_mise_not_application_user_data(self):
        self.assertEqual(sdk_environment.preserve_mise({'HOME': '/runner'}, system='Darwin')['MISE_DATA_DIR'], '/runner/.local/share/mise')
        self.assertEqual(sdk_environment.preserve_mise({'LOCALAPPDATA': r'C:\Users\runner\AppData\Local', 'USERPROFILE': r'C:\Users\runner'}, system='Windows')['MISE_DATA_DIR'], r'C:\Users\runner\AppData\Local\mise')
        self.assertEqual(sdk_environment.preserve_mise({'USERPROFILE': r'C:\Users\runner'}, system='Windows')['MISE_DATA_DIR'], r'C:\Users\runner\AppData\Local\mise')


if __name__ == '__main__':
    unittest.main()
