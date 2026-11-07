from source_fixture import TOOLS_ROOT
from source_fixture import SDK_ROOT
from source_fixture import write_source
"""Actual pinned frozen/offline generation and closed native binding evidence."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile
import sys
from unittest.mock import patch

ROOT = SDK_ROOT
spec = importlib.util.spec_from_file_location('binding_addons', TOOLS_ROOT / 'helpers/godot-addons.py')
addons = importlib.util.module_from_spec(spec); spec.loader.exec_module(addons)
sys.path.insert(0, str(ROOT / 'cookbook'))
import godot_addon_bindings
import godot_game


@unittest.skipUnless(os.environ.get('GDAM_BIN'), 'setup must prepare the actual pinned GDAM CLI')
class NativeAddonBindings(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name); self.project = self.root / 'app'; self.project.mkdir()
        self.bindings = {'tool': 'github:aviorstudio/gdam', 'pin': {'version': 'v0.2.0',
            'asset_pattern': 'gdam_Linux_x86_64.tar.gz', 'bin': 'gdam'}, 'binary': Path(os.environ['GDAM_BIN']).resolve()}
        self.archives = {}; provenance = {}; releases = {}
        names = ['@example/leaf', '@example/consumer']
        for number, name in enumerate(names, 1):
            payload = self.root / str(number); payload.mkdir()
            (payload / 'plugin.cfg').write_text('[plugin]\nname="Fixture"\n')
            (payload / 'gdam.json').write_text(json.dumps({'addons': {}, 'package': '.'}))
            script = ('extends RefCounted\nconst ANSWER = 42\n' if number == 1 else
                'extends RefCounted\nconst Dependencies = preload(".gdam/deps.gd")\nconst ANSWER = Dependencies.Leaf_value.ANSWER\n')
            (payload / 'value.gd').write_text(script)
            (payload / 'value.gd.uid').write_text('uid://b' + str(number) * 8 + '\n')
            archive = self.root / (str(number) + '.zip')
            with zipfile.ZipFile(archive, 'w') as package:
                for path in payload.iterdir(): package.write(path, path.name)
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.archives[number] = archive
            dependencies = {'@example/leaf': 'v0.0.1'} if number == 2 else {}
            required = ['gdam.json', '@example/consumer@v0.0.1'] if number == 1 else ['gdam.json']
            provenance[name] = {'tag': 'v0.0.1', 'asset_id': number, 'asset_sha256': digest,
                'tree_sha256': addons.tree_hash(payload), 'commit': str(number) * 40}
            releases[name + '@v0.0.1'] = {'name': name, 'tag': 'v0.0.1', 'github_owner': 'example',
                'github_repo': name.split('/')[1], 'github_release_id': number, 'commit_sha': str(number) * 40,
                'asset_id': number, 'asset_name': archive.name, 'asset_digest': 'sha256:' + digest,
                'published_at': '2026-01-01T00:00:00Z', 'prerelease': False, 'editor_plugin': False,
                'dependencies': dependencies, 'installs': [{'path': 'addons/' + name.replace('/', '_'), 'required_by': required}]}
        requirements = {name: 'v0.0.1' for name in names}
        write_source(self.project / 'gdam.toml', {'addons': {name: {'tag': tag} for name, tag in requirements.items()}})
        (self.project / 'gdam.lock').write_text(json.dumps({'version': 1, 'requirements': requirements, 'addons': releases}))
        (self.project / 'addon-provenance.json').write_text(json.dumps(provenance))
        subprocess.run(['git', 'init', '--quiet', str(self.root)], check=True)
        (self.root / '.gitignore').write_text('.artifacts/\n/app/gdam.json\n')
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)
        godot_game.runtime_configuration().prepare(self.root, 'app/gdam.toml', 'gdam')
        self.fetches = []

    def fetch(self, repository, asset, target):
        self.fetches.append(asset); shutil.copyfile(self.archives[asset], target)

    def prepare(self):
        addons.restore(self.project, self.fetch, bindings=self.bindings)

    def test_real_offline_generator_seals_transitive_bridge_without_altering_release_bytes(self):
        self.prepare()
        bridge = self.project / 'addons/@example_consumer/.gdam/deps.gd'
        self.assertIn('const Leaf_value = preload("res://addons/@example_leaf/value.gd")', bridge.read_text())
        addons.verify(self.project, self.bindings)
        with self.assertRaises(ValueError): addons.verify(self.project)
        self.fetches.clear(); self.prepare(); self.assertEqual(self.fetches, [])
        if os.environ.get('GODOT_BIN'):
            (self.project / 'project.godot').write_text('[application]\nconfig/name="Bindings fixture"\n')
            (self.project / 'verify.gd').write_text('extends SceneTree\nfunc _initialize():\n\tassert(preload("res://addons/@example_consumer/value.gd").ANSWER == 42)\n\tprint("NATIVE_BINDINGS_OK")\n\tquit()\n')
            imported = subprocess.run([os.environ['GODOT_BIN'], '--headless', '--editor', '--import', '--quit',
                '--path', str(self.project)], timeout=30, capture_output=True, text=True, check=True)
            self.assertNotIn('SCRIPT ERROR:', imported.stdout + imported.stderr)
            addons.verify(self.project, self.bindings)
            result = subprocess.run([os.environ['GODOT_BIN'], '--headless', '--path', str(self.project),
                '--script', 'res://verify.gd'], timeout=30, capture_output=True, text=True, check=True)
            self.assertIn('NATIVE_BINDINGS_OK', result.stdout)
            addons.verify(self.project, self.bindings)

    def test_missing_changed_foreign_or_linked_bridge_refuses_without_repair(self):
        self.prepare(); bridge = self.project / 'addons/@example_consumer/.gdam/deps.gd'; body = bridge.read_bytes()
        for mutation in ['missing', 'changed', 'foreign', 'link']:
            with self.subTest(mutation=mutation):
                if mutation == 'missing': bridge.unlink()
                elif mutation == 'changed': bridge.write_bytes(body + b'\n# changed')
                elif mutation == 'foreign': (bridge.parent / 'unrelated.gd').write_text('foreign')
                else: bridge.unlink(); bridge.symlink_to(self.root / '1/value.gd')
                with self.assertRaises(ValueError): addons.verify(self.project, self.bindings)
                if bridge.is_symlink() or bridge.exists(): bridge.unlink()
                (bridge.parent / 'unrelated.gd').unlink(missing_ok=True); bridge.write_bytes(body)
        addons.verify(self.project, self.bindings)

    def test_receipt_binds_exact_cli_lock_and_ordinary_release_bytes(self):
        self.prepare()
        binary = self.root / 'other-gdam'; shutil.copyfile(self.bindings['binary'], binary); binary.chmod(0o700)
        binary.write_bytes(binary.read_bytes() + b'changed')
        with self.assertRaisesRegex(ValueError, 'pinned CLI'): addons.verify(self.project, {**self.bindings, 'binary': binary})
        lock = self.project / 'gdam.lock'; body = lock.read_bytes(); lock.write_bytes(body + b'\n')
        with self.assertRaisesRegex(ValueError, 'frozen sources'): addons.verify(self.project, self.bindings)
        lock.write_bytes(body)
        (self.project / 'addons/@example_leaf/value.gd').write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'release'): addons.verify(self.project, self.bindings)

    def test_missing_bridge_refuses_before_engine_or_native_assertions_without_regeneration(self):
        self.prepare(); (self.project / 'project.godot').write_text('[application]\nconfig/name="Fixture"\n')
        bridge = self.project / 'addons/@example_consumer/.gdam/deps.gd'; bridge.unlink()
        config = {'components': {'godot': {}},
            'tests': {'unit': [{'runner': 'python', 'path': 'tests/assertion.py'}]}}
        with patch.object(godot_addon_bindings, 'installed', return_value=self.bindings), patch.object(godot_game, 'native_godot') as engine:
            with self.assertRaises(ValueError): godot_game.import_native(self.root, config, {})
            engine.assert_not_called()
            with patch.object(godot_game.komizo_sdk, 'check'), patch.object(godot_game, 'run_entries') as assertions:
                with self.assertRaises(ValueError): godot_game.native_tests(self.root, config, {}, 'unit')
                assertions.assert_not_called()
        self.assertFalse(bridge.exists())


class BindingCapabilitySelection(unittest.TestCase):
    def test_source_lint_does_not_require_or_generate_native_dependency_bindings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); (root / 'app').mkdir(); (root / 'app/project.godot').touch()
            config = {'components': {'godot': {}},
                'tests': {'lint': [{'runner': 'python', 'path': 'tests/source.py'}]}}
            with patch.object(godot_game, 'verify_addons') as verify, patch.object(godot_game, 'run_entries') as run:
                godot_game.native_tests(root, config, {}, 'lint')
            verify.assert_not_called(); run.assert_called_once()

    def test_worker_installs_explicit_pinned_generator_only_during_setup(self):
        import godot_ci
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'mise.toml').write_text('[tools]\npython="3.13.11"\n"github:aviorstudio/gdam"={version="v0.2.0",bin="gdam",asset_pattern="gdam_Linux_x86_64.tar.gz"}\n')
            config = {'components': {'godot': {'addon_bindings': {'tool': 'github:aviorstudio/gdam'}}}}
            entry = {'kind': 'unit', 'cases': []}
            with patch.object(godot_ci, 'declaration_entry', return_value=entry):
                self.assertEqual(godot_ci.worker_tools(root, config, {'COOKBOOK_GODOT_ENTRY': 'unit-1-1'}),
                    ['github:aviorstudio/gdam', 'python'])
            class Installed(Exception): pass
            with patch.object(godot_game, 'run', side_effect=Installed) as run:
                with self.assertRaises(Installed): godot_ci.setup_worker(root, config, {}, entry)
            self.assertEqual(run.call_args.args[0], ['mise', 'install', 'github:aviorstudio/gdam', 'python'])
            for selected in [{'tool': 'custom/program'}, {'tool': 'github:aviorstudio/gdam', 'command': 'install'}]:
                config['components']['godot']['addon_bindings'] = selected
                with self.assertRaises(ValueError): godot_addon_bindings.declaration(root, config)


if __name__ == '__main__': unittest.main()
