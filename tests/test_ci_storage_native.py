"""Exercise full-host ordering against real, separate rootless OCI stores."""
from source_fixture import SDK_ROOT
from contextlib import ExitStack, chdir
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import ci
import godot_game
import image_import_cache
import native_runtime


@unittest.skipUnless(sys.platform.startswith('linux'), 'rootless OCI stores require Linux')
class NativeHostImportEnvironment(unittest.TestCase):
    def test_actual_rootless_import_setup_and_revalidation_keep_one_store(self):
        if shutil.which('podman') is None or os.geteuid() == 0:
            if os.environ.get('GITHUB_ACTIONS') == 'true':
                self.fail('Linux CI must provide an actual rootless Podman identity')
            self.skipTest('local rootless Podman is unavailable')
        with tempfile.TemporaryDirectory(prefix='ci-storage-native-') as directory:
            root = Path(directory)
            caller = root / 'caller'
            caller.mkdir()
            (caller / '.gitignore').write_text('.artifacts/\n')
            for args in [['init', '-q'], ['add', '.'], ['-c', 'user.name=Fixture',
                         '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'storage fixture']]:
                subprocess.run(['git', *args], cwd=caller, check=True, capture_output=True, timeout=10)
            source = subprocess.check_output(['git', '-C', str(caller), 'rev-parse', 'HEAD'], text=True).strip()
            nonce = secrets.token_hex(16)
            label = 'cookbook.ci-storage-fixture'
            reference = 'localhost/cookbook-ci-storage-' + nonce + ':' + source
            config = {'recipe': 'godot-game'}
            original = dict(os.environ)
            base = dict(original)
            for key in ['CONTAINERS_STORAGE_CONF', 'STORAGE_OPTS']:
                base.pop(key, None)
            config_home = root / 'config'
            config_home.mkdir(mode=0o700)
            base.update(XDG_CONFIG_HOME=str(config_home), STORAGE_DRIVER='vfs',
                        SOURCE_COMMIT=source, COOKBOOK_CALLER_ROOT=str(caller),
                        GITHUB_ENV=str(root / 'fixture-github-env'), GITHUB_OUTPUT='')
            builder, inherited, final = [root / 'builder-data', root / 'inherited-data', caller / '.artifacts/godot-data']
            stores = [builder, inherited, final]
            environments = {path: dict(base, XDG_DATA_HOME=str(path)) for path in stores}
            foreign = 'ci-storage-foreign-' + nonce
            image = None
            foreign_id = None

            def run(args, environment, *, check=True, timeout=30):
                result = native_runtime.run(args, environment=environment, check=check,
                    capture_output=True, timeout=timeout)
                if len(result.stdout or b'') > 1 << 20 or len(result.stderr or b'') > 1 << 20:
                    raise ValueError('native storage observation exceeds its bound')
                return result

            def store(environment):
                value = json.loads(run(['info', '--format=json'], environment).stdout)
                self.assertTrue(value['host']['security']['rootless'])
                actual = Path(value['store']['graphRoot'])
                self.assertEqual(actual, Path(environment['XDG_DATA_HOME']) / 'containers/storage')
                self.assertEqual(value['store']['graphDriverName'], 'vfs')
                self.assertEqual(environment.get('HOME'), original.get('HOME'))
                self.assertEqual(environment.get('XDG_RUNTIME_DIR'), original.get('XDG_RUNTIME_DIR'))
                return actual

            def inspect(environment):
                values = json.loads(run(['image', 'inspect', image], environment).stdout)
                self.assertEqual(len(values), 1)
                value = values[0]
                self.assertEqual(value['Id'], image)
                self.assertEqual(value['Config']['Labels'].get(label), nonce)
                return value

            try:
                context = root / 'context'
                context.mkdir()
                (context / 'public').write_text('owned public OCI store fixture\n')
                (context / 'Containerfile').write_text('FROM scratch\nCOPY public /public\nENTRYPOINT ["/public"]\n')
                for environment in environments.values():
                    store(environment)
                run(['build', '--format=oci', '--layers', '--network=none', '--label', label + '=' + nonce,
                     '--tag', reference, str(context)], environments[builder], timeout=60)
                image = json.loads(run(['image', 'inspect', reference], environments[builder]).stdout)[0]['Id']
                inspect(environments[builder])
                archive = caller / '.artifacts/image.oci.tar'
                archive.parent.mkdir(parents=True, exist_ok=True)
                image_import_cache.dependencies.helper('oci_archive').save_image(reference, archive,
                    environment=environments[builder], timeout=60)
                archives = [{'path': archive.relative_to(caller).as_posix(),
                             'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}]
                expected = {reference: native_runtime.dependencies.helper('container_runtime').config_digest(image)}

                # The previous full-host order imports into A, then Godot selects B.
                image_import_cache.reload(caller, source, archives, expected, environments[inherited], timeout=60)
                inspect(environments[inherited])
                run(['create', '--name', foreign, '--pull=never', '--label', label + '=' + nonce,
                     '--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                     '--memory=33554432', '--memory-swap=33554432', '--cpus=.25', '--pids-limit=32', image],
                    environments[inherited])
                foreign_before = json.loads(run(['container', 'inspect', foreign], environments[inherited]).stdout)[0]
                foreign_id = foreign_before['Id']
                with patch.dict(os.environ, environments[inherited], clear=True):
                    ci.recipe.configure_environment(caller, config)
                    self.assertEqual(store(dict(os.environ)), final / 'containers/storage')
                    self.assertEqual(run(['image', 'exists', image], dict(os.environ), check=False).returncode, 1)
                    refused = run(['image', 'inspect', image], dict(os.environ), check=False)
                    self.assertEqual(refused.returncode, 125, 'counterexample must refuse the missing exact image')

                events = []
                def prepare(selected_root, selected_config, selected_source, _sdk):
                    self.assertEqual(store(dict(os.environ)), final / 'containers/storage')
                    image_import_cache.reload(selected_root, selected_source, archives, expected, dict(os.environ), timeout=60)
                    inspect(dict(os.environ))
                    events.append('import')
                    return {'source': selected_source}
                def revalidate(_root, _config, _matrix, environment):
                    self.assertEqual(store(environment), final / 'containers/storage')
                    inspect(environment)
                    events.append('revalidate')
                def command(args, *, cwd, env, check):
                    self.assertEqual(cwd, caller)
                    inspect(env)
                    events.append(args[-1])
                    return SimpleNamespace(returncode=0)
                matrix = SimpleNamespace(prepare_host=prepare, revalidate_host_build=revalidate,
                                         finalize_host=lambda accepted, _: accepted)
                with chdir(caller), ExitStack() as stack:
                    stack.enter_context(patch.dict(os.environ, environments[inherited], clear=True))
                    stack.enter_context(patch.object(ci, 'load', return_value=config))
                    stack.enter_context(patch.object(ci.orchestration, 'model', return_value={'route': 'godot'}))
                    stack.enter_context(patch.object(ci, 'identity', return_value='c' * 40))
                    stack.enter_context(patch.object(ci.orchestration, 'matrix_module', return_value=matrix))
                    stack.enter_context(patch.object(ci.recipe, 'adapter', return_value=SimpleNamespace(
                        configure_environment=godot_game.configure_environment,
                        artifact_paths=lambda *_: [])))
                    stack.enter_context(patch.object(ci, 'proof', return_value={'accepted': True}))
                    # Dispatch is bounded here; its setup/test boundaries still inspect the actual image store.
                    # The image reader uses subprocess.run too, so intercept only the exact SDK dispatch.
                    original_run = subprocess.run
                    dispatches = {tuple(ci.sdk_environment.python_entry(SDK_ROOT / 'cookbook/dispatch.py', name))
                                  for name in ['setup', 'test']}
                    def dispatch(args, *positional, **kwargs):
                        if tuple(args) in dispatches:
                            return command(args, **kwargs)
                        return original_run(args, *positional, **kwargs)
                    stack.enter_context(patch.object(ci.subprocess, 'run', side_effect=dispatch))
                    ci.run(caller, 'full', source, 'b' * 40)
                self.assertEqual(events, ['import', 'setup', 'revalidate', 'test'])
                foreign_after = json.loads(run(['container', 'inspect', foreign], environments[inherited]).stdout)[0]
                for field in ['Id', 'Image', 'Name', 'State', 'Config']:
                    self.assertEqual(foreign_after[field], foreign_before[field])
                self.assertFalse(foreign_after['State']['Running'])
            finally:
                if foreign_id is not None:
                    current = json.loads(run(['container', 'inspect', foreign_id], environments[inherited]).stdout)[0]
                    if current['Id'] != foreign_id or current['Config']['Labels'].get(label) != nonce:
                        raise ValueError('owned native fixture container identity changed')
                    run(['rm', '--force', '--volumes', foreign_id], environments[inherited])
                if image is not None:
                    for environment in environments.values():
                        result = run(['image', 'exists', image], environment, check=False)
                        if result.returncode == 0:
                            inspect(environment)
                            run(['rmi', image], environment)
                        elif result.returncode != 1:
                            raise ValueError('native fixture image presence is uncertain')


if __name__ == '__main__':
    unittest.main()
