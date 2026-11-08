#!/usr/bin/env python3
"""Native Godot addon operations behind the shared Mise command contract."""
import argparse
import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

import sdk_environment

from configuration import godot_pins, load, relative
from native_tests import run_entries, runner_tools
import node_component
import recipe_contract

import dependencies

COOKBOOK = Path(__file__).resolve().parents[1]
TOOLS = dependencies.tools_root()


def helper(name):
    return dependencies.recipe_helper(name)


def environment(root):
    pins = tomllib.loads((root / 'mise.toml').read_text()).get('env', {}) if (root / 'mise.toml').is_file() else {}
    return {**sdk_environment.preserve_mise(), **({'GODOT_VERSION': pins['GODOT_VERSION']} if 'GODOT_VERSION' in pins else {}),
            'TOOLS_ROOT': str(TOOLS), 'COOKBOOK_CALLER_ROOT': str(root.resolve()),
            'GODOT_BIN': str(root / '.artifacts/godot/bin/godot'),
            'XDG_DATA_HOME': str(root / '.artifacts/godot-data'),
            'XDG_CONFIG_HOME': str(root / '.artifacts/godot-config'),
            'TMPDIR': str(root / '.artifacts/tmp'),
            'GODOT_SILENCE_ROOT_WARNING': '1',
            'PLAYWRIGHT_CLI_BIN': str(root / '.artifacts/playwright-cli'),
            'GD_WEB_OUTPUT_DIR': str(root / '.artifacts/e2e/web-export-evidence'),
            'PLAYWRIGHT_BROWSERS_PATH': str(root / '.artifacts/playwright'),
            'RUNNER_TEMP': os.environ.get('RUNNER_TEMP', str(root / '.artifacts/tmp')),
            'PYTHONDONTWRITEBYTECODE': '1',
            'PATH': str(root / '.artifacts/godot/bin') + os.pathsep + os.environ.get('PATH', '')}


def run(argv, root, env, timeout=None):
    env = sdk_environment.command_environment(argv, root, env)
    subprocess.run(argv, cwd=root, env=env, check=True, timeout=timeout)


def package_data(config):
    values = config.get('package', {})
    if not isinstance(values, dict) or set(values) - {'archive', 'script_uids', 'embedded_manifest'} or 'archive' not in values:
        raise ValueError('addon package paths are derived from its canonical public archive identity')
    recipe_contract.validate_paths(config)
    archive = values['archive']
    if not isinstance(archive, str) or not re.fullmatch(r'dist/@[A-Za-z0-9][A-Za-z0-9_.-]*_[A-Za-z0-9][A-Za-z0-9_.-]*\.zip', archive):
        raise ValueError('addon archive must use dist/@owner_package.zip')
    for name in ['script_uids', 'embedded_manifest']:
        if name in values and type(values[name]) is not bool:
            raise ValueError('addon package capabilities must be explicit boolean data')
    result = {**values, 'source': 'addon', 'manifest': 'package.files'}
    if values.get('embedded_manifest'):
        stem = Path(archive).stem
        result.update(prefix='addons/' + stem + '/', registry_archive='dist/' + stem + '.gdam.zip',
                      version_source='package.json', allowlist='package-allowlist.toml',
                      generated_directories=['.gdam'] if config.get('components', {}).get('addon_dependencies') else [])
    return result


def package_inputs(root, config):
    values = package_data(config)
    return (relative(root, values['source'], exists=True),
            relative(root, values['manifest'], exists=True),
            relative(root, values['archive']))


def package_module(config):
    package_data(config)
    import manifest_addon
    return manifest_addon


def verification_scope(root, config):
    module = package_module(config)
    paths = module.artifact_paths(root, config)
    import addon_fixtures
    paths += addon_fixtures.artifact_paths(root, config)
    return helper('verification').scope(artifacts=paths)


def artifact_paths(root, config):
    return [*verification_scope(root, config)['artifacts'], *evidence_paths(root, config)]


def evidence_paths(root, config):
    paths = list(config.get('artifacts', {}).get('evidence_paths', []))
    for name in config.get('publication', {}).get('evidence_assets', []):
        if name not in paths:
            paths.append(name)
    if config.get('publication', {}).get('provenance'):
        paths.append('dist/CANDIDATE.json')
    if len(paths) != len(set(paths)):
        raise ValueError('artifact evidence paths must be distinct')
    for path in paths:
        relative(root, path)
    return paths


def post_test_evidence(root, config, env, commit):
    provenance = config.get('publication', {}).get('provenance')
    if provenance:
        if provenance != 'package-provenance.toml':
            raise ValueError('candidate provenance requires the native package provenance policy')
        run(['node', str(TOOLS / 'helpers/addon-provenance.mjs'), 'candidate', '--commit', commit,
             '--zip', str(relative(root, config['package']['archive'], exists=True)),
             '--out', str(relative(root, 'dist/CANDIDATE.json'))], root, env)


def packages(config):
    return config.get('node', {}).get('packages', [])


def required_tools(root, config, stage='full'):
    pins = tomllib.loads((root/'mise.toml').read_text()).get('tools', {})
    if stage == 'full':
        return list(pins)
    names = ['python', *(['actionlint'] if stage == 'fast' else [])]
    if stage == 'fast':
        names += sorted(runner_tools(config.get('tests', {}).get('lint', [])) - set(names))
    if packages(config):
        names.append('node')
    if not set(names) <= set(pins):
        raise ValueError('required source-check SDKs must be pinned in mise.toml')
    return names


def test_suites(root, config):
    return ['lint', 'unit', 'security', *[name for name in ['integration', 'e2e'] if name in config.get('tests', {})]]


def configure_environment(root, config):
    return environment(root)


def setup(root, config, env):
    (root / '.artifacts').mkdir(exist_ok=True)
    (root / '.artifacts/.gdignore').touch()
    (root / '.artifacts/tmp').mkdir(exist_ok=True)
    import python_component
    if os.environ.get('COOKBOOK_STAGE') == 'fast':
        # Source/configuration checks do not need an editor or export templates.
        run(['mise', 'install', *required_tools(root, config, 'fast')], root, env)
        python_component.setup(root, config, env)
        return
    run(['mise', 'install'], root, env)
    pins = godot_pins(root)
    command = sdk_environment.python_entry(str(TOOLS / 'helpers/godot-setup.py'), '--version', pins['GODOT_VERSION'], '--expected-version', pins['GODOT_EXPECTED_VERSION'], '--binary-checksum', pins['GODOT_BINARY_CHECKSUM'], '--templates-checksum', pins['GODOT_TEMPLATES_CHECKSUM'], '--origin', 'godot-builds', '--root', str(root / '.artifacts/godot'))
    command.append('--templates')
    run(command, root, env)
    python_component.setup(root, config, env)
    sdk = lambda argv, directory, values: run(argv, directory, values)
    node_component.install(root, packages(config), env, sdk)
    for value in packages(config):
        _data, directory = node_component.validate(value, root)
        if (directory/'node_modules').is_dir():
            (directory/'node_modules/.gdignore').touch()
    import addon_dependencies
    addon_dependencies.setup(root, config, env)
    browser = config.get('components', {}).get('browser')
    if browser:
        if set(browser)-{'package', 'cli_version'}:
            raise ValueError('unsupported native browser setup setting')
        helper('playwright-local').prepare(root, browser['package'], cli_version=browser.get('cli_version'),
            run=lambda argv, **kwargs: subprocess.run(argv, **kwargs))


def lint(root, config, env):
    for name in config.get('lint', {}).get('required_paths', []):
        relative(root, name, exists=True)
    module = package_module(config)
    module.inputs(root, config)
    tracked = subprocess.check_output(['git', 'ls-files', '-z', '--', '*.py', '*.sh'], cwd=root).split(b'\0')
    for encoded in tracked:
        if not encoded:
            continue
        path = relative(root, encoded.decode(), exists=True)
        if path.suffix == '.py':
            ast.parse(path.read_bytes(), filename=str(path))
        else:
            run(['bash', '-n', str(path)], root, env)
    if (root / '.github/workflows').is_dir():
        run(['actionlint'], root, env)
    run(sdk_environment.python_entry(str(TOOLS / 'helpers/documentation-policy.py')), root, env)
    if 'lint' in config.get('tests', {}):
        test_entries(root, config, env, 'lint')


def test_entries(root, config, env, suite):
    run_entries(root, config, env, suite)


def current_build(root, config, verification):
    declaration = verification_scope(root, config)
    receipt = json.loads((root / '.artifacts/verification/build.json').read_text())
    if receipt.get('identity') != verification.identity(declaration):
        raise ValueError('build is stale; run mise run build before packaged integration/E2E tests')
    if receipt.get('artifacts') != verification.artifacts(declaration):
        raise ValueError('packaged outputs changed after build verification')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['setup', 'fmt', 'build', 'validate', '_build',
                                            'test:lint', 'test:unit', 'test:integration',
                                            'test:e2e', 'test:security', '_test', 'test', 'clean'])
    parser.add_argument('--suite')
    args = parser.parse_args()
    root = Path.cwd().resolve()
    try:
        config = load(root)
        if config['recipe'] != 'godot-addon':
            raise ValueError('Godot addon operations require the addon recipe')
        env = environment(root)
        if args.operation not in {'setup', 'clean'} and (root / 'mise.toml').is_file():
            tools = tomllib.loads((root / 'mise.toml').read_text()).get('tools', {})
            if tools.get('python') != '.'.join(map(str, sys.version_info[:3])):
                raise ValueError('use the exact caller-pinned Python SDK; run mise run setup first')
            required = ['python']
            if args.operation == 'fmt':
                required.append('pipx:gdtoolkit')
            if args.operation in {'test', 'test:lint'} or (args.operation == '_test' and args.suite == 'lint'):
                required.append('actionlint')
            if config.get('node', {}).get('packages') and args.operation in {'test', 'test:security', '_test'}:
                required.append('node')
            for name in required:
                if name not in tools:
                    raise ValueError('declare the required SDK in mise.toml: ' + name)
                subprocess.run(['mise', 'where', name], cwd=root, env=env, check=True, stdout=subprocess.DEVNULL)
        if args.operation == 'setup':
            setup(root, config, env)
        elif args.operation == 'fmt':
            paths = config.get('format', {}).get('paths', ['addon', 'tests'])
            for name in paths:
                relative(root, name, exists=True)
            run(['gdformat', *paths], root, env)
        elif args.operation in ['_build', 'validate']:
            if args.operation == '_build':
                node_component.build(root, packages(config), env, run)
            module = package_module(config)
            digest = (module.build if args.operation == '_build' else module.verify)(root, config)
            print(digest + '  ' + config['package']['archive'])
            if args.operation == '_build':
                import addon_fixtures
                addon_fixtures.build_exports(root, {**config, 'package': package_data(config)}, env)
        elif args.operation == 'clean':
            paths = config.get('clean', {}).get('paths', [])
            if not paths:
                raise ValueError('cleanup requires explicit generated output paths')
            helper('clean').clean(root, paths)
        else:
            verification = helper('verification')
            directory = Path('.artifacts/verification')
            declaration = verification_scope(root, config)
            if args.operation == 'build':
                verification.run_command('build', 'build', declaration,
                    sdk_environment.python_entry(str(Path(__file__).resolve()), '_build'), directory)
            elif args.operation == 'test':
                suites = ['lint', 'unit', 'security', *[name for name in ['integration', 'e2e'] if name in config.get('tests', {})]]
                verification.run_session(suites, sdk_environment.python_entry(COOKBOOK / 'cookbook/dispatch.py', '_tests'), directory)
            elif args.operation == '_test':
                if args.suite == 'lint':
                    lint(root, config, env)
                elif args.suite == 'security':
                    scan = sdk_environment.python_entry(str(TOOLS / 'helpers/source-secrets.py'))
                    fixtures = config.get('security', {}).get('fixtures')
                    if fixtures:
                        scan += ['--fixtures', str(relative(root, fixtures, exists=True))]
                    run(scan, root, env)
                    node_component.audit(root, packages(config), env, run)
                    if 'security' in config.get('tests', {}):
                        test_entries(root, config, env, 'security')
                else:
                    test_entries(root, config, env, args.suite)
            else:
                suite = args.operation.split(':', 1)[1]
                if suite in ['integration', 'e2e']:
                    current_build(root, config, verification)
                verification.run_command('test', suite, declaration,
                    sdk_environment.python_entry(str(Path(__file__).resolve()), '_test', '--suite', suite), directory)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
