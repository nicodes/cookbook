#!/usr/bin/env python3
"""Native Godot application recipe; domain source and assertions remain caller-owned."""
import argparse
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor

import sdk_environment

from configuration import godot_pins, load, relative, runtime_configuration
from recipe_contract import app_directory, release_definition
from native_tests import run_entries, runner_tools
import rust_component
import rust_staging
import go_component
import image_fixtures
import asset_fixtures
import godot_package
import godot_sdk
import node_component
import images
import application_development
import playwright_sdk
import komizo_sdk
import workload_fixture
import godot_addon_bindings

import dependencies

COOKBOOK = Path(__file__).resolve().parents[1]
TOOLS = dependencies.tools_root()
GODOT_RELEASE_ORIGIN = 'godot'


def helper(name):
    return dependencies.recipe_helper(name)


def verification_scope(root, config):
    values = config.get('artifacts', {})
    if set(values) - {'paths', 'images', 'environment', 'revision_environment', 'release_metadata', 'evidence_paths', 'evidence_retention_days'}:
        raise ValueError('unknown Godot build artifact selector')
    if 'evidence_retention_days' in values:helper('native-diagnostics').declaration(root,config)
    verification = helper('verification')
    if config.get('components', {}).get('assets') or config.get('components', {}).get('images'):
        inputs = verification.release_definition(relative(root, release_definition(config), exists=True))['inputs']
    else:
        inputs = {'artifacts': values.get('paths', []), 'images': values.get('images', []),
            'environment': values.get('environment', []), 'revision_environment': values.get('revision_environment', [])}
    inputs = {key: list(value) for key, value in inputs.items()}
    metadata = values.get('release_metadata', [])
    inputs['artifacts'] += (export_import_boundaries(root, config) + metadata + rust_staging.artifact_paths(rust_components(config))
        + go_component.artifact_paths(root,config) + image_fixtures.artifact_paths(root,config) + asset_fixtures.artifact_paths(root,config))
    inputs['images'] += image_fixtures.references(root,config)
    return verification.scope(**inputs)


def rust_components(config):
    return config.get('components', {}).get('rust', [])


def export_import_boundaries(root, config):
    settings = config.get('components', {}).get('godot', {})
    result = set()
    for export in settings.get('exports', []):
        if 'project' in settings or 'project' in export:
            raise ValueError('Godot projects use the canonical application directory')
        project_root = relative(root, app_directory(config))
        output = relative(root, export['output'])
        build = project_root / 'build'
        if output.is_relative_to(build):
            result.add((build / '.gdignore').relative_to(root).as_posix())
    return sorted(result)


def prepare_export_import_boundaries(root, config, *, imported=False):
    names = export_import_boundaries(root, config)
    expected = {}
    if imported and names:
        receipt = relative(root, '.artifacts/verification/build.json', exists=True)
        if not receipt.is_file() or receipt.stat().st_size > 32 << 20:
            raise ValueError('imported Godot boundaries require their bounded accepted build receipt')
        expected = json.loads(receipt.read_text()).get('artifacts', {})
        if not isinstance(expected, dict):
            raise ValueError('imported Godot boundaries require their accepted artifact inventory')
    for name in names:
        path = relative(root, name)
        if path.exists():
            if not path.is_file() or path.stat().st_size > 4096:
                raise ValueError('Godot generated build import boundary must be a bounded regular file')
        else:
            if imported:
                raise ValueError('imported Godot build boundary is missing; setup may not repair accepted artifacts')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'')
        if imported and hashlib.sha256(path.read_bytes()).hexdigest() != expected.get(name):
            raise ValueError('imported Godot build boundary differs from its accepted artifact bytes')


def cargo_records(config, environment):
    records = []
    for component in rust_components(config):
        record = {key: value for key, value in component.items() if key not in {'output_directory', 'output_names'}}
        if any(key in {'APP_VERSION', 'SOURCE_COMMIT', 'CI', 'TOOLS_ROOT', 'KOMIZO_ACTIONS_ROOT', 'GH_TOKEN', 'GITHUB_TOKEN',
                'TOOLS_VERIFICATION_SESSION', 'GOTOOLCHAIN', 'RUSTUP_AUTO_INSTALL'}
                or key.startswith(('GITHUB_', 'MISE_', 'COOKBOOK_', 'REQUEST_')) for key in record.get('environment', {})):
            raise ValueError('Rust component environment may not replace execution authority')
        record['environment'] = {key: value.replace('{revision}', environment.get('APP_VERSION', ''))
            for key, value in record.get('environment', {}).items()}
        records.append(record)
    return records


def build_rust(root, config, environment):
    records = cargo_records(config, environment)
    if records:
        rust_staging.compile_and_stage(root, rust_components(config), records, environment, run)


def artifact_paths(root, config):
    paths=list(verification_scope(root, config)['artifacts'])
    if config.get('components',{}).get('image_security'):
        paths.append(config['components']['image_security']['reports'])
    if config.get('components',{}).get('browser'):
        paths.append(config['components']['browser']['reports'])
    return paths


def evidence_paths(root, config):
    paths=[]
    if config.get('components',{}).get('rust') and 'rust_inventory_manifests' in config.get('security',{}):
        paths.append('.artifacts/security/rust-source.json')
        if config.get('security',{}).get('rust_inventory_sbom',False):paths.append('.artifacts/security/rust-source-sbom.json')
    if config.get('components',{}).get('image_security'):
        import godot_security
        paths.extend(godot_security.evidence_paths(root,config))
    if config.get('components',{}).get('browser'):
        reports=relative(root,config['components']['browser']['reports'],exists=True)
        paths.extend(path.relative_to(root).as_posix() for path in sorted(reports.rglob('*.json')))
        if len(paths)>2000:
            raise ValueError('native browser evidence exceeds its file bound')
    return paths


def project(root, config):
    values = config.get('components', {}).get('godot', {})
    if 'project' in values:
        raise ValueError('Godot projects use the canonical application directory')
    directory = relative(root, app_directory(config), exists=True)
    if not (directory / 'project.godot').is_file():
        raise ValueError('Godot application component must own project.godot')
    return directory, values


def native_import_stage(settings):
    """Import after the component's declared build inputs exist."""
    if 'import_stage' in settings:
        raise ValueError('native import stages are derived from their build inputs')
    return 'build' if settings.get('extensions') or settings.get('prepare') else 'setup'


def validate_config(root, config):
    directory, values = project(root, config)
    allowed = {'exports', 'test_jobs', 'import_timeout', 'import_arguments',
               'addon_taint_markers', 'forbidden_diagnostics', 'art_sources',
               'prepare', 'postprocess', 'packs', 'web_releases', 'templates', 'rust_web', 'extensions', 'addon_bindings'}
    if set(values) - allowed:
        raise ValueError('unsupported Godot component setting')
    godot_addon_bindings.declaration(root, config)
    if type(values.get('test_jobs', 4)) is not int or not 1 <= values.get('test_jobs', 4) <= 16:
        raise ValueError('Godot test concurrency must be bounded')
    if type(values.get('import_timeout', 180)) is not int or not 1 <= values.get('import_timeout', 180) <= 600:
        raise ValueError('Godot imports require a bounded reviewed deadline')
    native_import_stage(values)
    seen = set()
    for export in values.get('exports', []):
        if not isinstance(export, dict) or set(export) - {'preset', 'output', 'kind', 'platform', 'runtime_compatibility', 'timeout', 'import_timeout', 'import_arguments', 'privileged_directories', 'source_files'}:
            raise ValueError('unsupported native Godot export setting')
        if export.get('kind', 'release') not in {'release', 'debug'} or export.get('platform', 'web') not in {'web', 'linux', 'windows', 'macos'}:
            raise ValueError('unsupported Godot export kind or platform')
        native_project = directory
        helper('godot-export').variant_options(native_project,[export.get('kind','release')],
            export.get('privileged_directories',[]),export.get('source_files',{}),root,read_sources=False)
        helper('godot-export').native_options(export.get('import_arguments', values.get('import_arguments', [])),
            export.get('timeout', 600), export.get('import_timeout', values.get('import_timeout', 180)))
        output = relative(root, export['output'])
        if output in seen:
            raise ValueError('Godot export destinations must be distinct')
        seen.add(output)
    if values.get('templates') is not None:
        import godot_template
        godot_template.declaration(root,config)
    components = config.get('components', {})
    for component in cargo_records(config, {'APP_VERSION': '0' * 40}):
        rust_component.validate(component, root)
    for path in rust_staging.artifact_paths(rust_components(config)):
        relative(root, path)
    go_component.declaration(root,config)
    image_fixtures.declaration(root,config)
    for package in packages(config):
        node_component.validate(package, root)
    playwright_sdk.declaration(root,config)
    godot_sdk.inputs(root, config)
    godot_package.validate(root, config, verification_scope(root, config)['artifacts'])
    if components.get('browser'):
        import godot_browser
        godot_browser.declaration(root, config)
    if components.get('image_security'):
        import godot_security
        godot_security.declaration(root, config)
    if config.get('security',{}).get('auth_policy'):
        relative(root,config['security']['auth_policy'],exists=True)
    development=config.get('dev')
    if development is not None:
        if not isinstance(development,dict) or development.get('kind') not in {None,'native'}:
            raise ValueError('Godot development requires an editor or owned native application driver')
        if development.get('kind')=='native':
            if set(development)-{'kind','identity','project_prefix','program','cleanup','capture_output'}:
                raise ValueError('unsupported native application development setting')
            if type(development.get('capture_output',False)) is not bool:
                raise ValueError('development output capture must be explicit')
            relative(root,development.get('identity','ghenv.toml'),exists=True)
            development_project(root,config)
            for entry in [development.get('program'),*([development['cleanup']] if development.get('cleanup') else [])]:
                application_development.native_entry(root,entry,{'APP_VERSION':'0'*40},long_running=True,
                    allowed_roots={'app','api','core','tests'})
        elif set(development)-{'mode','state','log'} or development.get('mode','editor') not in {'editor','run'}:
            raise ValueError('unsupported Godot editor session setting')
    for name,entry in config.get('database',{}).items():
        if name not in {'status','migrate','seed','reset'} or config.get('dev',{}).get('kind')!='native':
            raise ValueError('database operations require an implemented native application lifecycle')
        application_development.native_entry(root,entry,{'APP_VERSION':'0'*40},allowed_roots={'app','api','core','tests'})
    for entries in config.get('tests', {}).values():
        for entry in entries:
            policy=entry.get('policy',{})
            if policy:
                if (entry.get('runner')!='godot-check' or not isinstance(policy,dict)
                        or set(policy)-{'deferred_autoloads'} or type(policy.get('deferred_autoloads',False)) is not bool):
                    raise ValueError('Godot game test policies require a literal deferred autoload switch')
                if policy.get('deferred_autoloads'):
                    source=relative(root,entry['path'],exists=True)
                    if source.suffix!='.gd' or not source.is_relative_to(directory) or entry.get('args'):
                        raise ValueError('deferred Node tests require an owned project script and no extra arguments')
    if config.get('package'):
        raise ValueError('Godot delivery packaging integration is not implemented yet')


def packages(config):
    return config.get('node', {}).get('packages', [])


def package_linters(config):
    return [value for value in packages(config)
            if any(value.get(key) for key in ['typecheck', 'lint', 'format_check', 'expo'])]


def required_tools(root, config, stage='full'):
    pins = tomllib.loads((root / 'mise.toml').read_text()).get('tools', {})
    components = config.get('components', {})
    if stage in {'full','local'}:
        mandatory = {'python', 'actionlint', 'github:aviorstudio/gdlint', 'pipx:gdtoolkit'}
        if components.get('rust'):
            mandatory.update({'rust', 'cargo:cargo-audit'})
            if 'rust_inventory_manifests' in config.get('security',{}):
                mandatory.add('trivy')
        if components.get('go'):
            mandatory.add('go')
        if components.get('godot', {}).get('art_sources'):
            mandatory.add('bun')
        if components.get('playwright'):
            mandatory.add('node')
        for value in packages(config):
            mandatory.add('bun' if value.get('manager', 'npm') == 'bun' else 'node')
        for entries in config.get('tests', {}).values():
            for entry in entries:
                if entry['runner'] in {'node', 'node-test'}:
                    mandatory.add('node')
                if entry['runner'] in {'bun', 'bun-test'}:
                    mandatory.add('bun')
        for phase in ['prepare', 'postprocess']:
            for record in components.get('godot', {}).get(phase, []):
                if record['runner'] in {'node', 'bun'}:
                    mandatory.add(record['runner'])
        if not mandatory <= pins.keys():
            raise ValueError('required native SDKs must be declared in caller Mise pins: ' + ', '.join(sorted(mandatory - pins.keys())))
        selected = list(pins)
    else:
        selected = ['python']
        if stage == 'fast':
            selected += ['actionlint', 'github:aviorstudio/gdlint']
            selected += sorted(runner_tools(config.get('tests', {}).get('lint', [])))
        if components.get('rust'):
            selected.append('rust')
            if stage=='security':
                selected.append('cargo:cargo-audit')
                if 'rust_inventory_manifests' in config.get('security',{}):
                    selected.append('trivy')
        active_packages = package_linters(config) if stage == 'fast' else [value for value in packages(config) if value.get('audit', True)]
        for value in active_packages:
            selected.append('bun' if value.get('manager', 'npm') == 'bun' else 'node')
            if (relative(root, value['directory']) / 'security-remediations.toml').exists():
                selected.append('node')
    if config.get('security',{}).get('auth_policy') and stage in {'full','security'}:
        selected.append('bun')
    if any(name not in pins for name in selected):
        raise ValueError('required native SDK is not pinned in mise.toml')
    return sorted(set(selected))


def environment(root, config):
    source = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    if os.environ.get('SOURCE_COMMIT', source) != source:
        raise ValueError('SOURCE_COMMIT must identify this exact checkout')
    values = {**sdk_environment.preserve_mise(), 'TOOLS_ROOT': str(TOOLS), 'COOKBOOK_CALLER_ROOT': str(root.resolve()), 'SOURCE_COMMIT': source,
              'GODOT_BIN': str(root / '.artifacts/godot/bin/godot'),
              'GDRUST_GODOT_BIN': str(root / '.artifacts/godot/bin/godot'),
              'XDG_DATA_HOME': str(root / '.artifacts/godot-data'),
              'PLAYWRIGHT_BROWSERS_PATH': str(root / '.artifacts/playwright'),
              'RUNNER_TEMP': os.environ.get('RUNNER_TEMP', str(root / '.artifacts/tmp')),
              'PYTHONDONTWRITEBYTECODE': '1'}
    komizo=komizo_sdk.source_root(root,config)
    if komizo is not None:
        if os.environ.get('KOMIZO_ACTIONS_ROOT') not in {None,'',str(komizo)}:
            raise ValueError('KOMIZO_ACTIONS_ROOT must select this checkout exact accepted SDK')
        values['KOMIZO_ACTIONS_ROOT']=str(komizo)
    values['PATH'] = str(root / '.artifacts/godot/bin') + os.pathsep + os.environ.get('PATH', '')
    values['APP_VERSION'] = source
    if config.get('components', {}).get('assets') or config.get('components', {}).get('images'):
        definition = images.definition(root, config)
        for key, expected in [('IMAGE_BASE', definition['image_base'])]:
            if os.environ.get(key, expected) != expected:
                raise ValueError('image selection differs from the caller release authority: ' + key)
            values[key] = expected
    return values


def configure_environment(root, config):
    return environment(root, config)



def development_api():
    from types import SimpleNamespace
    return SimpleNamespace(**{name:globals()[name] for name in [
        'run','native','select_port','pinned_ghenv','development_project',
        'isolated_operation','stop_services','helper']})


def native(root, entries, environment, *, sdk=False, long_running=False):
    return application_development.native(root,entries,environment,api=development_api(),
        sdk=sdk,long_running=long_running,allowed_roots={'app','api','core','tests'},
        protected_environment={'SOURCE_COMMIT','APP_VERSION','CI','TOOLS_ROOT','KOMIZO_ACTIONS_ROOT','GH_TOKEN','GITHUB_TOKEN','TOOLS_VERIFICATION_SESSION','GOTOOLCHAIN','RUSTUP_AUTO_INSTALL'})
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
    pins=tomllib.loads((root/'mise.toml').read_text()).get('tools',{})
    if pins.get('python')!='.'.join(map(str,sys.version_info[:3])):
        raise ValueError('use the exact caller-pinned Python SDK; run mise run setup first')
    for name in required_tools(root,config,'local'):
        run(['mise','where',name],root,environment)

def test_suites(root, config):
    if (not config.get('tests', {}).get('unit') and not config.get('components', {}).get('rust')
            and not any(value.get('test') or value.get('test_paths') for value in packages(config))):
        raise ValueError('Godot applications require real native unit checks')
    return ['lint', 'unit', 'security', *[name for name in ['integration', 'e2e'] if config.get('tests', {}).get(name)]]


def run(argv, root, env, timeout=None):
    env = sdk_environment.command_environment(argv, root, env)
    subprocess.run(argv, cwd=root, env=env, check=True, timeout=timeout)


def setup(root, config, env):
    if env.get('COOKBOOK_GODOT_HOST'):
        import godot_ci
        matrix = godot_ci.host_matrix(root, config, env)
        entry = next(value for value in matrix['requirements'] if value['kind'] == 'host')
        godot_ci.setup_worker(root, config, env, entry)
        return
    requested_stage = os.environ.get('COOKBOOK_STAGE', 'full')
    stage = 'full' if requested_stage == 'local' else requested_stage
    selected = required_tools(root, config, stage)
    run(['mise', 'install', *selected], root, env)
    components = config.get('components', {})
    sdk = lambda argv, directory, environment: run(argv, directory, environment)
    if components.get('python') and stage in {'fast', 'full', 'security'}:
        sdk(['python3', str(COOKBOOK / 'cookbook/python_component.py'), 'setup'], root, env)
    if stage == 'full' and godot_sdk.inputs(root, config) is not None:
        sdk(['python3', str(COOKBOOK / 'cookbook/godot_sdk.py'), 'setup'], root, env)
        env = godot_sdk.activate(root, config, env)
        os.environ.update(env)
    if components.get('rust') and stage in {'fast', 'full'}:
        rust_component.setup(root, cargo_records(config, env), env, sdk)
    if stage in {'full', 'security', 'fast'}:
        node_component.install(root, package_linters(config) if stage == 'fast' else packages(config), env, sdk)
    if stage=='full' and env.get('COOKBOOK_GODOT_ENTRY')!='build' and requested_stage!='local':
        playwright_sdk.setup(root,config,env,run)
    if components.get('image_security') and stage in {'full','security'} and env.get('COOKBOOK_GODOT_ENTRY')!='build' and requested_stage!='local':
        import godot_security
        godot_security.setup(root,config,env,run)
    directory, _ = project(root, config)
    if stage != 'full':
        return
    if env.get('COOKBOOK_GODOT_ENTRY')!='build':komizo_sdk.setup(root,config,env)
    if not env.get('COOKBOOK_GODOT_ENTRY') or env.get('COOKBOOK_GODOT_ENTRY')=='build':
        workload_fixture.setup(root,config,env,run)
    go_component.setup(root,config,env,run)
    if components.get('browser') and not env.get('COOKBOOK_GODOT_ENTRY') and requested_stage!='local':
        import godot_browser
        godot_browser.setup(root, config, env)
    directory, settings = project(root, config)
    pins = godot_pins(root)
    (root / '.artifacts').mkdir(exist_ok=True)
    (root / '.artifacts/.gdignore').touch()
    (root / '.artifacts/tmp').mkdir(exist_ok=True)
    run(sdk_environment.python_entry(str(TOOLS / 'helpers/godot-setup.py'), '--version', pins['GODOT_VERSION'], '--expected-version', pins['GODOT_EXPECTED_VERSION'], '--binary-checksum', pins['GODOT_BINARY_CHECKSUM'], '--templates-checksum', pins['GODOT_TEMPLATES_CHECKSUM'], '--templates', '--root', str(root / '.artifacts/godot'), '--origin', GODOT_RELEASE_ORIGIN), root, env)
    if settings.get('templates') is not None:
        import godot_template
        godot_template.setup(root,config,env)
    if (directory / 'gdam.toml').exists():
        runtime_configuration().prepare(root, (directory / 'gdam.toml').relative_to(root), 'gdam')
        helper('godot-addons').restore(directory, taint_markers=settings.get('addon_taint_markers', []),
            bindings=godot_addon_bindings.installed(root, config, env))
    if native_import_stage(settings) == 'setup':
        import_native(root, config, env)


def import_native(root, config, env):
    imported = bool(env.get('COOKBOOK_GODOT_HOST') or env.get('COOKBOOK_GODOT_ENTRY') not in {None, '', 'build'})
    prepare_export_import_boundaries(root, config, imported=imported)
    directory, settings = project(root, config)
    verify_addons(root, config, env)
    strict = helper('godot-game-runner')
    helper('godot-project').import_project(directory, env['GODOT_BIN'],
        lambda argv, timeout: strict.main(argv, timeout=timeout, environment=env),
        timeout=settings.get('import_timeout', 180), arguments=settings.get('import_arguments', []),
        preserve_defaults=True)


def current_build(root, config, verify):
    import ci
    declaration = verification_scope(root, config)
    receipt = json.loads((root / '.artifacts/verification/build.json').read_text())
    if receipt.get('identity') != verify.identity(declaration) or receipt.get('artifacts') != ci.artifacts(root, config, declaration):
        raise ValueError('build outputs are missing, changed or stale; run mise run build first')


def native_godot(root, config, entry, env):
    directory, settings = project(root, config)
    source = relative(root, entry['path'], exists=True)
    cwd = relative(root, entry.get('cwd', '.'), exists=True)
    argv = [env['GODOT_BIN'], '--headless', '--path', str(directory)]
    if entry.get('policy',{}).get('deferred_autoloads'):
        if (source.suffix!='.gd' or not source.is_relative_to(directory) or entry.get('args')
                or entry['policy']['deferred_autoloads'] is not True):
            raise ValueError('deferred Node tests require an owned project script and a literal true policy')
        argv += ['--script',str(TOOLS/'helpers/godot-test-entry.gd'),'--','res://'+source.relative_to(directory).as_posix()]
    elif source.suffix == '.gd':
        argv += ['--script', str(source)]
    else:
        argv.append(str(source))
    argv += entry.get('args', [])
    # The native helper receives explicit project paths; no user-owned task or
    # shell implementation is introduced by this metadata.
    result = helper('godot-suite').run_one(argv,root/'.artifacts/godot-tests',name=entry['path'],
        timeout=entry.get('timeout',180),sentinel=entry.get('sentinel'),
        forbidden=settings.get('forbidden_diagnostics',[]),environment=env,cwd=cwd)
    if result['exit_code']:
        raise ValueError('native Godot assertion failed: ' + entry['path'])


def verify_addons(root, config, env):
    directory, _ = project(root, config)
    if (directory / 'gdam.toml').exists():
        runtime_configuration().verify(root, (directory / 'gdam.toml').relative_to(root), 'gdam')
        helper('godot-addons').verify(directory, bindings=godot_addon_bindings.installed(root, config, env))


def native_tests(root, config, env, suite):
    if suite!='lint':komizo_sdk.check(root,config,env)
    import godot_ci
    entries = godot_ci.selected_entries(root, config, env, suite)
    selected = godot_ci.declaration_entry(root, config, env['COOKBOOK_GODOT_ENTRY']) if env.get('COOKBOOK_GODOT_ENTRY') else None
    if suite != 'lint' and (selected is None or selected['kind'] in {'unit', 'host'} or selected.get('setup') == 'godot'):
        verify_addons(root, config, env)
    _, settings = project(root, config)
    godot_entries = [entry for entry in entries if entry['runner'] == 'godot-check']
    others = [entry for entry in entries if entry['runner'] != 'godot-check']
    if others:
        import godot_browser
        custom = {'godot-browser': lambda checkout, entry, environment: godot_browser.journeys(checkout, config, environment, entry)}
        wrapper = None
        if selected and selected.get('virtual_display'):
            import virtual_display
            wrapper = virtual_display.command
        run_entries(root, {**config, 'tests': {suite: others}}, env, suite, custom_runners=custom, command_wrapper=wrapper)
    if godot_entries:
        jobs = settings.get('test_jobs', 4)
        if type(jobs) is not int or not 1 <= jobs <= 16:
            raise ValueError('Godot test concurrency must be explicitly bounded')
        with ThreadPoolExecutor(max_workers=jobs) as executor:
            list(executor.map(lambda entry: native_godot(root, config, entry, env), godot_entries))


def pinned_gdlint(root, env):
    pins = tomllib.loads((root / 'mise.toml').read_text()).get('tools', {})
    if 'github:aviorstudio/gdlint' not in pins:
        raise ValueError('native Godot lint requires the caller-owned Gdlint SDK pin')
    value = subprocess.check_output(['mise', 'where', 'github:aviorstudio/gdlint'],
                                    cwd=root, env=env, text=True).strip()
    directory = Path(value)
    if not directory.is_absolute() or not directory.is_dir():
        raise ValueError('native Gdlint requires its installed pinned SDK directory')
    executable = directory / ('gdlint.exe' if os.name == 'nt' else 'gdlint')
    if executable.is_symlink() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError('native Gdlint requires its installed regular executable')
    return str(executable)


def lint(root, config, env):
    directory, settings = project(root, config)
    if (root / '.github/workflows').is_dir():
        run(['actionlint'], root, env)
    run(sdk_environment.python_entry(str(TOOLS / 'helpers/documentation-policy.py')), root, env)
    command = [pinned_gdlint(root, env), '-warn']
    if (directory / 'gdlint.toml').exists():
        with runtime_configuration().scoped(root, (directory / 'gdlint.toml').relative_to(root), 'gdlint'):
            run(command, directory, env)
    else:
        run(command, directory, env)
    node_component.lint(root, packages(config), env, run)
    components = config.get('components', {})
    if components.get('rust'):
        rust_component.lint(root, cargo_records(config, env), env, run)
    if config.get('tests', {}).get('lint'):
        native_tests(root, config, env, 'lint')


def security(root, config, env):
    command = sdk_environment.python_entry(str(TOOLS / 'helpers/source-secrets.py'))
    if config.get('security', {}).get('fixtures'):
        command += ['--fixtures', str(relative(root, config['security']['fixtures'], exists=True))]
    run(command, root, env)
    if config.get('components',{}).get('rust'):
        manifests=images.rust_inventory_manifests(root,config)
        if 'rust_inventory_manifests' in config.get('security',{}):
            images.rust_inventory(root,manifests,env,run,sbom=config.get('security',{}).get('rust_inventory_sbom',False))
        for manifest in manifests:
            lock=str(Path(manifest).with_name('Cargo.lock'))
            run(['cargo','audit','--file',lock,'--deny','warnings'],root,env)
    node_component.audit(root, packages(config), env, run)
    if config.get('security',{}).get('auth_policy'):
        run(sdk_environment.python_entry(str(COOKBOOK / 'cookbook/auth_boundary.py'), '--policy', config['security']['auth_policy']),root,env)
    if config.get('components',{}).get('image_security'):
        import godot_security
        godot_security.scan(root,config,env,run)
    if config.get('tests', {}).get('security'):
        native_tests(root, config, env, 'security')


def validate_outputs(root, config, env, compiler=False):
    _, settings = project(root, config)
    for export in settings.get('exports', []):
        output = relative(root, export['output'], exists=True)
        required = [output]
        if export.get('platform', 'web') == 'web':
            required += [output.with_suffix(suffix) for suffix in ['.wasm', '.pck', '.js']]
        for path in required:
            if path.is_symlink() or not path.is_file() or not path.stat().st_size:
                raise ValueError('Godot Web export is missing a required payload: ' + str(path))
    components = config.get('components', {})
    if compiler and components.get('rust'):
        validate = getattr(rust_component, 'validate_build', None)
        if validate is None:
            raise ValueError('shared Cargo build validation is not integrated')
        validate(root, cargo_records(config, env), env, run)
    if compiler:
        for source in settings.get('art_sources', []):
            run(['bun', '--no-macros', str(TOOLS / 'helpers/godot-art-sources.mjs'), str(relative(root, source, exists=True))], root, env)
    if components.get('assets') or components.get('images'):
        images.validate_release(root, config, env)
        images.validate_base_images(root, config, env)
        images.validate_static_assets(root, config, env)
    image_fixtures.verify(root,config,env)
    import ci
    ci.artifacts(root, config, verification_scope(root, config), env)


def compile_outputs(root, config, env):
    prepare_export_import_boundaries(root, config)
    compiler_environment = godot_sdk.activate(root, config, env)
    node_component.build(root, packages(config), env, run)
    directory, settings = project(root, config)
    if (directory / 'gdam.toml').exists():
        verify_addons(root, config, env)
    build_rust(root, config, compiler_environment)
    go_component.build(root,config,env,run)
    # Emscripten owns a bundled Node for its compiler. Native app packaging must
    # keep the caller's Mise Node/Bun selection and its reviewed compression bytes.
    if compiler_environment.get('LIBCLANG_PATH'):
        env = {**env, 'LIBCLANG_PATH': compiler_environment['LIBCLANG_PATH']}
    os.environ.update(env)
    godot_package.extensions(root, config, env)
    godot_package.author(root, config, env, run, 'prepare')
    if native_import_stage(settings) == 'build':
        import_native(root, config, env)
    exports = settings.get('exports', [])
    if not exports:
        raise ValueError('build requires at least one real native export')
    if settings.get('templates') is not None:
        import godot_template
        godot_template.verify(root,config,env)
    # The shared exporter performs strict isolated imports and preserves authored
    # input files. No product tests or dependency installation occur here.
    groups = {}
    for export in exports:
        if 'project' in export:
            raise ValueError('Godot projects use the canonical application directory')
        native_project = directory
        arguments = tuple(export.get('import_arguments', settings.get('import_arguments', [])))
        key = (native_project, arguments, export.get('timeout', 600), export.get('import_timeout', settings.get('import_timeout', 180)),
            tuple(export.get('privileged_directories',[])),tuple(sorted(export.get('source_files',{}).items())))
        groups.setdefault(key, []).append((export['preset'], relative(root, export['output']), export.get('kind', 'release')))
    for (native_project, arguments, timeout, import_timeout, directories, sources), presets in groups.items():
        helper('godot-export').export(native_project, presets, binary=env['GODOT_BIN'],
            import_arguments=arguments, timeout=timeout, import_timeout=import_timeout,
            privileged_directories=directories,source_files=dict(sources),source_root=root)
    godot_package.runtime(root, config)
    godot_package.packs(root, config, env)
    godot_package.author(root, config, env, run, 'postprocess')
    godot_package.package(root, config, env)
    if config.get('components', {}).get('assets') or config.get('components', {}).get('images'):
        images.validate_application(root, config, env, run)
        images.build(root, config, env, run, native=native)
        image_fixtures.package(root,config,env,run)
        images.package(root, config, env, run)
        asset_fixtures.package(root,config,env,native=native)
    validate_outputs(root, config, env, compiler=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['setup', 'fmt', 'build', 'validate', '_build', 'test',
        'test:lint', 'test:unit', 'test:integration', 'test:e2e', 'test:security', '_test', 'clean', 'dev', 'dev:stop', 'dev:logs', '_dev', '_stop-services', '_clean', '_database',
        'db:status','db:migrate','db:seed','db:reset'])
    parser.add_argument('--suite')
    args = parser.parse_args()
    root = Path.cwd().resolve()
    try:
        config = load(root)
        if config['recipe'] != 'godot-game':
            raise ValueError('this adapter requires the Godot application recipe')
        validate_config(root, config)
        env = environment(root, config)
        os.environ.update(env)
        if args.operation not in {'setup', 'clean', '_clean', 'dev:stop', 'dev:logs', '_stop-services'} and (root / 'mise.toml').is_file():
            pins = tomllib.loads((root / 'mise.toml').read_text()).get('tools', {})
            if pins.get('python') != '.'.join(map(str, sys.version_info[:3])):
                raise ValueError('use the exact caller-pinned Python SDK; run mise run setup first')
            stage = 'fast' if args.operation == 'test:lint' or (args.operation == '_test' and args.suite == 'lint') else 'full'
            if args.operation == 'test:security' or (args.operation == '_test' and args.suite == 'security'):
                stage = 'security'
            if env.get('COOKBOOK_GODOT_ENTRY') or env.get('COOKBOOK_GODOT_HOST'):
                import godot_ci
                selected = godot_ci.worker_tools(root, config, env)
            else:
                selected = required_tools(root, config, 'local' if args.operation in {'dev','_dev','_database','db:status','db:migrate','db:seed','db:reset'} else stage)
            for name in selected:
                subprocess.run(['mise', 'where', name], cwd=root, env=env, check=True, stdout=subprocess.DEVNULL)
        verify = helper('verification')
        declaration = verification_scope(root, config)
        if args.operation == 'setup':
            setup(root, config, env)
        elif args.operation == 'fmt':
            tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
            sources = [str(root / name) for name in tracked if name.endswith('.gd') and '/addons/' not in name]
            if sources:
                run(['gdformat', *sources], root, env)
            node_component.format(root, packages(config), env, run)
            if config.get('components', {}).get('rust'):
                rust_component.format(root, cargo_records(config, env), env, run)
        elif args.operation == 'clean':
            if config.get('dev',{}).get('kind')=='native':
                run(sdk_environment.python_entry(str(TOOLS/'helpers/dev.py'), 'clean', '--state', '.local/dev', '--', *isolated_operation(root,config,env,'_clean')),root,env)
            elif config.get('dev'):
                with helper('dev').checkout_lease(root):
                    helper('clean').clean(root, config.get('clean', {}).get('paths', []))
            else:
                helper('clean').clean(root, config.get('clean', {}).get('paths', []))
        elif args.operation == '_clean':
            clean_outputs(root,config,env)
        elif args.operation == '_dev':
            launch_development(root,config,env)
        elif args.operation == '_stop-services':
            stop_services(root,config,env)
        elif args.operation == '_database':
            entry=config.get('database',{}).get(args.suite)
            if not isinstance(entry,dict):raise ValueError('database operation is not implemented')
            native(root,[entry],env)
        elif args.operation.startswith('db:'):
            database(root,config,env,args.operation)
        elif args.operation == '_build':
            compile_outputs(root, config, env)
        elif args.operation == 'build':
            verify.run_command('build', 'build', declaration, sdk_environment.python_entry(str(Path(__file__).resolve()), '_build'), Path('.artifacts/verification'))
        elif args.operation == 'validate':
            current_build(root, config, verify)
            validate_outputs(root, config, env)
        elif args.operation == 'test':
            suites = test_suites(root, config)
            verify.run_session(suites, sdk_environment.python_entry(COOKBOOK / 'cookbook/dispatch.py', '_tests'), Path('.artifacts/verification'))
        elif args.operation == '_test':
            if env.get('COOKBOOK_GODOT_ENTRY'):
                native_tests(root, config, env, args.suite)
            elif args.suite == 'lint':
                lint(root, config, env)
            elif args.suite == 'security':
                security(root, config, env)
            else:
                if args.suite == 'unit':
                    node_component.test(root, packages(config), env, run)
                if args.suite == 'unit':
                    go_component.test(root,config,env,run)
                if args.suite == 'unit' and config.get('components', {}).get('rust'):
                    rust_component.test(root, cargo_records(config, env), env, run)
                directory, _ = project(root, config)
                if (directory / 'gdam.toml').exists():
                    verify_addons(root, config, env)
                native_tests(root, config, env, args.suite)
            if env.get('COOKBOOK_GODOT_HOST'):
                import godot_ci
                godot_ci.require_group(root, config, env, args.suite)
        elif args.operation.startswith('test:'):
            suite = args.operation.split(':', 1)[1]
            if suite in {'integration', 'e2e'}:
                current_build(root, config, verify)
            verify.run_command('test', suite, declaration, sdk_environment.python_entry(str(Path(__file__).resolve()), '_test', '--suite', suite), Path('.artifacts/verification'))
        elif config.get('dev',{}).get('kind')=='native':
            development(root,config,env,args.operation)
        else:
            directory, _ = project(root, config)
            state = config.get('dev', {}).get('state', '.local/dev')
            if not config.get('dev'):
                raise ValueError('development capability is not implemented')
            operation = {'dev': 'run', 'dev:stop': 'stop', 'dev:logs': 'logs'}[args.operation]
            command = sdk_environment.python_entry(str(TOOLS / 'helpers/dev.py'), operation, '--state', state)
            if args.operation == 'dev':
                command += ['--log', config['dev'].get('log', '.artifacts/dev/game.log'), '--', env['GODOT_BIN']]
                if config['dev'].get('mode', 'editor') == 'editor':
                    command.append('--editor')
                command += ['--path', str(directory)]
            run(command, root, env)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
