from source_fixture import SDK_ROOT
"""A real cold checkout preserves source while excluding parent runtime secrets."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import godot_cold


class ColdControls(unittest.TestCase):
    def test_real_exact_checkout_isolated_home_spaces_and_secret_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'source';root.mkdir();(root/'owned.txt').write_text('native source')
            for argv in [['init','-q'],['add','.'],['-c','user.name=Control','-c','user.email=control@example.invalid','commit','-qm','source']]:
                subprocess.run(['git','-C',str(root),*argv],check=True)
            environment={'PATH':'/bin','PARENT_RUNTIME_SECRET':'private','CLERK_SECRET_KEY':'private','GITHUB_RUN_ID':'12'}
            with patch.object(godot_cold.shutil,'which',return_value='/reviewed/mise'):
                with godot_cold.checkout(root,root,environment) as (client,child,mise):
                    self.assertIn(' ',str(client));self.assertEqual((client/'owned.txt').read_text(),'native source')
                    self.assertNotEqual(child['HOME'],str(root));self.assertNotIn('PARENT_RUNTIME_SECRET',child);self.assertNotIn('CLERK_SECRET_KEY',child)
                    self.assertEqual(child['GITHUB_RUN_ID'],'12');self.assertEqual(child['COOKBOOK_GODOT_ENTRY'],'build')
                    self.assertEqual(child['PATH'],'/usr/bin:/bin');self.assertEqual(mise,'/reviewed/mise')
                self.assertFalse(client.exists());self.assertEqual((root/'owned.txt').read_text(),'native source')

    def test_altered_source_after_cold_setup_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'owned.txt').write_text('native source')
            for argv in [['init','-q'],['add','.'],['-c','user.name=Control','-c','user.email=control@example.invalid','commit','-qm','source']]:
                subprocess.run(['git','-C',str(root),*argv],check=True)
            with patch.object(godot_cold.shutil,'which',return_value='/reviewed/mise'),self.assertRaises(subprocess.CalledProcessError):
                with godot_cold.checkout(root,root,{'PATH':'/bin'}) as (client,child,mise):
                    (client/'owned.txt').write_text('changed')


if __name__=='__main__':unittest.main()
