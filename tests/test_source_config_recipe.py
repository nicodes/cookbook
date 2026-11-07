"""Retain the original recipe codec/module-origin refusal control."""
from source_fixture import SDK_ROOT
import subprocess
import sys
import unittest

PATH = SDK_ROOT / 'cookbook/configuration.py'


class RecipeSourceOrigin(unittest.TestCase):
    def test_recipe_api_refuses_foreign_cached_source_and_native_compilers(self):
        program = '''import sys,types
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import configuration
module=types.ModuleType(sys.argv[2]);module.__file__='/foreign/'+sys.argv[2]+'.py'
sys.modules[sys.argv[2]]=module
try:
 if sys.argv[2]=='source_config': configuration.parse_source_data(b'value=1')
 else: configuration.runtime_configuration()
except ValueError as error: print(error)
else: raise AssertionError('foreign cached source authority was admitted')
'''
        for name in ['source_config', 'runtime_config']:
            result = subprocess.run([sys.executable, '-I', '-B', '-c', program,
                str(PATH.parents[1] / 'cookbook'), name], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('origin differs', result.stdout)
