"""The provider's existing native suite needs its exact Python and Bun SDKs."""
from source_fixture import SDK_ROOT, SDK_REF
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import provider


class ProviderUnitSDKs(unittest.TestCase):
    def exercise(self, *, missing=False, child_failure=False):
        with tempfile.TemporaryDirectory(prefix='provider SDK stage ') as temporary:
            root = Path(temporary)
            checkout = root / '.artifacts/test-sdk' / SDK_REF
            checkout.mkdir(parents=True)
            calls = []
            def activate(directory, required, environment):
                calls.append('activate')
                self.assertEqual(directory, root)
                self.assertEqual(required, [('python', 'python3'), ('bun', 'bun')])
                self.assertEqual(environment['COOKBOOK_TEST_SOURCE'], str(checkout))
                self.assertEqual(environment['COOKBOOK_TEST_REF'], SDK_REF)
                self.assertEqual(environment['GITHUB_SHA'], 'same-source')
                self.assertEqual(environment['PYTHONDONTWRITEBYTECODE'], '1')
                if missing:
                    raise ValueError('required exact caller SDK is not prepared; run explicit setup')
                return dict(environment, PATH='exact-prepared-Python-and-Bun')
            def run(argv, *, environment):
                calls.append('native')
                self.assertEqual(argv, [sys.executable, '-I', '-B', '-m', 'unittest', 'discover',
                                       '-s', 'tests', '-p', 'test_*.py', '-v'])
                self.assertEqual(environment['PATH'], 'exact-prepared-Python-and-Bun')
                if child_failure:
                    raise subprocess.CalledProcessError(23, argv)
            with patch.object(provider, 'ROOT', root), \
                 patch.dict(os.environ, {'GITHUB_SHA': 'same-source'}, clear=True), \
                 patch.object(provider.dependencies.sdk_source, 'git', return_value=SDK_REF), \
                 patch.object(provider.dependencies.sdk_source, 'verify',
                              side_effect=lambda *_args, **_kwargs: calls.append('verify')) as verify, \
                 patch.object(provider.dependencies, 'helper', return_value=SimpleNamespace(activation=activate)) as helper, \
                 patch.object(provider, 'run', side_effect=run) as native:
                if missing:
                    with self.assertRaisesRegex(ValueError, 'not prepared'):
                        provider.unit()
                    native.assert_not_called()
                    self.assertEqual(calls, ['verify', 'activate'])
                elif child_failure:
                    with self.assertRaises(subprocess.CalledProcessError) as error:
                        provider.unit()
                    self.assertEqual(error.exception.returncode, 23)
                    self.assertEqual(error.exception.cmd[0], sys.executable)
                else:
                    provider.unit()
                    self.assertEqual(calls, ['verify', 'activate', 'native'])
                verify.assert_called_once_with(checkout, SDK_REF, repository='nicodes/cookbook')
                helper.assert_called_once_with('pinned-tools')

    def test_exact_suite_SDKs_are_bound_after_source_verification_before_unchanged_native_argv(self):
        self.exercise()

    def test_missing_Bun_refuses_without_install_skip_or_native_execution(self):
        self.exercise(missing=True)

    def test_native_child_failure_status_and_absolute_interpreter_are_preserved(self):
        self.exercise(child_failure=True)


if __name__ == '__main__':
    unittest.main()
