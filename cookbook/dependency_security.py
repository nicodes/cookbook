"""Optional independent source dependency audit; never build or accept an app."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

from configuration import load, relative
import images
import node_component

import dependencies as sdk_dependencies

COOKBOOK = Path(__file__).resolve().parents[1]
TOOLS = sdk_dependencies.tools_root()
RUNNERS = {'ubuntu-22.04', 'ubuntu-24.04', 'blacksmith-4vcpu-ubuntu-2204', 'blacksmith-4vcpu-ubuntu-2404'}
RECEIPT = Path('.artifacts/sdk/dependency-security.json')


def declaration(config):
    value = config.get('ci', {}).get('dependency_security')
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'runner', 'timeout_minutes', 'setup_timeout_minutes'}
            or not isinstance(value['runner'], str) or value['runner'] not in RUNNERS or type(value['timeout_minutes']) is not int
            or not 1 <= value['timeout_minutes'] <= 180 or type(value['setup_timeout_minutes']) is not int
            or not 1 <= value['setup_timeout_minutes'] <= value['timeout_minutes']):
        raise ValueError('dependency security requires literal native runner and exact bounded setup/job budgets')
    return dict(value)


def dependencies(root, config):
    if declaration(config) is None:
        raise ValueError('independent dependency security is not declared')
    manifests = images.rust_inventory_manifests(root, config)
    packages = []
    files = {'cookbook.toml', 'mise.toml'}
    tools = {'python'} | ({'trivy'} if manifests else set())
    for manifest in manifests:
        files.update([manifest, Path(manifest).with_name('Cargo.lock').as_posix()])
    for package in config.get('node', {}).get('packages', []):
        package, directory = node_component.validate(package, root)
        if not package.get('audit', True):
            continue
        manager = package.get('manager', 'npm')
        locks = ['bun.lock', 'bun.lockb'] if manager == 'bun' else ['package-lock.json', 'npm-shrinkwrap.json']
        present = [name for name in locks if (directory / name).exists()]
        if not present:
            raise ValueError('dependency audit requires an existing frozen native Node lock')
        files.update((directory / name).relative_to(root).as_posix() for name in ['package.json', *present])
        tools.add('bun' if manager == 'bun' else 'node')
        if (directory / 'security-remediations.toml').exists():
            tools.add('node')
            files.add((directory / 'security-remediations.toml').relative_to(root).as_posix())
        packages.append(package)
    if not manifests and not packages:
        raise ValueError('dependency security requires real declared dependency inventories')
    pins = tomllib.loads(relative(root, 'mise.toml', exists=True).read_text()).get('tools', {})
    if not tools <= pins.keys():
        raise ValueError('dependency security tools must retain exact caller Mise pins')
    spec = importlib.util.spec_from_file_location('dependency_security_select', TOOLS / 'helpers/select-tools.py')
    selector = importlib.util.module_from_spec(spec); spec.loader.exec_module(selector)
    selector.select(root, ' '.join(sorted(tools)))
    if any(not isinstance(pins[name], str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', pins[name]) for name in tools):
        raise ValueError('dependency audit SDK pins must be literal exact numeric x.y.z versions')
    identities = {}
    for name in sorted(files):
        path = relative(root, name, exists=True)
        if not path.is_file() or path.stat().st_size > 16 << 20:
            raise ValueError('dependency inputs must be bounded regular frozen source files')
        identities[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return packages, manifests, sorted(tools), identities


def identity(root, config):
    packages, manifests, tools, sources = dependencies(root, config)
    return {'sources': sources, 'policy': declaration(config), 'tools': tools,
            'packages': packages, 'manifests': manifests}


def write(value):
    spec = importlib.util.spec_from_file_location('dependency_security_writer', TOOLS / 'helpers/verification.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    module.write(RECEIPT, value)


def setup(root, config, environment, run):
    before = identity(root, config)
    run(['mise', 'install', *before['tools']], root, environment)
    # A dependency audit must never trigger native application lifecycle builds.
    node_component.install(root, [{**value, 'ignore_scripts': True} for value in before['packages']], environment, run)
    if identity(root, config) != before:
        raise ValueError('dependency preparation changed frozen source inputs')
    if Path.cwd().resolve() != root.resolve():
        raise ValueError('dependency preparation requires its actual owning checkout')
    write(before)


def audit(root, config, environment, run):
    expected = identity(root, config)
    path = relative(root, RECEIPT.as_posix(), exists=True)
    if not path.is_file() or path.stat().st_size > 1 << 20 or json.loads(path.read_text()) != expected:
        raise ValueError('dependency inputs changed or were not prepared by setup')
    node_component.audit(root, expected['packages'], environment, run)
    if expected['manifests']:
        images.rust_inventory(root, expected['manifests'], environment, run,
            sbom=config.get('security', {}).get('rust_inventory_sbom', False))
    if identity(root, config) != expected:
        raise ValueError('dependency auditing changed frozen source inputs')


def admit(root, config, source, tools, runner, timeout, setup_timeout):
    if (not re.fullmatch('[a-f0-9]{40}', source) or not re.fullmatch('[a-f0-9]{40}', tools)
            or subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip() != source
            or subprocess.check_output(['git', '-C', str(COOKBOOK), 'rev-parse', 'HEAD'], text=True).strip() != tools
            or subprocess.run(['git', '-C', str(COOKBOOK), 'diff', '--quiet', 'HEAD', '--']).returncode
            or subprocess.run(['git', '-C', str(root), 'diff', '--quiet', 'HEAD', '--']).returncode):
        raise ValueError('dependency security requires exact clean source and SDK checkouts')
    sdk_dependencies.sdk_source.verify(COOKBOOK, tools, repository='nicodes/cookbook')
    sdk_dependencies.tools_root()
    import generate
    generate.check_entrypoint(root, tools)
    if declaration(config) != {'runner': runner, 'timeout_minutes': timeout, 'setup_timeout_minutes': setup_timeout}:
        raise ValueError('dependency security caller changed the native runner or deadlines')
    _, _, _, inputs = dependencies(root, config)
    subprocess.run(['git', '-C', str(root), 'ls-files', '--error-unmatch', '--', *inputs],
                   check=True, stdout=subprocess.DEVNULL)


def job(root, config, revision):
    value = declaration(config)
    if value is None:
        return ''
    dependencies(root, config)
    return ('  dependency-security:\n    name: Dependency security\n'
        '    uses: nicodes/cookbook/.github/workflows/cookbook-dependency-security.yml@' + revision + '\n'
        '    permissions:\n      contents: read\n      attestations: read\n    with:\n'
        "      cookbook-ref: ${{ inputs.cookbook || '" + revision + "' }}\n"
        "      source: ${{ inputs.source || github.sha }}\n"
        '      runner: ' + value['runner'] + '\n      timeout-minutes: ' + str(value['timeout_minutes']) + '\n'
        '      setup-timeout-minutes: ' + str(value['setup_timeout_minutes']) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['admit', 'setup', 'audit'])
    parser.add_argument('--source', default=''); parser.add_argument('--cookbook-ref', default='')
    parser.add_argument('--runner', default=''); parser.add_argument('--timeout', type=int, default=0)
    parser.add_argument('--setup-timeout', type=int, default=0)
    args = parser.parse_args(); root = Path.cwd(); config = load(root)
    if args.operation == 'admit':
        admit(root, config, args.source, args.cookbook_ref, args.runner, args.timeout, args.setup_timeout)
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                output.write('tools=' + ' '.join(dependencies(root, config)[2]) + '\n')
    else:
        environment = dict(os.environ, CI='1', MISE_AUTO_INSTALL='false', MISE_EXEC_AUTO_INSTALL='false')
        def run(command, directory, values):
            # Resolve the already installed caller pins for each native audit.
            # Automatic installation is disabled in both setup and audit.
            selected = command if command[:2] == ['mise', 'install'] else ['mise', 'exec', '--', *command]
            subprocess.run(selected, cwd=directory, env=values, check=True)
        globals()[args.operation](root, config, environment, run)


if __name__ == '__main__':
    try: main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
