#!/usr/bin/env python3
"""Rust CLI/TUI recipe operations with optional native Node UI components."""
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
import tempfile
import tomllib

import sdk_environment

from configuration import load, relative
import rust_component
import node_component
import ui_component
import native_components
import rust_tests
import recipe_contract

import dependencies

COOKBOOK = Path(__file__).resolve().parents[1]
TOOLS = dependencies.tools_root()


def helper(name):
    return dependencies.recipe_helper(name)


def run(argv, root, environment, timeout=None):
    environment = sdk_environment.command_environment(argv, root, environment)
    subprocess.run(argv, cwd=root, env=environment, check=True, timeout=timeout)


def components(config):
    values = config.get('components', {}).get('rust')
    if not isinstance(values, list) or not values:
        raise ValueError('Rust recipes require real native Cargo components')
    primary = [value for value in values if value.get('name') == 'cli']
    if (len(primary) != 1 or primary[0].get('manifest', 'Cargo.toml') != 'Cargo.toml'
            or primary[0].get('profile', 'release') != 'release'
            or primary[0].get('build', {}).get('profile', 'release') != 'release'
            or 'release' in primary[0] or 'release' in primary[0].get('build', {})
            or primary[0].get('build', {}).get('enabled') is False):
        raise ValueError('Rust CLI requires one release cli component at Cargo.toml')
    return values


def artifact_settings(config):
    values = config.get('artifacts', {})
    if not isinstance(values, dict) or set(values) - {'evidence_paths'}:
        raise ValueError('Rust artifact paths and identity inputs are derived by the recipe')
    return values


def binding_name(kind, name):
    if not isinstance(name, str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}', name):
        raise ValueError('native binary bindings require literal source names')
    return 'COOKBOOK_' + kind + '_' + name.replace('-', '_').upper() + '_BINARY'


def identity_bindings(config):
    """Retain actual compiler identity aliases without caller-selected authority."""
    result = {}
    for component in native_components.records(config):
        for name, token in component.get('environment', {}).items():
            if (name in {'COMMIT', 'VERSION', 'DATE', 'RUST_TARGET', 'TOOLS_ROOT', 'GH_TOKEN',
                         'TOOLS_VERIFICATION_SESSION', 'PATH', 'CARGO_TARGET_DIR', 'RUNNER_TEMP'}
                    or name.startswith(('GITHUB_', 'COOKBOOK_'))):
                raise ValueError('native identity bindings cannot replace recipe or execution authority')
            if name == 'SOURCE_COMMIT' and token != '{commit}':
                raise ValueError('SOURCE_COMMIT must identify the checked-out source')
            if name in result and result[name] != token:
                raise ValueError('native identity bindings cannot conflict across components')
            result[name] = token
    for component in components(config):
        if component.get('environment'):
            raise ValueError('Rust CLI compiler identity inputs belong to typed native bindings')
    return result


def required_tools(root, config, stage):
    pins = tomllib.loads((Path(root) / 'mise.toml').read_text()).get('tools', {})
    if stage == 'full':
        return list(pins)
    if stage not in {'fast', 'security', 'native'}:
        raise ValueError('unknown SDK preparation stage')
    names = ['python']
    if stage == 'native':
        names.append('rust')
        extra = config.get('components', {}).get('native_tools', [])
        if (not isinstance(extra, list) or len(extra) != len(set(extra))
                or any(not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9:._/@-]*', name) for name in extra)):
            raise ValueError('native setup dependencies must select distinct caller-pinned SDK identifiers')
        names += extra
        for fixture in config.get('tests', {}).get('unit', []):
            if fixture['runner'] in {'bun', 'bun-test'}:
                names.append('bun')
            elif fixture['runner'] in {'node', 'node-test'}:
                names.append('node')
    elif stage == 'fast':
        names += ['rust', 'actionlint']
        if config.get('lint', {}).get('shell_paths'):
            names.append('shellcheck')
    else:
        names += ['cargo:cargo-audit']
        if config.get('security', {}).get('trivy_paths') or config.get('security', {}).get('rust_inventory_manifests'):
            names.append('trivy')
    for value in config.get('node', {}).get('packages', []):
        package, _directory = node_component.validate(value, root)
        names.append('bun' if package.get('manager', 'npm') == 'bun' else 'node')
    if set(names) - set(pins):
        raise ValueError('required recipe SDKs are missing from mise.toml: ' + ', '.join(sorted(set(names) - set(pins))))
    return list(dict.fromkeys(names))


def environment(root, config):
    artifact_settings(config)
    package = package_data(root, config)
    source = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    if os.environ.get('COMMIT', source) != source:
        raise ValueError('requested build commit differs from the checked-out source')
    value = {**os.environ, 'TOOLS_ROOT': str(TOOLS), 'COOKBOOK_CALLER_ROOT': str(root.resolve()), 'COMMIT': source,
             'VERSION': os.environ.get('VERSION', 'dev'), 'DATE': os.environ.get('DATE', 'unknown'),
             'PYTHONDONTWRITEBYTECODE': '1', 'CARGO_INCREMENTAL': '0',
             'RUNNER_TEMP': os.environ.get('RUNNER_TEMP', str(root / '.artifacts/tmp'))}
    if config.get('components', {}).get('browser'):
        value['PLAYWRIGHT_BROWSERS_PATH'] = str(root / '.artifacts/playwright')
    if value.get('RUST_TARGET', '').endswith('-pc-windows-msvc'):
        value['RUSTFLAGS'] = value.get('RUSTFLAGS', '') + ' -C target-feature=+crt-static'
    for name in ['CC_x86_64_unknown_linux_musl', 'CC_aarch64_unknown_linux_musl']:
        value.setdefault(name, 'musl-gcc')
    value.setdefault('CARGO_TARGET_DIR', str(root / '.artifacts/rust-target'))
    bound = {}
    def bind(name, path):
        path = str(path)
        if name in bound or (name in value and value[name] != path):
            raise ValueError('native binary binding conflicts with another source or caller environment')
        bound[name] = path
        value[name] = path
    bind('COOKBOOK_CLI_BINARY', binary_path(root, package['output']))
    if config.get('components', {}).get('native'):
        record = selected_target(package, value)
        for declaration in native_components.records(config):
            bind(binding_name('NATIVE', declaration['name']),
                 native_components.output(root, declaration, native_components.platform(record)))
    for fixture in config.get('components', {}).get('fixtures', []):
        if 'environment' in fixture:
            raise ValueError('native fixture binary bindings are derived from their binary names')
        record = helper('native_cli').native_target({'native_targets': fixture['native_targets']})
        binary = fixture['binary'] + ('.exe' if record['platform'] == 'Windows' else '')
        bind(binding_name('FIXTURE', fixture['binary']), root / '.artifacts/fixtures' / fixture['binary'] /
             (record['platform'] + '-' + record['arch']) / binary)
    tokens = {'{commit}': source, '{version}': value['VERSION'], '{date}': value['DATE']}
    for name, token in identity_bindings(config).items():
        if name in value and value[name] != tokens[token]:
            raise ValueError('native identity input differs from its declared build identity')
        value[name] = tokens[token]
    python_paths = [str(relative(root, package['target'])) for package in config.get('components', {}).get('python', [])]
    if python_paths:
        value['PYTHONPATH'] = os.pathsep.join([*python_paths, *([value['PYTHONPATH']] if value.get('PYTHONPATH') else [])])
    if config.get('components', {}).get('native'):
        value['SOURCE_COMMIT'] = source
        value.setdefault('RUST_TARGET', selected_target(package_data(root, config), value)['target'])
    return value


def binary_path(root, name):
    if os.name == 'nt' and not name.endswith('.exe'):
        name += '.exe'
    return relative(root, name)


def base_artifact_paths(root, config):
    """Canonical shipping outputs shared by build verification and native packets."""
    artifact_settings(config)
    paths = ['dist', '.artifacts/build', '.artifacts/native-package']
    declarations = config.get('components', {}).get('ui', [])
    if declarations:
        ui_component.records(declarations)
        paths.append('.artifacts/ui')
    paths += fixture_artifacts(root, config)
    for name in paths:
        relative(root, name)
    return paths


def verification_scope(root, config):
    paths = base_artifact_paths(root, config)
    if config.get('components', {}).get('native'):
        environment_values = environment(root, config)
        record = selected_target(package_data(root, config), environment_values)
        selected = native_components.selection(config, environment_values)
        native_paths = native_components.artifacts(root, config, record, environment_values)
        paths = [*paths, *native_paths] if any(value['role'] == 'cli' for value in selected) else native_paths
    for name in paths:
        relative(root, name)
    bindings = identity_bindings(config)
    return helper('verification').scope(artifacts=paths,
        environment=['VERSION', 'COMMIT', 'DATE', 'RUST_TARGET', *bindings,
                     *(['COOKBOOK_NATIVE_COMPONENTS', 'COOKBOOK_NATIVE_BUILD_CHECKS'] if config.get('components', {}).get('native') else [])],
        revision_environment=['COMMIT', *[name for name, token in bindings.items() if token == '{commit}']])


def fixture_artifacts(root, config):
    """Derive explicitly targeted Cargo fixture libraries from native manifests."""
    paths = []
    for component in components(config):
        target = component.get('target')
        if not target or component.get('build', {}).get('enabled') is False:
            continue
        rust_component.validate(component, root)
        manifest = tomllib.loads(relative(root, component.get('manifest', 'Cargo.toml'), exists=True).read_text())
        library = manifest.get('lib', {})
        if target.startswith('wasm32-') and 'cdylib' in library.get('crate-type', []):
            name = library.get('name', manifest.get('package', {}).get('name', '').replace('-', '_'))
            if not isinstance(name, str) or not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', name):
                raise ValueError('Cargo fixture library must name its native output')
            directory = Path(component.get('target_directory', os.environ.get('CARGO_TARGET_DIR', '.artifacts/rust-target')))
            if directory.is_absolute():
                try:
                    directory = directory.relative_to(root)
                except ValueError:
                    raise ValueError('Cargo fixture outputs must stay inside the checkout') from None
            profile = component.get('build', {}).get('profile', component.get('profile', 'release'))
            paths.append((directory / target / ('debug' if profile in {'debug', 'dev'} else 'release') / (name + '.wasm')).as_posix())
    return paths


def artifact_paths(root, config):
    return [*verification_scope(root, config)['artifacts'], *evidence_paths(root, config)]


def evidence_paths(root, config):
    return artifact_settings(config).get('evidence_paths', [])


def current_build(root, config):
    verify = helper('verification')
    declaration = verification_scope(root, config)
    record = json.loads((root / '.artifacts/verification/build.json').read_text())
    if record.get('identity') != verify.identity(declaration) or record.get('artifacts') != verify.artifacts(declaration):
        raise ValueError('build is missing, stale or modified; run mise run build before binary boundary tests')


def package_data(root, config):
    package = config.get('package', {})
    allowed = {'binary', 'version_arguments', 'native_targets', 'version_template', 'version_fields', 'files',
               'file_sources', 'extra_files', 'release_metadata', 'info_arguments', 'info_fields', 'info_exact'}
    required = {'binary', 'version_arguments', 'native_targets'}
    if not isinstance(package, dict) or set(package) - allowed or not required <= package.keys():
        raise ValueError('native CLI package data must declare executable, identity and complete target matrix')
    recipe_contract.validate_paths(config)
    components(config)
    manifest = tomllib.loads(relative(root, 'Cargo.toml', exists=True).read_text())
    if manifest.get('package', {}).get('name') != package['binary']:
        raise ValueError('native CLI binary must match the root Cargo package identity')
    if not package.get('version_template') and not package.get('version_fields'):
        raise ValueError('native package must declare plain or machine version/source identity')
    native = helper('native_cli')
    names = [native.archive_name(package, item) for item in package['native_targets']]
    if not names or len(names) != len(set(names)):
        raise ValueError('native target matrix must be nonempty and unique')
    package = {**package, 'output': '.artifacts/build/' + package['binary'], 'profile': 'release', 'component': 'cli'}
    for name in package.get('files', []):
        relative(root, package.get('file_sources', {}).get(name, name), exists=True)
    return package


def selected_target(package, environment):
    native = helper('native_cli')
    record = native.native_target(package)
    if environment.get('RUST_TARGET') and environment['RUST_TARGET'] != record['target']:
        raise ValueError('native CI target does not match the runner platform/architecture')
    return record


def native_receipt(package, record, environment, archive):
    return {'schema': 1, 'source': environment['COMMIT'], 'version': environment['VERSION'],
            'date': environment['DATE'], 'target': environment.get('RUST_TARGET', ''),
            'platform': record['platform'], 'arch': record['arch'], 'archive': archive.name,
            'sha256': helper('native_cli').digest(archive)}


def assemble(root, config, environment):
    package = package_data(root, config)
    native = helper('native_cli')
    output = root / 'dist'
    output.mkdir(exist_ok=True)
    staging = root / '.artifacts/native-package'
    staging.mkdir(parents=True, exist_ok=True)
    source = environment.get('NATIVE_CLI_ARCHIVES')
    if source and any(Path(source).resolve().is_relative_to(directory.resolve()) for directory in [output, staging]):
        raise ValueError('collected native inputs must be separate from generated destinations')
    # Remove only the generated identities declared by this package, so a
    # previous complete matrix cannot masquerade as the next local source build.
    for record in package['native_targets']:
        name = native.archive_name(package, record)
        for path in [output / name, staging / name, staging / (name + '.json')]:
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise ValueError('native generated output must be a regular file')
            path.unlink(missing_ok=True)
    if source:
        source = Path(source)
        expected = {native.archive_name(package, record) for record in package['native_targets']}
        actual = {path.name for path in source.iterdir()}
        if actual != expected | {name + '.json' for name in expected}:
            raise ValueError('native payload set is incomplete or contains unexpected entries')
        for record in package['native_targets']:
            name = native.archive_name(package, record)
            archive, receipt = source / name, source / (name + '.json')
            if receipt.is_symlink() or not receipt.is_file():
                raise ValueError('native source receipt must be a regular file')
            accepted = json.loads(receipt.read_text())
            desired = native_receipt(package, record, environment | {'RUST_TARGET': record['target']}, archive)
            if accepted != desired:
                raise ValueError('native archive source, target or digest differs from this gate')
            native.inspect_archive(archive, package, record)
            for directory in [output, staging]:
                shutil.copyfile(archive, directory / name)
            shutil.copyfile(receipt, staging / receipt.name)
    else:
        record = selected_target(package, environment)
        matches = [value for value in components(config) if value.get('name') == 'cli']
        if len(matches) != 1:
            raise ValueError('native package must select exactly one Cargo component')
        component = matches[0]
        target_root = Path(environment.get('CARGO_TARGET_DIR', 'target'))
        if component and component.get('target_directory'):
            target_root = Path(component['target_directory'])
        profile = package.get('profile', 'release')
        target = environment.get('RUST_TARGET', '')
        binary = root / target_root / target / ('debug' if profile in {'debug', 'dev'} else 'release') / native.executable(package, record)
        native.verify_identity(binary, package, record, environment)
        if target:
            native.verify_linkage(binary, record['platform'])
        archive = native.write_archive(root, binary, package, record, output)
        shutil.copyfile(archive, staging / archive.name)
        (staging / (archive.name + '.json')).write_text(json.dumps(native_receipt(package, record, environment, archive), sort_keys=True) + '\n')
    records = package['native_targets'] if source else [selected_target(package, environment)]
    names = [native.archive_name(package, record) for record in records]
    for name in package.get('extra_files', []):
        shutil.copyfile(relative(root, name, exists=True), output / name)
        names.append(name)
    (output / 'checksums.txt').write_text(native.checksums(output, names))
    if package.get('release_metadata'):
        (output / 'release.json').write_text(json.dumps({'revision': environment['COMMIT'], 'version': environment['VERSION']}) + '\n')
    record = selected_target(package, environment)
    with tempfile.TemporaryDirectory(prefix='native-output-') as directory:
        binary = native.inspect_archive(output / native.archive_name(package, record), package, record, directory)
        destination = binary_path(root, package['output'])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(binary, destination)
        destination.chmod(0o755)


def validate_package(root, config, environment):
    package, native = package_data(root, config), helper('native_cli')
    records = package['native_targets'] if environment.get('NATIVE_CLI_ARCHIVES') else [selected_target(package, environment)]
    expected = [native.archive_name(package, record) for record in records]
    actual = {path.name for path in (root / 'dist').iterdir() if path.name.endswith(('.tar.gz', '.zip'))}
    if actual != set(expected):
        raise ValueError('native distributable archive set is not the declared complete set')
    for record in records:
        native.inspect_archive(root / 'dist' / native.archive_name(package, record), package, record)
        name = native.archive_name(package, record)
        staged = root / '.artifacts/native-package' / name
        receipt = staged.with_name(staged.name + '.json')
        desired_env = environment | {'RUST_TARGET': record['target']} if environment.get('NATIVE_CLI_ARCHIVES') else environment
        if (json.loads(receipt.read_text()) != native_receipt(package, record, desired_env, staged)
                or native.digest(staged) != native.digest(root / 'dist' / name)):
            raise ValueError('staged native source receipt or bytes changed')
    if (root / 'dist/checksums.txt').read_text() != native.checksums(root / 'dist', [*expected, *package.get('extra_files', [])]):
        raise ValueError('native package checksums changed')
    if package.get('release_metadata') and json.loads((root / 'dist/release.json').read_text()) != {
            'revision': environment['COMMIT'], 'version': environment['VERSION']}:
        raise ValueError('release metadata belongs to another source or version')
    native.verify_identity(binary_path(root, package['output']), package, selected_target(package, environment), environment)


def product_tests(root, config, environment, suite, selected=None):
    entries = config.get('tests', {}).get(suite, [])
    if selected is not None:
        entries = [entry for entry in entries if entry['path'] in selected]
        if {entry['path'] for entry in entries} != set(selected):
            raise ValueError('selected native checks must be exact declared product test entries')
    if entries and suite in {'unit', 'integration', 'e2e'}:
        for fixture in config.get('components', {}).get('fixtures', []):
            helper('native_cli').immutable_fixture(root, fixture)
    arguments = {'{binary}': str(binary_path(root, package_data(root, config)['output'])), '{version}': environment['VERSION'],
                 '{root}': str(root), '{commit}': environment['COMMIT']}
    if config.get('components', {}).get('native'):
        record = selected_target(package_data(root, config), environment)
        arguments.update({'{native.' + value['name'] + '}': str(native_components.output(root, value, native_components.platform(record)))
                          for value in native_components.records(config)})
    if entries:
        from native_tests import run_entries
        selected = [{**entry, 'args': [arguments.get(value, value) for value in entry.get('args', [])]} for entry in entries]
        run_entries(root, {**config, 'tests': {suite: selected}}, environment, suite)
    return len(entries)


def setup(root, config, environment):
    stage = os.environ.get('COOKBOOK_STAGE', 'full')
    tools = required_tools(root, config, stage)
    run(['mise', 'install', *tools], root, environment)
    def pinned(argv, directory, values):
        run(argv, directory,
            {**values, 'MISE_AUTO_INSTALL': 'false', 'MISE_EXEC_AUTO_INSTALL': 'false'})
    if stage == 'fast':
        version = tomllib.loads((root / 'mise.toml').read_text())['tools']['rust']
        pinned(['rustup', 'component', 'add', '--toolchain', version, 'rustfmt'], root, environment)
    elif stage in {'full', 'native'}:
        rust_component.setup(root, components(config), environment, pinned)
        runtime = config.get('components', {}).get('runtime')
        if runtime:
            if not isinstance(runtime, dict) or set(runtime) != {'manifest'}:
                raise ValueError('runtime setup requires one caller-owned input manifest')
            relative(root, runtime['manifest'], exists=True)
            helper('runtime-inputs').prepare(root, runtime['manifest'])
        if config.get('components', {}).get('python'):
            pinned(['python3', str(COOKBOOK / 'cookbook/python_component.py'), 'setup'], root, environment)
        for fixture in config.get('components', {}).get('fixtures', []):
            helper('native_cli').immutable_fixture(root, fixture, install=True)
    if config.get('node', {}).get('packages'):
        node_component.install(root, config['node']['packages'], environment, pinned)
    browser = config.get('components', {}).get('browser')
    if stage == 'full' and browser and not os.environ.get('COOKBOOK_NATIVE_COMPONENTS'):
        if not isinstance(browser, dict) or set(browser)-{'package', 'cli_version'}:
            raise ValueError('unsupported native browser setup setting')
        helper('playwright-local').prepare(root, browser['package'], cli_version=browser.get('cli_version'),
            run=lambda argv, **kwargs: subprocess.run(argv, **kwargs))
    (root / '.artifacts/tmp').mkdir(parents=True, exist_ok=True)


def lint(root, config, environment):
    rust_component.lint(root, components(config), environment, run)
    if (root / '.github/workflows').is_dir():
        run(['actionlint'], root, environment)
    paths = config.get('lint', {}).get('shell_paths', [])
    if paths:
        run(['shellcheck', *[str(relative(root, name, exists=True)) for name in paths]], root, environment)
    run(sdk_environment.python_entry(str(TOOLS / 'helpers/documentation-policy.py')), root, environment)
    node_component.lint(root, config.get('node', {}).get('packages', []), environment, run)
    product_tests(root, config, environment, 'lint')


def security(root, config, environment):
    command = sdk_environment.python_entry(str(TOOLS / 'helpers/source-secrets.py'))
    if config.get('security', {}).get('fixtures'):
        command += ['--fixtures', str(relative(root, config['security']['fixtures'], exists=True))]
    run(command, root, environment)
    lockfiles = sorted({Path(value.get('manifest', 'Cargo.toml')).with_name('Cargo.lock').as_posix() for value in components(config)})
    for lock in lockfiles:
        run(['cargo', 'audit', '--file', str(relative(root, lock, exists=True)), '--db', '.artifacts/advisory-db',
             *(['--deny', 'warnings'] if config.get('security', {}).get('deny_warnings') else [])], root, environment)
    for path in config.get('security', {}).get('trivy_paths', []):
        run(['trivy', 'fs', '--scanners', 'vuln', '--severity', 'HIGH,CRITICAL', '--exit-code', '1',
             str(relative(root, path, exists=True))], root, environment)
    manifests = config.get('security', {}).get('rust_inventory_manifests', [])
    if manifests:
        known = {value.get('manifest', 'Cargo.toml') for value in components(config)}
        if not isinstance(manifests, list) or set(manifests)-known:
            raise ValueError('source dependency inventory must select owned Cargo components')
        import images
        images.rust_inventory(root, manifests, environment, run)
    node_component.audit(root, config.get('node', {}).get('packages', []), environment, run)
    product_tests(root, config, environment, 'security')


def build_checks(root, config, environment):
    entries = config.get('components', {}).get('build_checks', [])
    if not isinstance(entries, list) or len(entries) > 32:
        raise ValueError('build validation checks must be a bounded list')
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) - {'runner', 'path', 'args', 'timeout', 'cwd', 'pattern'}
                or entry.get('runner') not in {'python', 'python-unittest', 'bash', 'node', 'node-test', 'bun', 'bun-test'}
                or 'tests' not in Path(entry.get('path', '')).parts):
            raise ValueError('build checks must name real owned test assertion programs')
        relative(root, entry['path'], exists=True)
        if (not isinstance(entry.get('args', []), list)
                or any(not isinstance(item, str) or '\0' in item for item in entry.get('args', []))
                or type(entry.get('timeout', 300)) is not int or not 1 <= entry.get('timeout', 300) <= 3600):
            raise ValueError('build check arguments and deadlines must be bounded native data')
    if entries:
        from native_tests import run_entries
        run_entries(root, {'tests': {'build': entries}}, environment, 'build')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['setup', 'fmt', 'build', '_build', 'validate', 'test', '_test',
                                            'test:lint', 'test:unit', 'test:integration', 'test:e2e', 'test:security', 'clean'])
    parser.add_argument('--suite')
    args = parser.parse_args()
    root = Path.cwd().resolve()
    try:
        config = load(root)
        if config['recipe'] != 'rust-cli':
            raise ValueError('Rust CLI operations require the Rust CLI/TUI recipe')
        env = environment(root, config)
        if args.operation not in {'setup', 'clean'} and (root / 'mise.toml').is_file():
            pins = tomllib.loads((root / 'mise.toml').read_text()).get('tools', {})
            if pins.get('python') != '.'.join(map(str, sys.version_info[:3])):
                raise ValueError('use the exact caller-pinned Python SDK; run mise run setup first')
            for name in ['python', 'rust']:
                if name not in pins:
                    raise ValueError('declare the required SDK in mise.toml: ' + name)
                run(['mise', 'where', name], root, env)
        # Evidence hashes metadata values, never their serialized secret values.
        os.environ.update({key: env[key] for key in ['COMMIT', 'VERSION', 'DATE', 'RUST_TARGET', *identity_bindings(config)] if key in env})
        selected = os.environ.get('COOKBOOK_TEST_PATHS')
        if selected:
            aggregate = root / '.artifacts/verification/test.json'
            if aggregate.is_symlink():
                raise ValueError('test aggregate may not be a link')
            aggregate.unlink(missing_ok=True)
            if len(selected) > 65536:
                raise ValueError('native check selection exceeds its bound')
            selected = json.loads(selected)
            if (not isinstance(selected, list) or not selected or len(selected) > 128
                    or any(not isinstance(path, str) or len(path) > 4096 for path in selected)
                    or len(set(selected)) != len(selected)):
                raise ValueError('native check selection must be a nonempty unique JSON path array')
            if args.operation not in {'test:integration', '_test'} or (args.operation == '_test' and args.suite != 'integration'):
                raise ValueError('filtered native checks cannot claim an aggregate or another test group')
        selected_cargo = os.environ.get('COOKBOOK_NATIVE_TESTS')
        if selected_cargo:
            aggregate = root / '.artifacts/verification/test.json'
            if aggregate.is_symlink():
                raise ValueError('test aggregate may not be a link')
            aggregate.unlink(missing_ok=True)
            if len(selected_cargo) > 65536:
                raise ValueError('native Cargo selection exceeds its bound')
            selected_cargo = json.loads(selected_cargo)
            if (selected or not isinstance(selected_cargo, list) or not selected_cargo or len(selected_cargo) > 128
                    or any(not isinstance(name, str) for name in selected_cargo)
                    or len(selected_cargo) != len(set(selected_cargo))
                    or args.operation not in {'test:unit', '_test'}
                    or (args.operation == '_test' and args.suite != 'unit')):
                raise ValueError('explicit native Cargo selections cannot claim an aggregate or another test group')
        if args.operation == 'setup':
            setup(root, config, env)
        elif args.operation == 'fmt':
            rust_component.format(root, components(config), env, run)
            node_component.format(root, config.get('node', {}).get('packages', []), env, run)
        elif args.operation == 'clean':
            helper('clean').clean(root, config['clean']['paths'])
        elif args.operation == '_build':
            node_component.build(root, config.get('node', {}).get('packages', []), env, run)
            ui_component.validate_build(root, config.get('components', {}).get('ui', []),
                                        config.get('node', {}).get('packages', []), env, run)
            graph = config.get('components', {}).get('native')
            contexts = []
            record = selected_target(package_data(root, config), env) if graph else None
            if graph:
                if not env.get('NATIVE_CLI_ARCHIVES'):
                    contexts = native_components.build(root, config, record, env, run)
                else:
                    for value in native_components.selection(config, env):
                        native_components.dependencies(root, config, value, native_components.platform(record), env, materialize=True)
                native_components.verify(root, config, record, env)
            elif not env.get('NATIVE_CLI_ARCHIVES'):
                rust_component.build(root, components(config), env, run, target=env.get('RUST_TARGET'))
            if not graph or any(value['role'] == 'cli' for value in native_components.selection(config, env)):
                assemble(root, config, env)
                validate_package(root, config, env)
            if contexts:
                for component, values in contexts:
                    rust_component.validate_build(root, [component], values, run, target=env.get('RUST_TARGET'))
                source_components = [value for value in components(config) if value.get('build', {}).get('enabled') is False
                                     and value.get('lint', {}).get('enabled') is not False]
                if source_components:
                    rust_component.validate_build(root, source_components, env, run, target=env.get('RUST_TARGET'))
            else:
                rust_component.validate_build(root, components(config), env, run, target=env.get('RUST_TARGET'))
            if config.get('components', {}).get('rust_tests'):
                record = selected_target(package_data(root, config), env)
                names = [item['name'] for item in rust_tests.eligible(root, config, env, native_components.platform(record), 'build')]
                if names:
                    rust_tests.run_selected(root, config, env, record, names, run, 'build')
            build_checks(root, config, env)
        elif args.operation == 'validate':
            if config.get('components', {}).get('native'):
                record = selected_target(package_data(root, config), env)
                native_components.verify(root, config, record, env)
                if any(value['role'] == 'cli' for value in native_components.selection(config, env)):
                    validate_package(root, config, env)
            else:
                validate_package(root, config, env)
        else:
            verify = helper('verification')
            declaration = verification_scope(root, config)
            directory = Path('.artifacts/verification')
            if args.operation == 'build':
                verify.run_command('build', 'build', declaration, sdk_environment.python_entry(str(Path(__file__).resolve()), '_build'), directory)
            elif args.operation == 'test':
                suites = ['lint', 'unit', 'security', *[s for s in ['integration', 'e2e'] if s in config.get('tests', {})]]
                verify.run_session(suites, sdk_environment.python_entry(COOKBOOK / 'cookbook/dispatch.py', '_tests'), directory)
            elif args.operation == '_test':
                if args.suite == 'lint':
                    lint(root, config, env)
                elif args.suite == 'security':
                    security(root, config, env)
                elif args.suite == 'unit':
                    if selected_cargo:
                        rust_tests.run_selected(root, config, env, selected_target(package_data(root, config), env), selected_cargo, run)
                    else:
                        names = []
                        if config.get('components', {}).get('rust_tests'):
                            record = selected_target(package_data(root, config), env)
                            names = [item['name'] for item in rust_tests.eligible(root, config, env, native_components.platform(record))]
                        if any(value.get('test', {}).get('enabled') is not False for value in components(config)) or not names:
                            rust_component.test(root, components(config), env, run, target=env.get('RUST_TARGET'))
                        if config.get('components', {}).get('rust_tests'):
                            if names:
                                rust_tests.run_selected(root, config, env, record, names, run)
                        node_component.test(root, config.get('node', {}).get('packages', []), env, run)
                        product_tests(root, config, env, 'unit')
                elif not product_tests(root, config, env, args.suite, selected):
                    raise ValueError('native test group has no real declared product checks')
            else:
                suite = args.operation.split(':', 1)[1]
                if suite in {'integration', 'e2e'}:
                    current_build(root, config)
                    if config.get('components', {}).get('native') and not any(value['role'] == 'cli' for value in native_components.selection(config, env)) and not selected:
                        raise ValueError('a partial native role build may run only declared filtered boundary checks')
                if selected_cargo:
                    record = selected_target(package_data(root, config), env)
                    allowed = {item['name'] for item in rust_tests.eligible(root, config, env, native_components.platform(record))}
                    if not set(selected_cargo) <= allowed:
                        raise ValueError('native Cargo selection does not match this source/platform/role')
                    for name in selected_cargo:
                        proof_name = 'native-cargo-' + hashlib.sha256(name.encode()).hexdigest()[:24]
                        previous = os.environ.get('COOKBOOK_NATIVE_TESTS')
                        os.environ['COOKBOOK_NATIVE_TESTS'] = json.dumps([name])
                        try:
                            verify.run_command('test', proof_name, declaration,
                                sdk_environment.python_entry(str(Path(__file__).resolve()), '_test', '--suite', 'unit'), directory)
                        finally:
                            if previous is None:
                                os.environ.pop('COOKBOOK_NATIVE_TESTS', None)
                            else:
                                os.environ['COOKBOOK_NATIVE_TESTS'] = previous
                elif selected:
                    allowed = set(selected_target(package_data(root, config), env).get('checks', []))
                    if config.get('components', {}).get('native'):
                        allowed = {path for value in native_components.selection(config, env) for path in value.get('checks', [])}
                    if not set(selected) <= allowed:
                        raise ValueError('selected checks are not declared for this native target')
                    for path in selected:
                        name = 'native-' + hashlib.sha256(path.encode()).hexdigest()[:24]
                        command_env = {**os.environ, 'COOKBOOK_TEST_PATHS': json.dumps([path])}
                        previous = os.environ.get('COOKBOOK_TEST_PATHS')
                        os.environ['COOKBOOK_TEST_PATHS'] = command_env['COOKBOOK_TEST_PATHS']
                        try:
                            verify.run_command('test', name, declaration,
                                sdk_environment.python_entry(str(Path(__file__).resolve()), '_test', '--suite', suite), directory)
                        finally:
                            os.environ['COOKBOOK_TEST_PATHS'] = previous
                else:
                    verify.run_command('test', suite, declaration,
                        sdk_environment.python_entry(str(Path(__file__).resolve()), '_test', '--suite', suite), directory)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
