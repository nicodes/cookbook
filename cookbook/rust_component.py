"""Typed Cargo operations shared by recipes; caller data never supplies commands."""
from pathlib import Path
import os
import re
import tomllib

from configuration import relative

NAME = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.+/-]*\Z')
TARGET = re.compile(r'[a-z0-9_]+(?:-[a-z0-9_]+){1,}\Z')
BASE_FIELDS = {'name', 'manifest', 'binaries', 'packages', 'features', 'all_features',
               'no_default_features', 'workspace', 'all_targets', 'release',
               'targets', 'target', 'target_directory', 'profile', 'test', 'build', 'lint', 'environment',
               'toolchain_environment', 'build_std', 'cwd', 'timeout'}
SELECTORS = {'binaries', 'packages', 'features', 'all_features', 'no_default_features',
             'workspace', 'all_targets', 'release', 'profile', 'lib', 'tests', 'enabled',
             'test_targets', 'test_filter', 'test_arguments'}
SELECTORS.add('native_host')


def names(value, label):
    if not isinstance(value, list) or any(not isinstance(item, str) or not NAME.fullmatch(item) for item in value):
        raise ValueError(label + ' must be literal native Cargo identifiers')
    if len(set(value)) != len(value):
        raise ValueError(label + ' cannot contain duplicates')
    return value


def validate(component, root=None):
    if not isinstance(component, dict) or set(component) - BASE_FIELDS:
        raise ValueError('unsupported Cargo component data')
    manifest = component.get('manifest', 'Cargo.toml')
    if not isinstance(manifest, str) or not manifest.endswith('Cargo.toml'):
        raise ValueError('Cargo components require a native Cargo.toml manifest')
    if root is not None:
        relative(root, manifest, exists=True)
    elif Path(manifest).is_absolute() or '..' in Path(manifest).parts or '\\' in manifest:
        raise ValueError('Cargo manifest must stay inside the checkout')
    for declaration in [component, *[component.get(name, {}) for name in ['test', 'build', 'lint']]]:
        if not isinstance(declaration, dict):
            raise ValueError('Cargo selectors must be tables of native options')
        if declaration is not component and set(declaration) - SELECTORS:
            raise ValueError('unsupported Cargo operation selector')
        for field in ['binaries', 'packages', 'features', 'test_targets']:
            if field in declaration:
                names(declaration[field], field)
        for field in ['all_features', 'no_default_features', 'workspace', 'all_targets', 'release', 'lib', 'tests', 'enabled', 'native_host']:
            if field in declaration and type(declaration[field]) is not bool:
                raise ValueError('Cargo switch values must be booleans')
        if declaration is component.get('build') and 'native_host' in declaration:
            raise ValueError('native host selectors belong to written unit or compiler validation coverage')
        if declaration.get('profile', 'release') not in {'release', 'debug', 'dev'}:
            raise ValueError('Cargo profile must be release or native development/debug')
        if declaration.get('all_features') and declaration.get('features'):
            raise ValueError('select all features or explicit features, not both')
        if 'test_filter' in declaration and (not isinstance(declaration['test_filter'], str)
                or not re.fullmatch('[A-Za-z0-9_][A-Za-z0-9_:.+-]*', declaration['test_filter'])):
            raise ValueError('Cargo test filter must be a literal native test name')
        if 'test_arguments' in declaration:
            arguments = declaration['test_arguments']
            if not isinstance(arguments, list) or len(arguments) > 16:
                raise ValueError('Cargo test arguments must be bounded native harness options')
            for value in arguments:
                if value not in {'--exact', '--ignored', '--include-ignored', '--nocapture', '--show-output'} and not (
                        isinstance(value, str) and re.fullmatch(r'--test-threads=[1-9][0-9]{0,2}', value)):
                    raise ValueError('unsupported native Rust test-harness option')
    for target in component.get('targets', []):
        if not isinstance(target, str) or not TARGET.fullmatch(target):
            raise ValueError('invalid Rust SDK target')
    if component.get('target') and not TARGET.fullmatch(component['target']):
        raise ValueError('invalid Cargo component target')
    if component.get('target_directory'):
        directory = component['target_directory']
        if root is not None:
            relative(root, directory)
        elif not isinstance(directory, str) or Path(directory).is_absolute() or '..' in Path(directory).parts or '\\' in directory:
            raise ValueError('Cargo target directory must stay inside the checkout')
    if 'cwd' in component:
        if root is not None:
            if not relative(root, component['cwd'], exists=True).is_dir():
                raise ValueError('Cargo cwd must be a native source directory')
        elif not isinstance(component['cwd'], str) or Path(component['cwd']).is_absolute() or '..' in Path(component['cwd']).parts:
            raise ValueError('Cargo cwd must stay inside the checkout')
    if 'timeout' in component and (type(component['timeout']) is not int or not 1 <= component['timeout'] <= 3600):
        raise ValueError('Cargo timeouts must be bounded native data')
    if type(component.get('build_std', False)) is not bool:
        raise ValueError('Cargo build_std must be a boolean')
    if component.get('toolchain_environment') and (not isinstance(component['toolchain_environment'], str) or not re.fullmatch('[A-Z][A-Z0-9_]*', component['toolchain_environment'])):
        raise ValueError('nightly compiler must select a caller Mise environment pin')
    if component.get('build_std') and not component.get('toolchain_environment'):
        raise ValueError('building Rust std requires an explicitly pinned nightly SDK')
    environment = component.get('environment', {})
    if (not isinstance(environment, dict) or any(not re.fullmatch('[A-Z][A-Z0-9_]*', key)
            or not isinstance(value, str) or '\0' in value for key, value in environment.items())):
        raise ValueError('Cargo component environment must contain literal named values')
    return component


def cargo_args(component, operation, *, target=None, environment=None):
    validate(component)
    commands = {'fetch': 'fetch', 'fmt': 'fmt', 'fmt-check': 'fmt', 'lint': 'clippy', 'unit': 'test', 'build': 'build'}
    if operation not in commands:
        raise ValueError('unsupported native Cargo operation')
    values = component | component.get({'unit': 'test', 'fmt-check': 'lint'}.get(operation, operation), {})
    if operation in {'unit', 'lint'} and 'binaries' not in component.get('test' if operation == 'unit' else 'lint', {}):
        # A package's distributable executables do not restrict its library,
        # workspace or integration-test coverage.
        values = {key: value for key, value in values.items() if key != 'binaries'}
    argv = ['cargo', commands[operation]]
    if component.get('toolchain_environment') and operation not in {'fmt', 'fmt-check'}:
        pin = (environment or os.environ).get(component['toolchain_environment'])
        if not isinstance(pin, str) or not re.fullmatch(r'nightly-[0-9]{4}-[0-9]{2}-[0-9]{2}', pin):
            raise ValueError('native Cargo requires the exact caller Mise nightly pin')
        argv = ['rustup', 'run', pin, *argv]
    if operation not in {'fmt', 'fmt-check'}:
        argv.append('--locked')
        if operation != 'fetch':
            argv.append('--offline')
    if component.get('build_std') and operation not in {'fmt', 'fmt-check'}:
        argv.append('-Zbuild-std')
    argv += ['--manifest-path', component.get('manifest', 'Cargo.toml')]
    if operation in {'fmt', 'fmt-check'}:
        if values.get('workspace'):
            argv.append('--all')
        for package in values.get('packages', []):
            argv += ['--package', package]
        if operation == 'fmt-check':
            argv += ['--', '--check']
        return argv
    target = component.get('target', target)
    if operation in {'unit', 'lint'} and values.get('native_host'):
        target = None
    if target:
        if not TARGET.fullmatch(target):
            raise ValueError('Rust target must be a native target identifier')
        argv += ['--target', target]
    if operation == 'fetch':
        return argv
    if component.get('target_directory'):
        argv += ['--target-dir', component['target_directory']]
    if values.get('workspace'):
        argv.append('--workspace')
    if values.get('all_targets', operation == 'lint'):
        argv.append('--all-targets')
    for field, flag in [('packages', '--package'), ('binaries', '--bin')]:
        for value in values.get(field, []):
            argv += [flag, value]
    if values.get('all_features'):
        argv.append('--all-features')
    elif values.get('features'):
        argv += ['--features', ','.join(values['features'])]
    if values.get('no_default_features'):
        argv.append('--no-default-features')
    if operation == 'build':
        profile = values.get('profile', 'release' if values.get('release', True) else 'dev')
        if profile == 'release':
            argv.append('--release')
    if operation in {'unit', 'lint'} and values.get('lib'):
        argv.append('--lib')
    if operation == 'unit':
        if values.get('tests'):
            argv.append('--tests')
        for name in values.get('test_targets', []):
            argv += ['--test', name]
        if values.get('test_filter'):
            argv.append(values['test_filter'])
        if values.get('test_arguments'):
            argv += ['--', *values['test_arguments']]
    if operation == 'lint':
        argv += ['--', '-D', 'warnings']
    return argv


def component_environment(component, environment, root=None):
    values = {**environment, **component.get('environment', {}), 'RUSTUP_AUTO_INSTALL': '0'}
    if component.get('toolchain_environment') and root is not None:
        name = component['toolchain_environment']
        pin = tomllib.loads((Path(root) / 'mise.toml').read_text()).get('env', {}).get(name)
        if not isinstance(pin, str) or not re.fullmatch(r'nightly-[0-9]{4}-[0-9]{2}-[0-9]{2}', pin) or values.get(name, pin) != pin:
            raise ValueError('nightly SDK must match the exact caller Mise environment pin')
        values[name] = pin
    return values


def invoke(root, components, environment, operation, run, *, target=None):
    if not components:
        raise ValueError('a Rust operation requires at least one real Cargo component')
    count = 0
    selector = {'unit': 'test', 'fmt-check': 'lint'}.get(operation, operation)
    for component in components:
        validate(component, root)
        if component.get(selector, {}).get('enabled') is False:
            continue
        values = component_environment(component, environment, root)
        argv = cargo_args(component, operation, target=target, environment=values)
        cwd = relative(root, component.get('cwd', '.'), exists=True)
        if cwd != root:
            for flag in ['--manifest-path', '--target-dir']:
                if flag in argv:
                    index = argv.index(flag) + 1
                    argv[index] = str(relative(root, argv[index], exists=flag == '--manifest-path'))
            if values.get('CARGO_TARGET_DIR') and not Path(values['CARGO_TARGET_DIR']).is_absolute():
                values['CARGO_TARGET_DIR'] = str(relative(root, values['CARGO_TARGET_DIR']))
        if component.get('timeout') is not None and operation in {'build', 'unit', 'lint'}:
            run(argv, cwd, values, timeout=component['timeout'])
        else:
            run(argv, cwd, values)
        count += 1
    if not count:
        raise ValueError('the Cargo operation has no applicable native components')
    return count


def setup(root, components, environment, run):
    pins = tomllib.loads((Path(root) / 'mise.toml').read_text()).get('tools', {})
    version = pins.get('rust')
    if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Rust SDK must be exactly pinned in mise.toml')
    run(['rustup', 'component', 'add', '--toolchain', version, 'rustfmt', 'clippy'], root, environment)
    stable = [component for component in components if not component.get('toolchain_environment')]
    targets = {target for component in stable for target in validate(component, root).get('targets', [])}
    targets.update(component['target'] for component in stable if component.get('target'))
    if environment.get('RUST_TARGET'):
        if not TARGET.fullmatch(environment['RUST_TARGET']):
            raise ValueError('invalid native CI Rust target')
        targets.add(environment['RUST_TARGET'])
    if targets:
        run(['rustup', 'target', 'add', '--toolchain', version, *sorted(targets)], root, environment)
    installed = set()
    nightly_components = {}
    for component in components:
        if component.get('toolchain_environment'):
            values = component_environment(component, environment, root)
            key = values[component['toolchain_environment']]
            selected = nightly_components.setdefault(key, set())
            if component.get('build_std'):
                selected.add('rust-src')
            if component.get('lint', {}).get('enabled') is not False:
                selected.add('clippy')
    for component in components:
        validate(component, root)
        if not component.get('toolchain_environment'):
            continue
        values = component_environment(component, environment, root)
        nightly = values[component['toolchain_environment']]
        if nightly not in installed:
            command = ['rustup', 'toolchain', 'install', nightly, '--profile', 'minimal']
            for name in sorted(nightly_components[nightly]):
                command += ['--component', name]
            run(command, root, environment)
            installed.add(nightly)
        extra = set(component.get('targets', []))
        if component.get('target'):
            extra.add(component['target'])
        if extra and not component.get('build_std'):
            run(['rustup', 'target', 'add', '--toolchain', nightly, *sorted(extra)], root, environment)
    # A target-filtered fetch can omit dependencies selected by host-side build
    # scripts/proc macros (notably psm when targeting musl). Restore the complete
    # locked package closure here so every subsequent compiler command is offline.
    closure = [{key: value for key, value in component.items() if key not in {'target', 'build_std'}} for component in components]
    invoke(root, closure, environment, 'fetch', run)
    standard_library = [component for component in components if component.get('build_std')]
    if standard_library:
        invoke(root, standard_library, environment, 'fetch', run, target=environment.get('RUST_TARGET'))


def format(root, components, environment, run):
    invoke(root, components, environment, 'fmt', run)


def lint(root, components, environment, run, *, target=None):
    invoke(root, components, environment, 'fmt-check', run)


def validate_build(root, components, environment, run, *, target=None):
    """Compiler-backed validation runs after required product assets exist."""
    invoke(root, components, environment, 'lint', run, target=target)


def test(root, components, environment, run, *, target=None):
    invoke(root, components, environment, 'unit', run, target=target)


def build(root, components, environment, run, *, target=None):
    invoke(root, components, environment, 'build', run, target=target)
