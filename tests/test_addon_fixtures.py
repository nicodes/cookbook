from source_fixture import TOOLS_ROOT
from source_fixture import SDK_ROOT
import importlib.util
import contextlib
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import addon_fixtures


class ExportControls(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'project.godot').write_text('[application]\n')
        self.entry = dict(project='.', preset='Web', output='dist/web/index.html', timeout=120,
                          required_outputs=['index.html', 'index.wasm'])
        self.config = {'components': {'fixture_exports': [self.entry]}}

    def execute(self, command, **options):
        self.assertEqual(options['timeout'], 120)
        self.assertEqual(command[1:3], ['--headless', '--path'])
        output = Path(command[-1])
        self.assertTrue((output.parent / '.gdignore').is_file())
        output.write_text('native html')
        output.with_suffix('.wasm').write_bytes(b'native wasm')
        return subprocess.CompletedProcess(command, 0, 'export complete\n', '')

    def test_outputs_are_declared_and_freshly_generated(self):
        self.assertEqual(addon_fixtures.artifact_paths(self.root, self.config), ['dist/web'])
        addon_fixtures.build_exports(self.root, self.config, {'GODOT_BIN': '/pinned/godot'}, execute=self.execute)
        self.assertEqual((self.root / 'dist/web/index.wasm').read_bytes(), b'native wasm')

    def test_zero_exit_error_and_stale_output_are_rejected(self):
        output = self.root / 'dist/web'
        output.mkdir(parents=True)
        for name in self.entry['required_outputs']:
            (output / name).write_bytes(b'stale')
        def empty(command, **options):
            return subprocess.CompletedProcess(command, 0, '', '')
        with self.assertRaises((ValueError, FileNotFoundError)):
            addon_fixtures.build_exports(self.root, self.config, {'GODOT_BIN': 'godot'}, execute=empty)
        self.assertFalse((output / 'index.html').exists())
        def error(command, **options):
            self.execute(command, **options)
            return subprocess.CompletedProcess(command, 0, 'SCRIPT ERROR: failed\n', '')
        with self.assertRaisesRegex(ValueError, 'export failed'):
            addon_fixtures.build_exports(self.root, self.config, {'GODOT_BIN': 'godot'}, execute=error)

    def test_rejects_escaping_or_overlapping_output_plan_before_execution(self):
        self.entry['output'] = '../escaped/index.html'
        with self.assertRaises(ValueError):
            addon_fixtures.artifact_paths(self.root, self.config)
        self.entry['output'] = 'dist/web/index.html'
        self.config['components']['fixture_exports'].append(dict(self.entry))
        with self.assertRaisesRegex(ValueError, 'distinct'):
            addon_fixtures.build_exports(self.root, self.config, {'GODOT_BIN': 'godot'}, execute=self.execute)

    def test_import_boundary_refuses_links_and_nonregular_marker_before_export(self):
        output=self.root/'dist/web';output.mkdir(parents=True)
        target=self.root/'owned-source';target.write_bytes(b'untouched source')
        marker=output/'.gdignore';marker.symlink_to(target)
        with self.assertRaises(ValueError):
            addon_fixtures.build_exports(self.root,self.config,{'GODOT_BIN':'godot'},execute=self.execute)
        self.assertEqual(target.read_bytes(),b'untouched source')
        marker.unlink();marker.mkdir()
        with self.assertRaisesRegex(ValueError,'regular file'):
            addon_fixtures.build_exports(self.root,self.config,{'GODOT_BIN':'godot'},execute=self.execute)

    def verification_fixture(self):
        (self.root/'.gitignore').write_text('dist/\n.godot/\n.artifacts/\n')
        subprocess.run(['git','init','--quiet',str(self.root)],check=True)
        for arguments in [['config','user.name','Fixture'],['config','user.email','fixture@example.invalid'],
                          ['add','project.godot','.gitignore'],['commit','--quiet','-m','Native fixture source']]:
            subprocess.run(['git','-C',str(self.root),*arguments],check=True,stdout=subprocess.DEVNULL)
        spec=importlib.util.spec_from_file_location('fixture_verification',TOOLS_ROOT/'helpers/verification.py')
        verify=importlib.util.module_from_spec(spec);spec.loader.exec_module(verify)
        from godot_addon import current_build, verification_scope
        declaration=(verification_scope(self.root,self.config) if self.config.get('package') else
                     verify.scope(artifacts=addon_fixtures.artifact_paths(self.root,self.config)))
        return verify,current_build,declaration

    def test_import_marker_does_not_exempt_artifact_payload_or_extra_file_mutations(self):
        (self.root/'addon').mkdir();(self.root/'addon/owned.gd').write_text('extends Node\n')
        (self.root/'package.files').write_text('owned.gd\n')
        archive=self.root/'dist/@owner_plugin.zip'
        checksum=addon_fixtures.package_helper().build(self.root/'addon',self.root/'package.files',archive)
        archive.with_suffix('.zip.sha256').write_text(checksum+'  '+archive.name+'\n')
        self.config.update(version=1,recipe='godot-addon',package={'archive':'dist/@owner_plugin.zip'})
        verify,current_build,declaration=self.verification_fixture()
        addon_fixtures.build_exports(self.root,self.config,{'GODOT_BIN':'godot'},execute=self.execute)
        with contextlib.chdir(self.root):
            directory=self.root/'.artifacts/verification';directory.mkdir(parents=True)
            receipt={'identity':verify.identity(declaration),'artifacts':verify.artifacts(declaration)}
            import json
            (directory/'build.json').write_text(json.dumps(receipt))
            current_build(self.root,self.config,verify)
            extra=self.root/'dist/web/unexpected.import';extra.write_bytes(b'foreign output')
            with self.assertRaisesRegex(ValueError,'packaged outputs changed'):
                current_build(self.root,self.config,verify)
            extra.unlink();(self.root/'dist/web/index.wasm').write_bytes(b'mutated payload')
            with self.assertRaisesRegex(ValueError,'packaged outputs changed'):
                current_build(self.root,self.config,verify)

    @unittest.skipUnless(os.environ.get('GODOT_BIN'),'real editor import control requires explicitly prepared GODOT_BIN')
    def test_real_editor_keeps_export_artifacts_stable_only_behind_the_owned_boundary(self):
        def chunk(kind,data):
            return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
        image=(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',1,1,8,6,0,0,0))
               +chunk(b'IDAT',zlib.compress(b'\0\xff\0\0\xff'))+chunk(b'IEND',b''))
        (self.root/'project.godot').write_text('config_version=5\n[application]\nconfig/name="SDK import fixture"\n[rendering]\nrenderer/rendering_method="gl_compatibility"\n')
        verify,_guard,declaration=self.verification_fixture()
        def export(command,**options):
            result=self.execute(command,**options);Path(command[-1]).with_suffix('.png').write_bytes(image);return result
        addon_fixtures.build_exports(self.root,self.config,{'GODOT_BIN':os.environ['GODOT_BIN']},execute=export)
        with contextlib.chdir(self.root):
            before=verify.artifacts(declaration)
            subprocess.run([os.environ['GODOT_BIN'],'--headless','--editor','--import','--quit','--path',str(self.root)],check=True,timeout=45,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            self.assertEqual(verify.artifacts(declaration),before)
            (self.root/'dist/web/.gdignore').unlink()
            shutil.rmtree(self.root/'.godot')
            unprotected=verify.artifacts(declaration)
            subprocess.run([os.environ['GODOT_BIN'],'--headless','--editor','--import','--quit','--path',str(self.root)],check=True,timeout=45,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            self.assertNotEqual(verify.artifacts(declaration),unprotected)
            self.assertTrue((self.root/'dist/web/index.png.import').is_file())

    def test_installed_fixture_contains_exact_accepted_archive_bytes(self):
        fixture = self.root / 'tests/web'
        fixture.mkdir(parents=True)
        (fixture / 'project.godot').write_text('[application]\n')
        source = self.root / 'addon'
        source.mkdir()
        (source / 'plugin.cfg').write_bytes(b'accepted plugin bytes')
        manifest = self.root / 'package.files'
        manifest.write_text('plugin.cfg\n')
        archive = self.root / 'dist/plugin.zip'
        helper = addon_fixtures.package_helper()
        digest = helper.build(source, manifest, archive)
        archive.with_suffix('.zip.sha256').write_text(digest + '  plugin.zip\n')
        self.config['package'] = dict(source='addon', manifest='package.files', archive='dist/plugin.zip')
        self.entry.update(project='tests/web', install_archive='addons/@owner_plugin')
        def inspect(command, **options):
            installed = Path(command[3]) / 'addons/@owner_plugin/plugin.cfg'
            self.assertEqual(installed.read_bytes(), b'accepted plugin bytes')
            return self.execute(command, **options)
        addon_fixtures.build_exports(self.root, self.config, {'GODOT_BIN': 'godot'}, execute=inspect)
        self.entry['import_timeout'] = 45
        def import_and_mutate(command, **options):
            if '--import' in command:
                self.assertEqual(options['timeout'], 45)
                return subprocess.CompletedProcess(command, 0, '', '')
            result = self.execute(command, **options)
            (Path(command[3]) / 'addons/@owner_plugin/plugin.cfg').write_bytes(b'changed by engine')
            return result
        with self.assertRaisesRegex(ValueError, 'installed addon bytes'):
            addon_fixtures.build_exports(self.root, self.config, {'GODOT_BIN': 'godot'}, execute=import_and_mutate)
        (source / 'plugin.cfg').write_bytes(b'mutated after packaging')
        with self.assertRaisesRegex(ValueError, 'source'):
            addon_fixtures.build_exports(self.root, self.config, {'GODOT_BIN': 'godot'}, execute=inspect)


if __name__ == '__main__':
    unittest.main()
