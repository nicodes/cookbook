"""Separate recipe and generic module origins across real helper load lifetimes."""
from source_fixture import SDK_ROOT, TOOLS_ROOT
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


CHILD = r'''
import importlib, importlib.util, json, os
from pathlib import Path
import sys, time, types
from unittest.mock import patch
cookbook, tools, mode, *callers = sys.argv[1:]
cookbook = str(Path(cookbook).resolve())
tools = str(Path(tools).resolve())
sys.path[:] = [cookbook, *callers, *sys.path]
import dependencies
original_callers = [value for value in sys.path if value not in {cookbook, tools}]
shared = ['private_directory', 'sdk_identity', 'source_policy', 'image_import_cache']
importers = ['provider_dependency', 'mise_entrypoint', 'effective_tasks',
             'react_native', 'godot_ci', 'bootstrap', 'workflow_bootstrap']

def checked():
    assert sys.path[:2] == [cookbook, tools], sys.path
    assert sys.path[2:] == original_callers, sys.path
    assert sys.path.count(cookbook) == sys.path.count(tools) == 1

if mode in {'helpers-first', 'cookbook-first'}:
    if mode == 'cookbook-first':
        for name in shared:
            importlib.import_module(name)
    generic = {name: dependencies.helper(name) for name in
               ['source_config', 'private_directory', 'sdk_identity', 'image_import_cache', 'source_policy']}
    checked()  # Actual source_policy body prepends its directory; it cannot escape this lifetime.
    origins = {}
    for name in importers:
        importlib.import_module(name)
    for name in shared:
        module = importlib.import_module(name)
        assert Path(module.__file__).resolve() == Path(cookbook, name + '.py')
        assert Path(generic[name].__file__).resolve() == Path(tools, name + '.py')
        origins[name] = {'cookbook': str(Path(module.__file__).resolve()),
                         'tools': str(Path(generic[name].__file__).resolve())}
    import bootstrap, private_directory
    assert private_directory.SELECTION and bootstrap.SEED_LENGTH == len(bootstrap.SEED.encode())
    # Exercise real generic APIs and their own-file/lazy siblings, without setup or native work.
    policy = generic['source_policy']
    directory, executable, identity = policy.inputs(Path(cookbook).parent)
    assert len(identity['requirements']) == len(identity['checker']) == 64
    assert generic['image_import_cache'].remaining(time.monotonic() + 10) > 0
    project = dependencies.helper('godot-project')
    assert Path(project.godot_import.__file__).resolve() == Path(tools, 'godot_import.py')
    security = dependencies.helper('image_security_report')
    try: security.image_scan_findings({}, {}, context={})
    except ValueError as error: assert 'closed completion and identity context' in str(error)
    else: raise AssertionError('incomplete scan evidence was accepted')
    assert Path(importlib.import_module('image_scan_context').__file__).resolve() == Path(tools, 'image_scan_context.py')
    checked()
    print(json.dumps({'mode': mode, 'origins': origins, 'importers': importers,
                      'generic_source_policy_inputs_and_Godot_sibling': True,
                      'caller_paths_preserved': True, 'SDK_install_or_product_execution': False}))
elif mode == 'failed-import':
    original = importlib.util.spec_from_file_location
    class FailingLoader:
        def __init__(self, loader): self.loader = loader
        def create_module(self, spec): return self.loader.create_module(spec)
        def exec_module(self, module):
            assert sys.path[:2] == [tools, cookbook]
            self.loader.exec_module(module)  # Actual lower source_policy, including its insert0.
            sys.path.insert(0, 'foreign module-body path')
            raise RuntimeError('controlled failure after actual generic import')
    def failing(name, path, *args, **kwargs):
        spec = original(name, path, *args, **kwargs)
        if name == 'cookbook_tools_source_policy': spec.loader = FailingLoader(spec.loader)
        return spec
    with patch.object(importlib.util, 'spec_from_file_location', side_effect=failing):
        try: dependencies.helper('source_policy')
        except RuntimeError as error: assert 'controlled failure' in str(error)
        else: raise AssertionError('failed generic module was admitted')
    assert 'cookbook_tools_source_policy' not in sys.modules
    assert 'cookbook_tools_source_policy' not in dependencies._LOADED_HELPERS
    checked()
    print(json.dumps({'failed_import_cache_removed': True, 'caller_paths_preserved': True,
                      'SDK_priority_restored': True}))
elif mode == 'foreign-cache':
    real = dependencies.helper('source_config')
    checked()
    sys.path[:] = [tools, cookbook, tools, *original_callers]
    assert dependencies.helper('source_config') is real
    checked()  # A legitimate cached helper restores the same priority without reloading it.
    before = list(sys.path)
    sys.modules['cookbook_tools_source_config'] = types.SimpleNamespace(__file__=str(Path(callers[0], 'foreign.py')))
    try: dependencies.helper('source_config')
    except ValueError as error: assert 'origin differs' in str(error)
    else: raise AssertionError('foreign cached helper was accepted')
    assert sys.path == before
    assert dependencies._LOADED_HELPERS['cookbook_tools_source_config'][1] is real
    print(json.dumps({'foreign_cached_module_refused': True, 'no_cache_adoption_or_path_change': True}))
else:
    raise AssertionError('unknown finite source control')
'''


class HelperModuleOrigins(unittest.TestCase):
    def run_case(self, mode):
        with tempfile.TemporaryDirectory(prefix='Caller paths with spaces ') as directory:
            caller = Path(directory)
            first, second = caller / 'first', caller / 'second'
            first.mkdir(); second.mkdir()
            environment = {**os.environ, 'TOOLS_ROOT': str(TOOLS_ROOT),
                           'PYTHONDONTWRITEBYTECODE': '1'}
            result = subprocess.run([sys.executable, '-I', '-B', '-c', CHILD,
                str(SDK_ROOT / 'cookbook'), str(TOOLS_ROOT / 'helpers'), mode, str(first), str(second)],
                cwd=caller, env=environment, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            value = json.loads(result.stdout)
            self.assertEqual(list(first.iterdir()), [])
            self.assertEqual(list(second.iterdir()), [])
            return value

    def test_actual_generic_helpers_first_preserve_all_four_Cookbook_origins(self):
        value = self.run_case('helpers-first')
        self.assertEqual(len(value['origins']), 4)
        self.assertEqual(len(value['importers']), 7)

    def test_actual_Cookbook_modules_first_preserve_both_origins_and_lazy_generic_siblings(self):
        self.assertTrue(self.run_case('cookbook-first')['generic_source_policy_inputs_and_Godot_sibling'])

    def test_actual_generic_import_failure_restores_source_priority_paths_and_cache(self):
        self.assertTrue(self.run_case('failed-import')['failed_import_cache_removed'])

    def test_foreign_cached_generic_helper_still_refuses_without_replacement(self):
        self.assertTrue(self.run_case('foreign-cache')['foreign_cached_module_refused'])


if __name__ == '__main__':
    unittest.main()
