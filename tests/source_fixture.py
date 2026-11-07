"""Use the production source codec for authored test declarations."""
from pathlib import Path
import importlib.util
import os
import subprocess
import sys
import tempfile

SDK_ROOT = Path(os.environ.get('COOKBOOK_TEST_SOURCE', Path(__file__).resolve().parents[1]))
PREPARED_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import sdk_identity
SDK_REF = sdk_identity.git(SDK_ROOT, 'rev-parse', 'HEAD')
if os.environ.get('COOKBOOK_TEST_SOURCE'):
    sdk_identity.verify(SDK_ROOT, os.environ['COOKBOOK_TEST_REF'])
import dependencies
TOOLS_ROOT = dependencies.tools_root()
source = dependencies.helper('source_config')
write_source = source.write
read_source = source.read
dump_source = source.dumps


def tool_fixture(name):
    """Read a generic test fixture from the separately verified Tools source."""
    if name != 'image_scan_fixture':
        raise ValueError('select the original generic archive test fixture')
    path = dependencies.tools_root() / 'tests' / (name + '.py')
    spec = importlib.util.spec_from_file_location('cookbook_tools_test_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

read = source.read
dumps = source.dumps
write = source.write
equal = source.equal


def parser_root():
    if sdk_identity.git(PREPARED_ROOT, 'rev-parse', 'HEAD') != SDK_REF:
        raise ValueError('parser preparation must belong to the same provider source as the clean SDK')
    return PREPARED_ROOT


def native_entry(command):
    """Expose original SDK arguments only after checking the isolated wrapper."""
    import sdk_source
    if command[:5] != [sys.executable, '-I', '-B', '-c', sdk_source.SDK_ENTRY]:
        raise AssertionError('SDK helper requires the fixed isolated Python entrypoint')
    return [command[0], *command[5:]]


def write_entrypoint(root, revision=None):
    import generate
    root = Path(root)
    if not (root / 'mise.toml').exists():
        (root / 'mise.toml').write_text('[tools]\npython="3.13.11"\nactionlint="1.7.12"\n')
    for name, body in generate.entrypoint_files(root, revision or SDK_REF).items():
        (root / name).write_bytes(body.encode())


def run_task(root, operation='help', *, environment=None, cookbook_root=None):
    import sdk_environment
    root = Path(root)
    values = sdk_environment.preserve_mise(environment)
    source_root = SDK_ROOT if cookbook_root is None else Path(cookbook_root)
    source_ref = SDK_REF if cookbook_root is None else sdk_identity.git(source_root, 'rev-parse', 'HEAD')
    if cookbook_root is not None:
        sdk_identity.verify(source_root, source_ref)
    values.update(COOKBOOK_ROOT=str(source_root), COOKBOOK_REF=source_ref,
                  TOOLS_ROOT=str(dependencies.tools_root()),
                  TOOLS_REF=dependencies.tools_identity()['revision'],
                  PYTHONDONTWRITEBYTECODE='1', MISE_AUTO_INSTALL='false',
                  MISE_EXEC_AUTO_INSTALL='false', MISE_TASK_RUN_AUTO_INSTALL='false',
                  MISE_TRUSTED_CONFIG_PATHS=str(root), MISE_CEILING_PATHS=str(root.parent))
    with tempfile.TemporaryDirectory(prefix='Mise native control ') as directory:
        directory = Path(directory)
        for key in ['MISE_CONFIG_FILE', 'MISE_OVERRIDE_CONFIG_FILENAMES', 'MISE_ENV']:
            values.pop(key, None)
        for name in ['global.toml', 'system.toml']:
            (directory / name).write_text('')
        values.update(MISE_CONFIG_DIR=str(directory / 'config'),
                      MISE_GLOBAL_CONFIG_FILE=str(directory / 'global.toml'),
                      MISE_SYSTEM_CONFIG_FILE=str(directory / 'system.toml'),
                      MISE_CACHE_DIR=str(directory / 'cache'), MISE_STATE_DIR=str(directory / 'state'),
                      MISE_OFFLINE='true')
        return subprocess.run(['mise', 'run', operation], cwd=root, env=values,
                              capture_output=True, text=True)


def run_unit_adapter(root, operation, *, environment):
    values = {**environment, 'COOKBOOK_ROOT': str(SDK_ROOT), 'COOKBOOK_REF': SDK_REF,
              'COOKBOOK_TEST_REF': SDK_REF, 'TOOLS_ROOT': str(dependencies.tools_root()),
              'TOOLS_REF': dependencies.tools_identity()['revision'], 'PYTHONDONTWRITEBYTECODE': '1'}
    return subprocess.run([sys.executable, '-I', '-B', str(SDK_ROOT / 'tests/adapter_fixture.py'),
                           str(SDK_ROOT / 'cookbook/dispatch.py'), operation],
                          cwd=root, env=values, capture_output=True, text=True)
