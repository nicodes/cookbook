"""Exercise dependent-container cleanup on the actual local native engine."""
from source_fixture import SDK_ROOT
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import application_restore
import native_runtime


@unittest.skipUnless(sys.platform.startswith('linux'), 'native namespace lifetime is a Linux control')
class NativeRestoreNamespaceLifetime(unittest.TestCase):
    def test_actual_api_namespace_dependency_is_removed_before_parent_on_success_and_failure(self):
        if shutil.which('podman') is None:
            if os.environ.get('GITHUB_ACTIONS') == 'true':
                self.fail('Linux CI must provide its native Podman engine')
            self.skipTest('local native Podman is unavailable')
        environment = dict(os.environ)
        nonce = secrets.token_hex(16)
        label = 'cookbook.restore-fixture'
        tag = 'localhost/cookbook-restore-fixture-' + nonce + ':controlled'
        names = []
        image = None

        def run(arguments, *, check=True, timeout=30, **kwargs):
            value = native_runtime.run(arguments, environment=environment, check=check,
                capture_output=True, timeout=timeout, **kwargs)
            if len(value.stdout or b'') > 1 << 20 or len(value.stderr or b'') > 1 << 20:
                raise ValueError('native restore fixture output exceeded its bound')
            return value

        def exists(name):
            result = run(['container','exists',name],check=False)
            if result.returncode not in {0,1}:raise ValueError('native fixture presence is uncertain')
            return result.returncode == 0

        def inspect(name):
            values = json.loads(run(['container','inspect',name]).stdout)
            if len(values)!=1:raise ValueError('native fixture identity is ambiguous')
            value = values[0]
            if (value['Config']['Labels'].get(label)!=nonce or value['Name'].lstrip('/')!=name
                    or value['Image'].removeprefix('sha256:')!=image.removeprefix('sha256:')):
                raise ValueError('native fixture ownership changed')
            return value

        try:
            with tempfile.TemporaryDirectory(prefix='restore-namespace-native-') as directory:
                root = Path(directory)
                (root/'hold.c').write_text('#include <unistd.h>\nint main(void) { for (;;) pause(); }\n')
                subprocess.run(['/usr/bin/cc','-static','-Os','-Wall','-Wextra','-Werror',
                    '-o',str(root/'hold'),str(root/'hold.c')],check=True,capture_output=True,timeout=30,
                    env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
                (root/'Containerfile').write_text('FROM scratch\nCOPY hold /hold\nENTRYPOINT ["/hold"]\n')
                run(['build','--format=oci','--layers','--network=none','--label',label+'='+nonce,
                    '--tag',tag,str(root)],timeout=60)
                image = json.loads(run(['image','inspect',tag]).stdout)[0]['Id']
                foreign = 'restore-foreign-' + nonce
                names.append(foreign)
                run(['create','--name',foreign,'--label',label+'='+nonce,'--pull=never',
                    '--network=none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges',image])
                retained = inspect(foreign)
                for failed in [False,True]:
                    parent = 'restore-parent-' + nonce + ('-failure' if failed else '-success')
                    child = 'restore-api-' + nonce + ('-failure' if failed else '-success')
                    names.extend([parent,child])
                    @contextmanager
                    def database():
                        run(['run','--detach','--name',parent,'--label',label+'='+nonce,'--pull=never',
                            '--network=none','--read-only','--read-only-tmpfs=false','--image-volume=ignore',
                            '--user=10001:10001','--cap-drop=ALL','--security-opt=no-new-privileges',
                            '--memory=33554432','--memory-swap=33554432','--cpus=.25','--pids-limit=32',
                            '--timeout=30','--stop-timeout=1',image])
                        parent_record = inspect(parent)
                        try:
                            yield parent_record['Id']
                        finally:
                            self.assertFalse(exists(child), 'API namespace dependency survived into parent teardown')
                            current = inspect(parent)
                            self.assertEqual(current['Id'],parent_record['Id'])
                            run(['rm','--force','--volumes',current['Id']])
                    original = RuntimeError('synthetic restore body failure')
                    try:
                        with database() as namespace, application_restore.restore_containers(environment) as owned:
                            record = {'user':'10001:10001','limits':{'memory_bytes':32<<20,
                                'memory_swap_bytes':32<<20,'milli_cpus':250,'pids':32}}
                            owned.append(child)
                            command = application_restore.container_arguments(record,namespace,child)
                            run([*command,'--label',label+'='+nonce,'--timeout=30','--stop-timeout=1',image])
                            child_record = inspect(child)
                            self.assertEqual(child_record['HostConfig']['NetworkMode'],'container:'+namespace)
                            self.assertTrue(child_record['State']['Running'])
                            if failed:raise original
                    except RuntimeError as error:
                        self.assertTrue(failed);self.assertIs(error,original)
                    self.assertFalse(exists(parent));self.assertFalse(exists(child))
                self.assertEqual({key:inspect(foreign)[key] for key in ['Id','Image','Name','State']},
                                 {key:retained[key] for key in ['Id','Image','Name','State']})
        finally:
            for name in reversed(names):
                if exists(name):run(['rm','--force','--volumes',inspect(name)['Id']])
            if image is not None:
                value = json.loads(run(['image','inspect',image]).stdout)[0]
                if value['Config']['Labels'].get(label)!=nonce:raise ValueError('fixture image ownership changed')
                run(['rmi',tag])


if __name__ == '__main__':
    unittest.main()
