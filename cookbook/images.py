"""Image build and verification primitives shared by application recipes."""
import sdk_environment
import gzip
import contextlib
import copy
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
import time
import urllib.error
import urllib.request

from configuration import relative
from recipe_contract import compose_path, release_definition
import buildkit

import dependencies

COOKBOOK = Path(__file__).resolve().parents[1]
TOOLS = dependencies.tools_root()
NAME = re.compile(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z')


def helper(name):
    return dependencies.recipe_helper(name)


def definition(root, config):
    path = relative(root, release_definition(config), exists=True)
    return helper('verification').release_definition(path)


def components(value):
    base = value['image_base']
    result = []
    for image in value['inputs']['images']:
        prefix, suffix = base + '-', ':{revision}'
        if not image.startswith(prefix) or not image.endswith(suffix):
            raise ValueError('application images must use the exact release image base and source revision')
        name = image[len(prefix):-len(suffix)]
        if not NAME.fullmatch(name) or name in result:
            raise ValueError('release image component names must be distinct native identifiers')
        result.append(name)
    if not result:
        raise ValueError('image application requires declared release images')
    return result


def references(value, revision):
    if not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('image builds require an exact source revision')
    return [value['image_base'] + '-' + name + ':' + revision for name in components(value)]


@contextlib.contextmanager
def compose_validation(root, config, environment):
    settings = config.get('components', {}).get('compose', {})
    source = relative(root, compose_path(config), exists=True)
    if settings:
        raise ValueError('Compose validation uses the process environment; remove environment_files')
    with tempfile.TemporaryDirectory(prefix='compose-validation-') as directory:
        target = Path(directory)
        shutil.copy2(source, target / 'compose.yml')
        command = ['docker', 'compose', '--env-file', os.devnull,
                   '--project-directory', str(target), '-p', 'engineering-validation',
                   '-f', str(target / 'compose.yml'), 'config', '--no-env-resolution']
        # Compose 2.x resolves required service env files while constructing its
        # project, before --no-env-resolution is applied. Its native model path
        # preserves the declarations without reading their contents.
        original = json.loads(subprocess.check_output(
            [*command, '--no-interpolate', '--no-path-resolution', '--format', 'json'],
            cwd=root, env=environment, text=True, timeout=30))
        if not isinstance(original, dict) or not isinstance(original.get('services'), dict):
            raise ValueError('Compose validation requires a native service model')
        model = copy.deepcopy(original)
        references = {}
        for name, service in model['services'].items():
            records = service.get('env_file', [])
            if not isinstance(records, list):
                raise ValueError('Compose environment files require native path records')
            if records:
                references[name] = copy.deepcopy(records)
            for record in records:
                if not isinstance(record, dict) or not isinstance(record.get('path'), str):
                    raise ValueError('Compose environment files require literal relative paths')
                path = record['path']
                if '$' in path:
                    raise ValueError('Compose environment files require literal relative paths')
                relative(source.parent, path)
                destination = relative(target, path)
                if destination.exists() or destination.is_symlink():
                    raise ValueError('Compose validation may not read environment file contents')
                record['required'] = False
        # This is the only generated input, never an env/credential file. The
        # second native pass retains interpolation, schema and consistency checks.
        (target / 'compose.yml').write_text(json.dumps(model))
        yield command, references


@contextlib.contextmanager
def compose_command(root, config, environment=None):
    with compose_validation(root, config, dict(os.environ) if environment is None else environment) as (command, _):
        yield command


def validate_compose(root, config, environment, run):
    with compose_command(root, config, environment) as command:
        run([*command, '--quiet'], root, environment)


def compose_model(root, config, environment):
    """Read the deployment model without reading or writing checkout credentials."""
    with compose_validation(root, config, environment) as (command, references):
        model = json.loads(subprocess.check_output([*command, '--format', 'json'], cwd=root,
                                                  env=environment, text=True, timeout=30))
        for name, records in references.items():
            model['services'][name]['env_file'] = records
        return model


def stage_caddy(root, *, host=False):
    directory = relative(root, '.artifacts/caddy-build')
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    names = {'caddy.go.mod': 'go.mod', 'caddy.go.sum': 'go.sum', 'caddy-build.sh': 'build.sh'} if host else {
        name: name for name in ['caddy.go.mod', 'caddy.go.sum', 'caddy-build.sh']}
    for name, destination in names.items():
        shutil.copy2(TOOLS / 'helpers' / name, directory / destination)
    return directory


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


def host_caddy(root, environment, run):
    expected = tomllib.loads((root / 'mise.toml').read_text())['tools']['go']
    actual = subprocess.check_output(['go', 'env', 'GOVERSION'], cwd=root, env=environment, text=True).strip()
    if actual != 'go' + expected:
        raise ValueError('Caddy requires the exact caller-pinned Go SDK')
    platform = environment.get('DOCKER_DEFAULT_PLATFORM') or subprocess.check_output(
        ['docker', 'version', '--format', '{{.Server.Os}}/{{.Server.Arch}}'],
        cwd=root, env=environment, text=True, timeout=30).strip()
    if platform not in {'linux/amd64', 'linux/arm64'}:
        raise ValueError('unsupported Caddy runtime platform')
    directory = stage_caddy(root, host=True)
    values = environment | {'CGO_ENABLED': '0', 'GOOS': 'linux', 'GOARCH': platform.split('/')[1],
                            'GOTOOLCHAIN': 'local', 'GOFLAGS': '-buildvcs=false'}
    run(['sh', 'build.sh'], directory, values)
    # The previous compiler-image cache is obsolete for host-compiled Caddy.
    # Use the same component lock as docker-build.py and retain every other cache.
    if environment.get('FLEET_BUILDKIT_CACHE'):
        import fcntl
        cache_root = Path(environment['FLEET_BUILDKIT_CACHE'])
        cache, lock = cache_root / 'gate-caddy', cache_root / 'gate-caddy.lock'
        if any(path.is_symlink() for path in [cache_root, cache, lock]):
            raise ValueError('compiler layer cache must not be a symlink')
        if cache.exists():
            with lock.open('a') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                if cache.exists():
                    shutil.rmtree(cache)


def build(root, config, environment, run, *, native=None):
    value = definition(root, config)
    revision = environment['APP_VERSION']
    references(value, revision)
    records = config.get('components', {}).get('images', [])
    if not isinstance(records, list) or not records:
        raise ValueError('image builds require typed Dockerfile component records')
    built, plans = [], []
    for record in records:
        if set(record) - {'name', 'dockerfile', 'context', 'target', 'cache_key', 'pull', 'build_arguments', 'native', 'release_cache_before', 'release_cache_after'}:
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
        if any(key in record and type(record[key]) is not bool for key in ['release_cache_before', 'release_cache_after']):
            raise ValueError('image compiler-store release policy must be boolean')
        dockerfile = relative(root, record.get('dockerfile', 'deploy/images/' + name + '.Dockerfile'), exists=True)
        context = relative(root, record.get('context', '.'), exists=True)
        command = ['docker', 'buildx', 'build', '--load']
        if record.get('pull', True):
            command.append('--pull')
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
            command = sdk_environment.python_entry(str(TOOLS / 'helpers/docker-build.py'), '--cache-key', cache, *(['--local-base', local_base] if local_base else []), '--', *command[4:])
        elif local_base:
            command[3:3] = ['--builder', 'default']
        plans.append((record, command, local_base))
        built.append(name)
    if not set(components(value)) <= set(built):
        raise ValueError('not every declared shipping image has a build component')
    for record, command, local_base in plans:
        if record.get('release_cache_before'):
            buildkit.release_cache(root, environment, run)
        if record.get('native'):
            native(root, record['native'], environment)
        if local_base:
            identity = subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Id}}', local_base],
                cwd=root, env=environment, text=True, timeout=60).strip()
            if not re.fullmatch('sha256:[a-f0-9]{64}', identity):
                raise ValueError('local native base requires its actual immutable Docker image identity')
        run(command, root, environment)
        if record.get('release_cache_after'):
            buildkit.release_cache(root, environment, run)


def package(root, config, environment, run):
    value = definition(root, config)
    refs = references(value, environment['APP_VERSION'])
    directory = relative(root, '.artifacts/release')
    directory.mkdir(parents=True, exist_ok=True)
    inspected = subprocess.check_output(['docker', 'image', 'inspect', *refs], cwd=root, env=environment, timeout=60)
    (directory / 'images.json').write_bytes(inspected)
    archive = directory / 'images.tar.gz'
    with tempfile.TemporaryFile() as errors, archive.open('wb') as output:
        process = subprocess.Popen(['docker', 'save', *refs], cwd=root, env=environment,
                                   stdout=subprocess.PIPE, stderr=errors)
        try:
            with gzip.GzipFile(fileobj=output, mode='wb', compresslevel=1, mtime=0) as compressor:
                shutil.copyfileobj(process.stdout, compressor)
        except BaseException:
            process.kill()
            process.wait()
            archive.unlink(missing_ok=True)
            raise
        finally:
            process.stdout.close()
        if process.wait() != 0:
            archive.unlink(missing_ok=True)
            raise ValueError('Docker could not save the complete image set')
    with archive.open('rb') as incoming:
        digest = hashlib.file_digest(incoming, 'sha256').hexdigest()
    archive.with_suffix(archive.suffix + '.sha256').write_text(digest + '  images.tar.gz\n')
    command = sdk_environment.python_entry(str(TOOLS / 'helpers/release.py'), 'record', '--project', value['project'], '--image-base', value['image_base'], '--revision', environment['APP_VERSION'], '--components', *components(value))
    if value.get('stateful_contract'):
        command += ['--stateful-contract', value['stateful_contract']]
    run(command, root, environment)


def validate_release(root, config, environment):
    value = definition(root, config)
    release = helper('release')
    manifest = json.loads((root / '.artifacts/release/release.json').read_text())
    refs = release.validate(manifest, value['project'], environment['APP_VERSION'], components(value),
                            root / '.artifacts/release/images.tar.gz', value['image_base'])
    if release.inspect(refs) != manifest['images']:
        raise ValueError('local image identities changed after release packaging')


def static_image(image, source, installed='/srv/public'):
    def manifest(directory):
        values = {}
        for path in Path(directory).rglob('*'):
            if path.is_symlink():
                raise ValueError('static image inputs and installed bytes must not contain links')
            if path.is_file():
                with path.open('rb') as incoming:
                    values[path.relative_to(directory).as_posix()] = hashlib.file_digest(incoming, 'sha256').hexdigest()
        return values
    expected = manifest(source)
    if not expected:
        raise ValueError('static image source must contain actual output files')
    container = subprocess.check_output(['docker', 'create', image], text=True, timeout=30).strip()
    if not re.fullmatch('[a-f0-9]{64}', container):
        raise ValueError('an immutable temporary container identity is required')
    try:
        with tempfile.TemporaryDirectory(prefix='static-image-') as directory:
            subprocess.run(['docker', 'cp', container + ':' + installed + '/.', directory], check=True, timeout=60)
            actual = manifest(Path(directory))
            if expected != actual:
                missing = sorted(set(expected)-set(actual))
                extra = sorted(set(actual)-set(expected))
                changed = sorted(name for name in set(expected)&set(actual) if expected[name]!=actual[name])
                details = {'missing_count':len(missing),'extra_count':len(extra),'changed_count':len(changed),
                    'missing':[name[:240] for name in missing[:10]],'extra':[name[:240] for name in extra[:10]],
                    'changed':[{'path':name[:240],'source_sha256':expected[name],'installed_sha256':actual[name]}
                               for name in changed[:10]]}
                raise ValueError('static image does not preserve its complete source bytes: '+json.dumps(details,sort_keys=True))
    finally:
        subprocess.run(['docker', 'rm', '-v', container], check=True, stdout=subprocess.DEVNULL, timeout=30)
    return expected


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
    inspected = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image], text=True, timeout=30))
    if len(inspected) != 1 or not re.fullmatch('sha256:[a-f0-9]{64}', inspected[0].get('Id', '')):
        raise ValueError('Rust runtime requires an immutable image identity')
    identity = inspected[0]['Id']
    if inspected[0]['Config']['User'] != contract.get('user', '10001:10001'):
        raise ValueError('Rust runtime user differs from the declared contract')
    container = subprocess.check_output(['docker', 'create', identity], text=True, timeout=30).strip()
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
            subprocess.run(['docker', 'cp', container + ':' + installed, str(lock)], check=True, timeout=30)
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
                subprocess.run(['docker', 'cp', container + ':' + installed, str(destination)], check=True, timeout=30)
                data = destination.read_bytes()
                if data != original.read_bytes() or not data.startswith(b'\x7fELF'):
                    raise ValueError('Rust runtime differs from the accepted native executable')
                if any(marker.encode() in data for marker in contract.get('forbidden_markers', [])):
                    raise ValueError('development authority entered the shipping Rust image')
    finally:
        subprocess.run(['docker', 'rm', '-v', container], check=True, stdout=subprocess.DEVNULL, timeout=30)


@contextlib.contextmanager
def gate_fixture(root, config, environment):
    settings = config.get('components', {}).get('browser_gate')
    if not settings:
        yield environment
        return
    if set(settings) - {'hostname', 'component', 'user'}:
        raise ValueError('unknown packaged gate browser fixture setting')
    hostname = settings.get('hostname')
    if not isinstance(hostname, str) or not re.fullmatch('[a-z0-9][a-z0-9.-]*', hostname):
        raise ValueError('packaged gate browser hostname must be a literal host')
    value = definition(root, config)
    name = settings.get('component', 'gate')
    if name not in components(value):
        raise ValueError('browser fixture must select a declared shipping image')
    reference = value['image_base'] + '-' + name + ':' + environment['APP_VERSION']
    receipt = json.loads((root / '.artifacts/verification/build.json').read_text())
    image = receipt.get('artifacts', {}).get('image:' + reference, '')
    if not re.fullmatch('sha256:[a-f0-9]{64}', image):
        raise ValueError('browser fixture needs the accepted immutable shipping image')
    user = settings.get('user', '65534:65534')
    if not isinstance(user, str) or not re.fullmatch('[0-9]+:[0-9]+', user):
        raise ValueError('browser fixture runtime identity must be an explicit numeric UID/GID')
    uid, gid = user.split(':')
    output = relative(root, '.artifacts/e2e')
    output.mkdir(parents=True, exist_ok=True)
    container = None
    with tempfile.TemporaryDirectory(prefix='gate-fixture-') as directory:
        cid = Path(directory) / 'container'
        command = ['docker', 'run', '-d', '--cidfile', str(cid), '--read-only', '--cap-drop', 'ALL',
                   '--security-opt', 'no-new-privileges', '--user', user,
                   '--sysctl', 'net.ipv4.ip_unprivileged_port_start=0']
        for target in ['/tmp', '/data', '/config']:
            command += ['--tmpfs', target + ':rw,noexec,nosuid,size=16m,uid=' + uid + ',gid=' + gid]
        command += ['--log-opt', 'max-size=10m', '--log-opt', 'max-file=3', '-p', '127.0.0.1::80', image]
        try:
            subprocess.run(command, cwd=root, env=environment, check=True, capture_output=True, text=True, timeout=60)
            container = cid.read_text().strip()
            if not re.fullmatch('[a-f0-9]{64}', container):
                raise ValueError('browser fixture requires its exact owned container identity')
            port = subprocess.check_output(['docker', 'port', container, '80/tcp'], text=True, timeout=30).strip().rsplit(':', 1)[-1]
            if not re.fullmatch('[0-9]+', port) or not 1 <= int(port) <= 65535:
                raise ValueError('browser fixture did not publish an owned loopback port')
            request = urllib.request.Request('http://127.0.0.1:' + port + '/', headers={'Host': hostname + ':' + port})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            deadline = time.monotonic() + 60
            while True:
                try:
                    with opener.open(request, timeout=5) as response:
                        if response.status == 200:
                            break
                except (OSError, urllib.error.URLError):
                    pass
                if time.monotonic() >= deadline:
                    raise ValueError('packaged gate browser fixture did not become ready')
                time.sleep(1)
            yield environment | {'E2E_URL': 'http://' + hostname + ':' + port + '/'}
        finally:
            if container is None and cid.is_file():
                selected = cid.read_text().strip()
                if re.fullmatch('[a-f0-9]{64}', selected):
                    container = selected
            if container:
                try:
                    logs = subprocess.run(['docker', 'logs', '--tail', '100', container], capture_output=True, text=True, timeout=30)
                    (output / 'gate.log').write_text((logs.stdout + logs.stderr)[-200000:])
                finally:
                    subprocess.run(['docker', 'rm', '-f', container], check=True, stdout=subprocess.DEVNULL, timeout=30)
