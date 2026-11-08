"""Serialize real generic-helper imports without changing origin or cleanup guards."""
from source_fixture import SDK_ROOT, TOOLS_ROOT
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

CHILD = r'''
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import importlib.util,json,sys,threading,types
from pathlib import Path
from unittest.mock import patch
sdk,tools,mode=sys.argv[1:]
sys.path.insert(0,str(Path(sdk)/'cookbook'))
import dependencies
entered,release,second_started=threading.Event(),threading.Event(),threading.Event()
original=importlib.util.spec_from_file_location
loads=[];partial=[];nested=[]
class Loader:
 def __init__(self,loader,name):self.loader,self.name=loader,name
 def create_module(self,spec):return self.loader.create_module(spec)
 def exec_module(self,module):
  loads.append(self.name)
  if self.name=='cookbook_tools_godot_suite' and len(partial)==0:
   partial.append(module);entered.set()
   if mode=='reentrant':nested.append(dependencies.helper('source_config'))
   elif mode=='same-thread-partial':
    try:dependencies.helper('godot-suite')
    except ValueError as error:assert 'origin differs' in str(error)
    else:raise AssertionError('partial same-thread module accepted')
   else:assert release.wait(10)
   if mode=='failure':raise RuntimeError('controlled first import failure')
  self.loader.exec_module(module)
def controlled(name,path,*args,**kwargs):
 spec=original(name,path,*args,**kwargs)
 if name in {'cookbook_tools_godot_suite','cookbook_tools_source_config'}:spec.loader=Loader(spec.loader,name)
 return spec
def second(name):
 second_started.set();return dependencies.helper(name)
with patch.object(importlib.util,'spec_from_file_location',side_effect=controlled):
 if mode in {'same','different','failure'}:
  with ThreadPoolExecutor(max_workers=2) as pool:
   first=pool.submit(dependencies.helper,'godot-suite');assert entered.wait(10)
   later=pool.submit(second,'source_config' if mode=='different' else 'godot-suite');assert second_started.wait(10)
   try:later.result(timeout=0.1)
   except TimeoutError:pass
   else:raise AssertionError('second resolver escaped complete import serialization')
   assert loads==['cookbook_tools_godot_suite']
   release.set()
   if mode=='failure':
    try:first.result(timeout=10)
    except RuntimeError as error:assert 'controlled first import failure' in str(error)
    else:raise AssertionError('first import failure was hidden')
    module=later.result(timeout=10)
    assert module is not partial[0] and sys.modules['cookbook_tools_godot_suite'] is module
    assert dependencies._LOADED_HELPERS['cookbook_tools_godot_suite'][1] is module
   else:
    module=first.result(timeout=10);other=later.result(timeout=10)
    assert (module is other) is (mode=='same')
 elif mode in {'reentrant','same-thread-partial'}:
  module=dependencies.helper('godot-suite')
  assert dependencies._LOADED_HELPERS['cookbook_tools_godot_suite'][1] is module
  if mode=='reentrant':assert nested[0] is dependencies.helper('source_config')
 elif mode=='foreign':
  release.set();module=dependencies.helper('godot-suite');before=list(sys.path)
  impostor=types.SimpleNamespace(__file__=module.__file__)
  sys.modules['cookbook_tools_godot_suite']=impostor
  try:dependencies.helper('godot-suite')
  except ValueError as error:assert 'origin differs' in str(error)
  else:raise AssertionError('foreign same-path module accepted')
  assert sys.path==before and sys.modules['cookbook_tools_godot_suite'] is impostor
  assert dependencies._LOADED_HELPERS['cookbook_tools_godot_suite'][1] is module
 else:raise AssertionError('unknown finite concurrency mode')
if mode!='foreign':
 assert sys.path[:2]==[str(Path(sdk)/'cookbook'),str(Path(tools)/'helpers')]
 assert Path(module.__file__).resolve()==Path(tools)/'helpers/godot-suite.py'
print(json.dumps({'mode':mode,'loads':loads,'metadata_import_only':True,'Godot_or_product_execution':False}))
'''


class HelperImportConcurrency(unittest.TestCase):
    def run_case(self, mode):
        result = subprocess.run([sys.executable, '-I', '-B', '-c', CHILD, str(SDK_ROOT), str(TOOLS_ROOT), mode],
            env={**os.environ, 'TOOLS_ROOT': str(TOOLS_ROOT), 'PYTHONDONTWRITEBYTECODE': '1'},
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_same_helper_first_import_waits_and_reuses_one_verified_module(self):
        self.assertEqual(self.run_case('same')['loads'], ['cookbook_tools_godot_suite'])

    def test_different_helper_first_imports_serialize_shared_path_lifetime(self):
        self.assertEqual(self.run_case('different')['loads'], ['cookbook_tools_godot_suite', 'cookbook_tools_source_config'])

    def test_nested_different_helper_load_is_reentrant(self):
        self.assertEqual(self.run_case('reentrant')['loads'], ['cookbook_tools_godot_suite', 'cookbook_tools_source_config'])

    def test_recursive_same_helper_still_refuses_partial_module(self):
        self.assertTrue(self.run_case('same-thread-partial')['metadata_import_only'])

    def test_failed_import_cleans_partial_cache_before_waiting_resolver_retries(self):
        self.assertEqual(self.run_case('failure')['loads'], ['cookbook_tools_godot_suite', 'cookbook_tools_godot_suite'])

    def test_foreign_same_path_cached_module_still_refuses_without_rebinding(self):
        self.assertTrue(self.run_case('foreign')['metadata_import_only'])


if __name__ == '__main__':
    unittest.main()
