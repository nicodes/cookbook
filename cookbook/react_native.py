#!/usr/bin/env python3
"""React Native application operations behind the shared Mise contract."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib

import sdk_environment

from configuration import load, relative
from recipe_contract import app_directory
import images
import node_component
import postgres_fixture
import expo_exports
import rust_staging
import source_policy
import application_fixtures
import application_development
import komizo_sdk
from native_tests import run_entries, runner_tools

import dependencies

COOKBOOK = Path(__file__).resolve().parents[1]
TOOLS = dependencies.tools_root()
ENVIRONMENT = re.compile('[A-Z][A-Z0-9_]*\\Z')
PROTECTED_ENVIRONMENT = {'APP_VERSION', 'CI', 'TOOLS_ROOT', 'KOMIZO_ACTIONS_ROOT', 'GH_TOKEN', 'GITHUB_TOKEN',
                         'TOOLS_VERIFICATION_SESSION', 'REQUEST_HEAD', 'REQUEST_BASE', 'COOKBOOK_PREVIEW_NUMBER',
                         'GOTOOLCHAIN', 'RUSTUP_AUTO_INSTALL'}
PHASES = {'setup', 'lint', 'pre_packages', 'pre_export', 'post_export', 'post_public', 'pre_images', 'post_images', 'fixtures', 'validate'}


def helper(name):
    return dependencies.recipe_helper(name)


def cargo():
    spec = importlib.util.spec_from_file_location('react_native_cargo', COOKBOOK / 'cookbook/rust_component.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def packages(config):
    return config.get('node', {}).get('packages', [])


def rust_components(config):
    return config.get('components', {}).get('rust', [])


def preview_context(environment):
    return (environment.get('GITHUB_EVENT_NAME') == 'pull_request' or
            bool(re.fullmatch('[a-f0-9]{40}', environment.get('REQUEST_HEAD', '')) and
                 re.fullmatch('[a-f0-9]{40}', environment.get('REQUEST_BASE', ''))))


def configure_environment(root, config):
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    values = {'APP_VERSION': revision, 'CI': '1', 'GOTOOLCHAIN': 'local',
              'TOOLS_ROOT': str(TOOLS), 'COOKBOOK_CALLER_ROOT': str(root.resolve()), 'PYTHONDONTWRITEBYTECODE': '1',
              'PLAYWRIGHT_BROWSERS_PATH': str(root / '.artifacts/playwright'),
              'MISE_AUTO_INSTALL': 'false', 'MISE_EXEC_AUTO_INSTALL': 'false',
              'MISE_TRUSTED_CONFIG_PATHS': str(root)}
    if os.environ.get('APP_VERSION') not in {None, '', revision}:
        raise ValueError('APP_VERSION must match this exact source checkout')
    komizo=komizo_sdk.source_root(root,config)
    if komizo is not None:
        if os.environ.get('KOMIZO_ACTIONS_ROOT') not in {None,'',str(komizo)}:
            raise ValueError('KOMIZO_ACTIONS_ROOT must select this checkout exact accepted SDK')
        values['KOMIZO_ACTIONS_ROOT']=str(komizo)
    app = config.get('components', {}).get('app', {})
    defaults = app.get('environment', {})
    if not isinstance(defaults, dict):
        raise ValueError('application build environment must be a literal named table')
    for name, value in defaults.items():
        if not ENVIRONMENT.fullmatch(name) or not isinstance(value, str) or '\0' in value:
            raise ValueError('application build environment contains an invalid literal')
        if name in PROTECTED_ENVIRONMENT or name.startswith(('GITHUB_', 'MISE_', 'COOKBOOK_', 'REQUEST_')):
            raise ValueError('application literals may not replace source, workflow or pinned SDK authority')
        values[name] = os.environ.get(name, value)
    if preview_context(os.environ):
        for name, value in app.get('preview_environment', {}).items():
            if not ENVIRONMENT.fullmatch(name) or not isinstance(value, str):
                raise ValueError('preview public environment must use literal named values')
            if name in PROTECTED_ENVIRONMENT or name.startswith(('GITHUB_', 'MISE_', 'COOKBOOK_', 'REQUEST_')):
                raise ValueError('preview literals may not replace source, workflow or pinned SDK authority')
            values[name] = os.environ.get(name, value)
        for name, source in app.get('preview_variables', {}).items():
            if name != 'EXPO_PUBLIC_CLERK_PUBLISHABLE_KEY' or source != 'COOKBOOK_PREVIEW_CLERK_KEY':
                raise ValueError('preview variables must select the bounded public Clerk key input')
            value = os.environ.get(source, '')
            if value:
                values[name] = value
    os.environ.update(values)
    return dict(os.environ)


def required_tools(root, config, stage):
    pins = tomllib.loads((root / 'mise.toml').read_text()).get('tools', {})
    if stage in {'local', 'dev'}:
        return list(pins)
    result = {'python', 'node'}
    for value in packages(config):
        package, _ = node_component.validate(value, root)
        if package.get('manager', 'npm') == 'bun':
            result.add('bun')
    if stage == 'fast':
        result |= runner_tools(config.get('tests', {}).get('lint', []))
    if stage in {'fast', 'full'}:
        result.add('actionlint')
        if config.get('lint', {}).get('shell_paths'):
            result.add('shellcheck')
        if rust_components(config):
            result.add('rust')
    if stage in {'unit', 'integration', 'e2e'} and rust_components(config):
        result.add('rust')
    if stage in {'full', 'security'}:
        if rust_components(config) or config.get('security', {}).get('images'):
            result.add('trivy')
        if config.get('security', {}).get('go_images'):
            result |= {'go', 'go:golang.org/x/vuln/cmd/govulncheck'}
        if config.get('security', {}).get('spdx_images'):
            result.add('syft')
    if stage == 'format':
        if rust_components(config):
            result.add('rust')
        if any(node_component.validate(value, root)[0].get('format_paths') for value in packages(config)):
            result.add('npm:prettier')
    absent = result - pins.keys()
    if absent:
        raise ValueError('required SDKs must be declared in mise.toml: ' + ', '.join(sorted(absent)))
    return sorted(result)


def verification_scope(root, config):
    definition = images.definition(root, config)
    inputs = {key: list(value) for key, value in definition['inputs'].items()}
    inputs['artifacts'] += (config.get('artifacts', {}).get('fixture_paths', []) + expo_exports.artifact_paths(root, config)
                            + rust_staging.artifact_paths(rust_components(config)))
    return helper('verification').scope(**inputs)


def artifact_paths(root, config):
    return [*verification_scope(root, config)['artifacts'], *config.get('artifacts', {}).get('evidence_paths', [])]


def test_suites(root, config):
    names = {'lint', 'unit', 'security', *config.get('tests', {})}
    names.update(entry.get('suite') for entry in config.get('components', {}).get('rust_tests', []))
    if not names <= {'lint', 'unit', 'integration', 'e2e', 'security'}:
        raise ValueError('unknown React Native native test suite')
    return [name for name in ['lint', 'unit', 'security', 'integration', 'e2e'] if name in names]


def run(arguments, root, environment, timeout=None):
    environment = sdk_environment.command_environment(arguments, root, environment)
    subprocess.run(arguments, cwd=root, env=environment, check=True, timeout=timeout)



def development_api():
    from types import SimpleNamespace
    return SimpleNamespace(**{name:globals()[name] for name in [
        'run','native','select_port','pinned_ghenv','development_project',
        'isolated_operation','stop_services','helper']})

def native(root, entries, environment, *, sdk=False, long_running=False):
    return application_development.native(root, entries, environment, api=development_api(), sdk=sdk, long_running=long_running, protected_environment=PROTECTED_ENVIRONMENT)


def phase(root, config, environment, name, stage='full'):
    if name not in PHASES:
        raise ValueError('unknown native build component phase')
    entries = []
    for entry in config.get('components', {}).get('native', []):
        if entry.get('phase') not in PHASES:
            raise ValueError('unknown native product build component phase')
        if entry.get('phase') != 'setup' and 'stages' in entry:
            raise ValueError('native build and validation components cannot silently vary by CI stage')
        stages = entry.get('stages', ['full', 'local'])
        if not isinstance(stages, list) or not stages or not set(stages) <= {'fast', 'full', 'local', 'security'}:
            raise ValueError('native SDK setup requires explicit supported stages')
        if entry['phase'] == name and stage in stages:
            entries.append(entry)
    native(root, entries, environment, sdk=name == 'setup')


def cargo_records(config, environment):
    records = []
    for value in rust_components(config):
        record = {key: item for key, item in value.items() if key not in {'output_directory','output_names'}}
        if any(key in PROTECTED_ENVIRONMENT or key.startswith(('GITHUB_', 'MISE_', 'COOKBOOK_', 'REQUEST_')) for key in record.get('environment', {})):
            raise ValueError('Rust component environment may not replace execution authority')
        record['environment'] = {key: item.replace('{revision}', environment['APP_VERSION'])
                                 for key, item in record.get('environment', {}).items()}
        records.append(record)
    return records


def setup(root, config, environment):
    stage = os.environ.get('COOKBOOK_STAGE', 'local')
    pin = tomllib.loads((root / 'mise.toml').read_text())['tools']['python']
    if pin != '.'.join(map(str, sys.version_info[:3])):
        raise ValueError('setup requires the exact caller-pinned Python SDK; run mise run setup')
    run(['mise', 'install', *required_tools(root, config, stage)], root, environment)
    komizo_sdk.setup(root,config,environment)
    if stage in {'fast', 'full', 'local'}:
        source_policy.setup(root, config, environment)
    def sdk(arguments, directory, values, timeout=None):
        run(arguments, directory, values, timeout)
    selected = []
    for value in packages(config):
        package, _ = node_component.validate(value, root)
        if stage != 'fast' or any(package.get(key) for key in ['expo', 'typecheck', 'lint', 'format_check']):
            selected.append(package)
    node_component.install(root, selected, environment, sdk)
    records = cargo_records(config, environment)
    if records:
        if stage == 'fast':
            version = tomllib.loads((root / 'mise.toml').read_text())['tools']['rust']
            sdk(['rustup', 'component', 'add', '--toolchain', version, 'rustfmt'], root, environment)
        elif stage != 'security':
            cargo().setup(root, records, environment, sdk)
    phase(root, config, environment, 'setup', stage)
    if stage in {'local', 'full'}:
        for name in config.get('components', {}).get('playwright', {}).get('packages', []):
            directory = relative(root, name, exists=True)
            command = ['bun', 'x', '--no-install', 'playwright', 'install']
            if os.environ.get('GITHUB_ACTIONS') == 'true':
                command.append('--with-deps')
            sdk([*command, '--only-shell', 'chromium'], directory, environment)


def lint(root, config, environment):
    run(['actionlint'], root, environment)
    run(sdk_environment.python_entry(str(TOOLS / 'helpers/documentation-policy.py')), root, environment)
    source_policy.check(root, config, environment, run)
    for name in config.get('lint', {}).get('caddy_dockerfiles', []):
        run(['bash', str(TOOLS / 'helpers/caddy-pin.sh'),
             str(relative(root, name, exists=True))], root, environment)
    if config.get('lint', {}).get('auth_policy'):
        run(sdk_environment.python_entry(str(COOKBOOK / 'cookbook/auth_boundary.py'), '--policy', str(relative(root, config['lint']['auth_policy'], exists=True))), root, environment)
    node_component.lint(root, packages(config), environment, run)
    for value in packages(config):
        package, directory = node_component.validate(value, root)
        if package.get('expo'):
            run(['node', str(TOOLS / 'helpers/dependency-compatibility.cjs'), str(directory)], root, environment)
    if rust_components(config):
        cargo().lint(root, cargo_records(config, environment), environment, run)
    paths = []
    for name in config.get('lint', {}).get('shell_paths', []):
        paths.append(str(relative(root, name, exists=True)))
    if paths:
        run(['shellcheck', *paths], root, environment)
    images.validate_compose(root, config, environment, run)
    if config.get('tests', {}).get('lint'):
        run_entries(root, config, environment, 'lint')
    phase(root, config, environment, 'lint')


def build(root, config, environment):
    app = config.get('components', {}).get('app', {})
    if preview_context(environment):
        for name, source in app.get('preview_variables', {}).items():
            if not environment.get(source) or environment.get(name) != environment[source]:
                raise ValueError('the declared Preview public Clerk variable is unavailable for this build')
    for name in app.get('forbidden_environment', []):
        if not ENVIRONMENT.fullmatch(name) or environment.get(name):
            raise ValueError('development-only environment cannot enter a shipping build: ' + name)
    phase(root, config, environment, 'pre_packages')
    node_component.build(root, packages(config), environment, run)
    node_component.link(root, config, environment, run)
    phase(root, config, environment, 'pre_export')
    directory = relative(root, app_directory(config), exists=True)
    run(sdk_environment.python_entry(str(TOOLS / 'helpers/export-web.py'), '--app', str(directory)), root, environment)
    exported = relative(root, str(directory.relative_to(root) / 'dist'), exists=True)
    html_count = len(list(exported.rglob('*.html')))
    if not app.get('minimum_html', 1) <= html_count <= app.get('maximum_html', 100000):
        raise ValueError('Expo export does not satisfy the declared HTML output count')
    for path in exported.rglob('*'):
        if path.is_symlink():
            raise ValueError('shipping app output cannot contain symlinks')
        if path.is_file():
            content = path.read_bytes()
            if any(value.encode() in content for value in app.get('forbidden_markers', [])):
                raise ValueError('development fixture marker entered the shipping app')
    phase(root, config, environment, 'post_export')
    if app.get('public_target'):
        target = relative(root, app['public_target'])
        if app.get('replace_public'):
            public = relative(root, 'public')
            if public.exists():
                shutil.rmtree(public)
        elif target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(exported, target)
        if app.get('release_file'):
            (target / 'release.json').write_text(json.dumps({'revision': environment['APP_VERSION']}) + '\n')
    phase(root, config, environment, 'post_public')
    images.validate_compose(root, config, environment, run)
    build_rust(root, config, environment)
    records = cargo_records(config, environment)
    if records:
        cargo().validate_build(root, records, environment, run)
    caddy = config.get('components', {}).get('caddy', {})
    if caddy:
        if caddy.get('host'):
            images.host_caddy(root, environment, run)
        else:
            images.stage_caddy(root)
    images.stage_empty_directories(root, config)
    phase(root, config, environment, 'pre_images')
    if 'config' in images.components(images.definition(root, config)):
        run(['bun', str(TOOLS / 'helpers/hostnames.mjs')], root, environment)
    images.build(root, config, environment, run, native=native)
    phase(root, config, environment, 'post_images')
    expo_exports.build(root, config, environment, run)
    phase(root, config, environment, 'fixtures')
    images.package(root, config, environment, run)
    validate_outputs(root, config, environment)


def build_rust(root, config, environment):
    """Compile and stage declared native binaries; usable by an owned local service."""
    records = cargo_records(config, environment)
    if records:
        rust_staging.compile_and_stage(root, rust_components(config), records, environment, run)

def validate_outputs(root, config, environment):
    images.validate_release(root, config, environment)
    value = images.definition(root, config)
    app = config.get('components', {}).get('app', {})
    images.static_image(value['image_base'] + '-gate:' + environment['APP_VERSION'],
                        relative(root, app.get('image_source', 'public'), exists=True),
                        app.get('installed_root', '/srv/public'))
    for name, contract in config.get('components', {}).get('rust_images', {}).items():
        if name not in images.components(value):
            raise ValueError('Rust runtime contract names an undeclared image')
        images.rust_image(root, value['image_base'] + '-' + name + ':' + environment['APP_VERSION'], contract)
    phase(root, config, environment, 'validate')


def current_build(root, config):
    verification = helper('verification')
    declaration = verification_scope(root, config)
    receipt = json.loads((root / '.artifacts/verification/build.json').read_text())
    if receipt.get('identity') != verification.identity(declaration) or receipt.get('artifacts') != verification.artifacts(declaration):
        raise ValueError('missing, changed or stale build; run mise run build before this check')


def security(root, config, environment):
    command = sdk_environment.python_entry(str(TOOLS / 'helpers/source-secrets.py'))
    if config.get('security', {}).get('fixtures'):
        command += ['--fixtures', str(relative(root, config['security']['fixtures'], exists=True))]
    run(command, root, environment)
    node_component.audit(root, packages(config), environment, run)
    records = cargo_records(config, environment)
    if records:
        images.rust_inventory(root, sorted({record['manifest'] for record in records}), environment, run)
    definition = images.definition(root, config)
    declared = set(images.components(definition))
    for name in config.get('security', {}).get('images', []):
        if name not in declared:
            raise ValueError('security scan names an undeclared release image')
        image = definition['image_base'] + '-' + name + ':' + environment['APP_VERSION']
        output = relative(root, '.artifacts/security')
        output.mkdir(parents=True, exist_ok=True)
        if name in config.get('security', {}).get('spdx_images', []):
            run(['syft', 'docker:' + image, '-o', 'spdx-json=' + str(output / (name + '.spdx.json'))], root, environment)
        run(['trivy', 'image', '--image-src', 'docker', '--scanners', 'vuln', '--pkg-types', 'os,library',
             '--severity', 'HIGH,CRITICAL', '--format', 'json', '--output', str(output / (name + '.json')), image], root, environment)
        run(['trivy', 'image', '--image-src', 'docker', '--scanners', 'vuln', '--pkg-types', 'os,library',
             '--severity', 'HIGH,CRITICAL', '--ignore-unfixed', '--exit-code', '1', image], root, environment)
    pins = tomllib.loads((root / 'mise.toml').read_text())['tools']
    for name in config.get('security', {}).get('go_images', []):
        if name not in declared:
            raise ValueError('Go runtime scan names an undeclared release image')
        run(sdk_environment.python_entry(str(TOOLS / 'helpers/scan-image.py'), definition['image_base'] + '-' + name + ':' + environment['APP_VERSION'], '--expected-go', pins['go']), root, environment)
    if config.get('tests', {}).get('security'):
        run_entries(root, config, environment, 'security')


def behavioral_tests(root, config, environment, suite):
    # Native disposable databases are test fixtures, independent of retained dev
    # data. Their role policy and cleanup are identical in Mise and Actions.
    with postgres_fixture.context(root, config, environment, suite) as selected:
        native_behavioral_tests(root, config, selected, suite)


def native_behavioral_tests(root, config, environment, suite):
    count = 0
    if suite == 'integration' and application_fixtures.recovery(root, config, environment, run):
        count += 1
    if suite == 'unit':
        count += node_component.test(root, packages(config), environment, run)
    records = {}
    for record in cargo_records(config, environment):
        # A later build-only fixture can compile the same native crate with
        # different features. It must not replace the declared product test base.
        records.setdefault(record['manifest'], record)
    for entry in config.get('components', {}).get('rust_tests', []):
        if entry.get('suite') == suite:
            if entry.get('manifest') not in records:
                raise ValueError('native Rust test selects an undeclared component')
            record = records[entry['manifest']] | {'test': entry['selectors']}
            cargo().test(root, [record], environment, run)
            count += 1
    if config.get('tests', {}).get(suite):
        if suite == 'e2e':
            with images.gate_fixture(root, config, environment) as selected:
                run_entries(root, config, selected, suite, custom_runners={'playwright': playwright})
        else:
            run_entries(root, config, environment, suite, custom_runners={'playwright': playwright})
        count += 1
    if not count:
        raise ValueError('native behavioral test group has no real implemented checks: ' + suite)


def playwright(root, entry, environment):
    config = relative(root, entry['path'], exists=True)
    if entry.get('args') or config.suffix not in {'.ts', '.js', '.mjs'}:
        raise ValueError('native Playwright groups must run their actual declared config without skip/list flags')
    cwd = relative(root, entry.get('cwd', 'app'), exists=True)
    command = ['bun', 'x', '--no-install', 'playwright', 'test', '--config', str(config)]
    timeout = entry.get('timeout', 900)
    if sys.platform != 'linux':
        expo_exports.compile_native(command, cwd, environment, timeout)
        return
    # Reuse the proven pidfd/subreaper mechanism without acquiring a dev lease.
    # Playwright servers can open separate process groups; they remain our children.
    processes = helper('dev')
    processes.adopt_descendants()
    child = subprocess.Popen(command, cwd=cwd, env=environment, start_new_session=True)
    try:
        if child.wait(timeout=timeout):
            raise ValueError('native Playwright assertions failed')
    finally:
        processes.stop_descendants(child)


def check_sdks(root, config, operation, environment, suite=None):
    pins = tomllib.loads((root / 'mise.toml').read_text()).get('tools', {})
    if pins.get('python') != '.'.join(map(str, sys.version_info[:3])):
        raise ValueError('use the exact caller-pinned Python SDK; run mise run setup first')
    stage = {'fmt': 'format', 'test:lint': 'fast', 'test:unit': 'unit',
             'test:integration': 'integration', 'test:e2e': 'e2e', 'test:security': 'security',
             'dev': 'dev', '_dev': 'dev', '_database': 'dev',
             **{name: 'dev' for name in ['db:status', 'db:migrate', 'db:seed', 'db:reset']}}.get(operation, 'full')
    if operation == '_test':
        stage = 'fast' if suite == 'lint' else suite
    for name in required_tools(root, config, stage):
        subprocess.run(['mise', 'where', name], cwd=root, env=environment, check=True, stdout=subprocess.DEVNULL)


def development_project(root, config=None):
    return application_development.development_project(root, config)

def select_port(preferred, attempts=100):
    return application_development.select_port(preferred, attempts)

def pinned_ghenv(root, environment):
    return application_development.pinned_ghenv(root, environment)

def launch_development(root, config, environment):
    return application_development.launch_development(root, config, environment, api=development_api())

def clean_outputs(root, config, environment):
    return application_development.clean_outputs(root, config, environment, api=development_api())

def stop_services(root, config, environment):
    return application_development.stop_services(root, config, environment, api=development_api())

def isolated_operation(root, config, environment, operation):
    return application_development.isolated_operation(root, config, environment, operation, api=development_api())

def development(root, config, environment, operation):
    return application_development.development(root, config, environment, operation, api=development_api())

def database(root, config, environment, operation):
    return application_development.database(root, config, environment, operation, api=development_api())


def check_development_sdks(root, config, environment, operation, suite=None):
    check_sdks(root, config, operation, environment, suite)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['setup', 'fmt', 'build', 'validate', '_build', 'test', '_test',
        'test:lint', 'test:unit', 'test:integration', 'test:e2e', 'test:security', 'clean',
        'dev', 'dev:stop', 'dev:logs', '_dev', '_stop-services', '_clean', '_database',
        'db:status', 'db:migrate', 'db:seed', 'db:reset'])
    parser.add_argument('--suite')
    args = parser.parse_args()
    root = Path.cwd().resolve()
    try:
        config = load(root)
        if config['recipe'] != 'react-native':
            raise ValueError('React Native operations require the application recipe')
        environment = configure_environment(root, config)
        if args.operation not in {'setup', 'clean', '_clean', 'dev:stop', 'dev:logs', '_stop-services'}:
            check_sdks(root, config, args.operation, environment, args.suite)
            komizo_sdk.check(root,config,environment)
        if args.operation == 'setup':
            setup(root, config, environment)
        elif args.operation == 'fmt':
            count = node_component.format(root, packages(config), environment, run)
            if rust_components(config):
                cargo().format(root, cargo_records(config, environment), environment, run)
                count += 1
            if not count:
                raise ValueError('recipe has no implemented native formatter')
        elif args.operation == '_build':
            build(root, config, environment)
        elif args.operation == 'validate':
            current_build(root, config)
        elif args.operation == 'clean':
            if config.get('dev'):
                run(sdk_environment.python_entry(str(TOOLS / 'helpers/dev.py'), 'clean', '--state', '.local/dev', '--', *isolated_operation(root, config, environment, '_clean')), root, environment)
            else:
                clean_outputs(root, config, environment)
        elif args.operation == '_clean':
            clean_outputs(root, config, environment)
        elif args.operation in {'dev', 'dev:stop', 'dev:logs'}:
            development(root, config, environment, args.operation)
        elif args.operation == '_dev':
            launch_development(root, config, environment)
        elif args.operation == '_stop-services':
            stop_services(root, config, environment)
        elif args.operation.startswith('db:'):
            database(root, config, environment, args.operation)
        elif args.operation == '_database':
            native(root, [config['database'][args.suite]], environment)
        else:
            verification = helper('verification')
            directory = Path('.artifacts/verification')
            declaration = verification_scope(root, config)
            if args.operation == 'build':
                verification.run_command('build', 'build', declaration,
                    sdk_environment.python_entry(str(Path(__file__).resolve()), '_build'), directory)
            elif args.operation == 'test':
                verification.run_session(test_suites(root, config), sdk_environment.python_entry(COOKBOOK / 'cookbook/dispatch.py', '_tests'), directory)
            elif args.operation == '_test':
                if args.suite == 'lint':
                    lint(root, config, environment)
                elif args.suite == 'security':
                    security(root, config, environment)
                else:
                    behavioral_tests(root, config, environment, args.suite)
            else:
                suite = args.operation.split(':', 1)[1]
                if suite in {'integration', 'e2e', 'security'}:
                    current_build(root, config)
                verification.run_command('test', suite, declaration,
                    sdk_environment.python_entry(str(Path(__file__).resolve()), '_test', '--suite', suite), directory)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
