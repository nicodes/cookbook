"""Image build and verification primitives shared by application recipes."""
import sdk_environment
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from configuration import relative
from recipe_contract import release_definition
import native_runtime
import deployment

import dependencies

TOOLS = dependencies.tools_root()
NAME = re.compile(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z')


def helper(name):
    return dependencies.recipe_helper(name)


def definition(root, config):
    path = relative(root, release_definition(config), exists=True)
    return helper('verification').release_definition(path)


def components(value):
    return deployment.image_components(value)


def references(value, revision):
    if not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('image builds require an exact source revision')
    return [value['image_base'] + '-' + name + ':' + revision for name in components(value)]


def validate_application(root, config, environment, run):
    deployment.declaration(root)


def stage_empty_directories(root, config):
    records = config.get('components', {}).get('empty_directories', [])
    if not isinstance(records, list):
        raise ValueError('empty runtime staging directories require typed native records')
    planned = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {'path', 'mode'}:
            raise ValueError('empty runtime staging requires an explicit path and mode')
        path = relative(root, record['path'])
        if not path.is_relative_to(root / '.artifacts') or path == root / '.artifacts':
            raise ValueError('runtime staging directories must stay in generated artifacts')
        if type(record['mode']) is not int or record['mode'] not in {0o700, 0o750, 0o755}:
            raise ValueError('runtime staging must use a reviewed directory permission mode')
        if path.exists() and (not path.is_dir() or any(item.name != '.keep' or item.is_symlink()
                or not item.is_file() or item.stat().st_size for item in path.iterdir())):
            raise ValueError('empty runtime staging may not contain retained or private data')
        planned.append((path, record['mode']))
    for path, mode in planned:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, mode)
        (path / '.keep').touch()


def build(root, config, environment, run, *, native=None):
    value = definition(root, config)
    revision = environment['APP_VERSION']
    references(value, revision)
    records = config.get('components', {}).get('images', [])
    if not isinstance(records, list):
        raise ValueError('image builds require typed Dockerfile component records')
    built, plans = [], []
    for record in records:
        if set(record) - {'name', 'dockerfile', 'context', 'target', 'cache_key', 'pull', 'build_arguments', 'native'}:
            raise ValueError('unknown image component setting')
        name = record.get('name')
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError('invalid image component name')
        if name in built:
            raise ValueError('duplicate image build component')
        if record.get('native') and native is None:
            raise ValueError('native image preparation has no owning recipe runner')
        if 'pull' in record and type(record['pull']) is not bool:
            raise ValueError('image pull policy must be boolean')
        dockerfile = relative(root, record.get('dockerfile', 'deploy/images/' + name + '.Dockerfile'), exists=True)
        context = relative(root, record.get('context', '.'), exists=True)
        command = ['build', '--format', 'oci', '--layers']
        if record.get('pull', True):
            command.append('--pull=always')
        target = record.get('target')
        if target:
            if not isinstance(target, str) or not NAME.fullmatch(target):
                raise ValueError('invalid native Dockerfile target')
            command += ['--target', target]
        arguments = record.get('build_arguments', {})
        if not isinstance(arguments, dict):
            raise ValueError('image build arguments must be a literal table')
        for key, argument in arguments.items():
            if not re.fullmatch('[A-Z][A-Z0-9_]*', key) or not isinstance(argument, str):
                raise ValueError('image build arguments must be literal named values')
            command += ['--build-arg', key + '=' + argument.replace('{revision}', revision)]
        local_base = None
        selected_base = arguments.get('BASE_IMAGE', '').replace('{revision}', revision)
        if record.get('pull') is False and selected_base.startswith(value['image_base'] + '-') and selected_base.endswith(':' + revision):
            if selected_base not in {value['image_base'] + '-' + component + ':' + revision for component in built}:
                raise ValueError('local native base must select an earlier same-revision image component')
            local_base = selected_base
        command += ['-f', str(dockerfile), '-t', value['image_base'] + '-' + name + ':' + revision, str(context)]
        cache = record.get('cache_key')
        if cache:
            if not isinstance(cache, str) or not NAME.fullmatch(cache):
                raise ValueError('image cache key must be a native component identifier')
            command = sdk_environment.python_entry(str(TOOLS / 'helpers/podman-build.py'), '--cache-key', cache, *(['--local-base', local_base] if local_base else []), '--', *command[4:])
        else:
            if local_base:
                command.append('--pull=never')
            command, _ = native_runtime.command(command, environment)
        plans.append((record, command, local_base))
        built.append(name)
    if not set(components(value)) <= set(built):
        raise ValueError('not every declared shipping image has a build component')
    for record, command, local_base in plans:
        if record.get('native'):
            native(root, record['native'], environment)
        if local_base:
            native_runtime.inspect([local_base], environment)
        values = native_runtime.environment(environment)
        run(command, root, values)


def package(root, config, environment, run):
    return deployment.package(root, config, environment, definition(root, config))


def validate_release(root, config, environment):
    return deployment.verify(root, environment)


def runtime_image(root, config, environment, name):
    """Select admitted bytes by config identity without importing a mutable name."""
    release = deployment.verify(root, environment)
    artifact = release['artifacts'].get(name)
    if artifact is not None:
        if artifact['kind'] != 'image':
            raise ValueError('runtime selection requires an accepted OCI image')
        return artifact['config_digest']
    import image_fixtures
    fixtures = image_fixtures.verify(root, config, environment)
    reference = definition(root, config)['image_base'] + '-' + name + ':' + environment['SOURCE_COMMIT']
    if reference not in fixtures:
        raise ValueError('runtime selection requires a declared accepted image component')
    return fixtures[reference]


def runtime_identities(root, config, environment):
    release = deployment.verify(root, environment)
    value = definition(root, config)
    source = release['source']['revision']
    accepted = {value['image_base'] + '-' + name + ':' + source: artifact['config_digest']
                for name, artifact in release['artifacts'].items() if artifact['kind'] == 'image'}
    import image_fixtures
    for reference, identity in image_fixtures.verify(root, config, environment).items():
        if reference in accepted:
            raise ValueError('fixture image identity collides with the accepted shipping scope')
        accepted[reference] = identity
    return accepted


def validate_static_assets(root, config, environment):
    import artifact_packet
    release = deployment.verify(root, environment)
    for name, record in deployment.assets(root, config).items():
        limits = {'max_bytes': record['max_bytes'], 'max_file_bytes': record['max_file_bytes'],
                  'max_files': record.get('max_files', artifact_packet.MAX_MEMBERS)}
        expected = artifact_packet.static_sources(relative(root, record['source'], exists=True), **limits)
        with tempfile.TemporaryDirectory(prefix='static-parity-') as scratch:
            target = Path(scratch) / 'public'
            artifact_packet.unpack_static(Path(root) / '.artifacts/release/static' / (name + '.tar.gz'), target, **limits)
            actual = artifact_packet.static_sources(target, **limits)
            # Static assets are data; native serving normalizes their file mode.
            # Membership and all payload bytes must remain exact.
            def content(base, records):
                return {key: artifact_packet.digest_file(base / key)[0]
                        for key, value in records.items()}
            if content(relative(root, record['source'], exists=True), expected) != content(target, actual):
                raise ValueError('accepted static archive does not preserve its complete source bytes')
    return release


def rust_inventory_manifests(root,config):
    components=config.get('components',{}).get('rust',[])
    policy=config.get('security',{})
    values=policy.get('rust_inventory_manifests',sorted({record['manifest'] for record in components}))
    if not components and 'rust_inventory_manifests' not in policy:return []
    if (not isinstance(values,list) or not 1<=len(values)<=64 or any(not isinstance(value,str) for value in values)
            or len(set(values))!=len(values)):
        raise ValueError('Rust security requires distinct bounded owning Cargo manifests')
    if type(config.get('security',{}).get('rust_inventory_sbom',False)) is not bool:
        raise ValueError('Rust inventory SBOM selection must be explicit')
    for name in values:
        manifest=relative(root,name,exists=True)
        if manifest.name!='Cargo.toml' or not manifest.is_file():raise ValueError('Rust inventory selects native Cargo manifests')
        lock=relative(root,str(Path(name).with_name('Cargo.lock')),exists=True)
        if not lock.is_file():raise ValueError('Rust inventories require their actual owning regular lockfiles')
    return list(values)


def rust_inventory(root, manifests, environment, run, *, sbom=False):
    if type(sbom) is not bool:raise ValueError('Rust inventory SBOM selection must be explicit')
    if not manifests or len(set(manifests)) != len(manifests):
        raise ValueError('Rust inventory scanning requires declared Cargo manifests')
    directory = relative(root, '.artifacts/security/rust-inputs')
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    for index, manifest in enumerate(manifests):
        source = relative(root, str(Path(manifest).with_name('Cargo.lock')), exists=True)
        target = directory / str(index)
        target.mkdir()
        shutil.copy2(source, target / 'Cargo.lock')
    report = root / '.artifacts/security/rust-source.json'
    run(['trivy', 'fs', '--quiet', '--scanners', 'vuln', '--pkg-types', 'library',
         '--include-dev-deps', '--list-all-pkgs', '--format', 'json', '--output', str(report), str(directory)],
        root, environment)
    if sbom:
        inventory=root/'.artifacts/security/rust-source-sbom.json'
        run(['trivy','fs','--quiet','--scanners','vuln','--pkg-types','library',
             '--include-dev-deps','--list-all-pkgs','--format','cyclonedx','--output',str(inventory),str(directory)],root,environment)
        bom=json.loads(inventory.read_text())
        if bom.get('bomFormat')!='CycloneDX' or not isinstance(bom.get('components'),list) or not bom['components']:
            raise ValueError('Rust dependency SBOM is missing its complete package inventory')
    value = json.loads(report.read_text())
    if value.get('SchemaVersion') != 2 or not isinstance(value.get('Results'), list):
        raise ValueError('unsupported Rust dependency report')
    inventories = [item for item in value['Results'] if item.get('Type') == 'cargo' and item.get('Packages')]
    targets = {Path(item.get('Target', '')).parts[-2:] for item in inventories}
    if len(inventories) != len(manifests) or targets != {(str(i), 'Cargo.lock') for i in range(len(manifests))}:
        raise ValueError('not every declared Rust dependency inventory was scanned')
    blocked = [v for item in inventories for v in item.get('Vulnerabilities', [])
               if v.get('Severity') in {'HIGH', 'CRITICAL'}]
    if blocked:
        raise ValueError('Rust dependency inventory has High/Critical findings; full report retained')


def rust_image(root, image, contract):
    if set(contract) - {'user', 'lock_source', 'lock_installed', 'binaries', 'forbidden_markers'}:
        raise ValueError('unknown native Rust image contract setting')
    inspected = native_runtime.inspect([image])
    if len(inspected) != 1 or not re.fullmatch('sha256:[a-f0-9]{64}', inspected[0].get('Id', '')):
        raise ValueError('Rust runtime requires an immutable image identity')
    identity = inspected[0]['Id']
    if inspected[0]['Config']['User'] != contract.get('user', '10001:10001'):
        raise ValueError('Rust runtime user differs from the declared contract')
    container = native_runtime.capture(['create', identity], text=True, timeout=30).strip()
    if not re.fullmatch('[a-f0-9]{64}', container):
        raise ValueError('Rust inspection container must have an immutable identity')
    try:
        with tempfile.TemporaryDirectory(prefix='rust-image-') as directory:
            directory = Path(directory)
            source = relative(root, contract.get('lock_source', 'api/Cargo.lock'), exists=True)
            installed = contract.get('lock_installed')
            if not isinstance(installed, str) or not installed.startswith('/') or '..' in Path(installed).parts:
                raise ValueError('Rust runtime inventory needs an absolute installed lock path')
            lock = directory / 'Cargo.lock'
            native_runtime.run(['cp', container + ':' + installed, str(lock)], check=True, timeout=30)
            if source.read_bytes() != lock.read_bytes():
                raise ValueError('runtime dependency inventory differs from the source lockfile')
            entries = contract.get('binaries', [])
            if not entries:
                raise ValueError('Rust image contracts require actual shipped binaries')
            for index, entry in enumerate(entries):
                if set(entry) != {'source', 'installed'}:
                    raise ValueError('Rust image binaries require source and installed paths')
                original = relative(root, entry['source'], exists=True)
                installed = entry['installed']
                if not isinstance(installed, str) or not installed.startswith('/') or '..' in Path(installed).parts:
                    raise ValueError('Rust runtime binaries need absolute installed paths')
                destination = directory / ('binary-' + str(index))
                native_runtime.run(['cp', container + ':' + installed, str(destination)], check=True, timeout=30)
                data = destination.read_bytes()
                if data != original.read_bytes() or not data.startswith(b'\x7fELF'):
                    raise ValueError('Rust runtime differs from the accepted native executable')
                if any(marker.encode() in data for marker in contract.get('forbidden_markers', [])):
                    raise ValueError('development authority entered the shipping Rust image')
    finally:
        native_runtime.run(['rm', '-v', container], check=True, stdout=subprocess.DEVNULL, timeout=30)


@contextlib.contextmanager
def browser_fixture(root, config, environment):
    settings = config.get('components', {}).get('workload_browser')
    if settings is None:
        yield environment
        return
    if not isinstance(settings, dict) or set(settings) != {'hostname'}:
        raise ValueError('workload browser fixture requires one admitted asset hostname')
    import workload_fixture
    hostname=None if environment.get('COOKBOOK_PREVIEW_NUMBER') else settings['hostname']
    with workload_fixture.context(root, config, environment, hostname=hostname) as fixture:
        yield fixture.environment


def validate_base_images(root, config, environment):
    value = definition(root, config)
    for record in config.get('components', {}).get('image_bases', []):
        if set(record) != {'component', 'reference'} or record['component'] not in components(value):
            raise ValueError('unchanged image bases require exact shipping workload components')
        reference = record['reference']
        if not deployment.IMAGE.fullmatch(reference):
            raise ValueError('unchanged image bases require complete immutable upstream references')
        built = runtime_image(root, config, environment, record['component'])
        native_runtime.run(['pull', reference], environment=environment, check=True, timeout=300)
        base, actual = native_runtime.inspect([reference, built], environment)
        if (base['RootFS'] != actual['RootFS'] or
                {key: item for key, item in base['Config'].items() if key != 'Labels'} !=
                {key: item for key, item in actual['Config'].items() if key != 'Labels'}):
            raise ValueError('source-labeled datastore image changed its trusted filesystem or runtime configuration')
