"""Build one immutable workload descriptor and its exact native artifact packet."""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib

import artifact_packet as packet
from configuration import relative
import dependencies

NAME = re.compile(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z')
DIGEST = re.compile(r'sha256:[a-f0-9]{64}\Z')
IMAGE = re.compile(r'[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9][a-z0-9/._-]*@sha256:[a-f0-9]{64}\Z')
STATIC_MEDIA = 'application/vnd.komizo.static.v1.tar+gzip'
CONTAINER_FIELDS = {'artifact', 'command', 'entrypoint', 'user', 'environment', 'environment_files',
    'secrets', 'volumes', 'tmpfs', 'networks', 'limits', 'health', 'needs', 'read_only',
    'capabilities', 'init', 'working_directory', 'stop_timeout_seconds', 'ulimits', 'sysctls'}
WORKLOAD_FIELDS = {'services', 'jobs', 'assets', 'networks', 'volumes', 'routes', 'readiness'}
PROVIDER_FIELDS = {'cookbook_revision', 'tools_revision', 'core_revision'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode()


def closed(value, fields, label, required=()):
    if not isinstance(value, dict) or set(value) - set(fields) or not set(required) <= set(value):
        raise ValueError(label + ' has missing or unknown fields')


def validate_providers(value):
    closed(value, PROVIDER_FIELDS, 'release providers', PROVIDER_FIELDS)
    if any(not isinstance(revision, str) or not re.fullmatch('[a-f0-9]{40}', revision)
           for revision in value.values()):
        raise ValueError('release providers require exact immutable source revisions')
    return value


def provider_provenance():
    import komizo_core
    import sdk_source
    root = Path(__file__).resolve().parents[1]
    revision = sdk_source.git(root, 'rev-parse', 'HEAD')
    sdk_source.verify(root, revision, repository='nicodes/cookbook')
    dependencies.tools_root()
    return validate_providers({'cookbook_revision': revision,
        'tools_revision': dependencies.tools_identity()['revision'],
        'core_revision': komizo_core.pin()['revision']})


def static_manifest(digest, size):
    if not DIGEST.fullmatch(digest) or type(size) is not int or size <= 0:
        raise ValueError('static OCI artifact requires its exact nonempty payload identity')
    return canonical({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
        'artifactType': 'application/vnd.komizo.static.v1',
        'config': {'mediaType': 'application/vnd.oci.empty.v1+json',
                   'digest': 'sha256:' + hashlib.sha256(b'{}').hexdigest(), 'size': 2},
        'layers': [{'mediaType': STATIC_MEDIA, 'digest': digest, 'size': size}]})


def declaration(root):
    path = relative(root, 'deploy/application.toml', exists=True)
    value = tomllib.loads(packet.read_regular(path, 1 << 20).decode())
    closed(value, {'kind', 'version', 'application', 'workload'}, 'application',
           {'kind', 'version', 'application', 'workload'})
    if (value['kind'] != 'komizo.application' or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['application'], str) or not NAME.fullmatch(value['application'])):
        raise ValueError('application requires its native kind, version and identifier')
    validate_workload(value['workload'])
    return value


def validate_workload(value):
    closed(value, WORKLOAD_FIELDS, 'workload', WORKLOAD_FIELDS)
    logical = set()
    for kind in ['services', 'jobs', 'assets', 'networks', 'volumes']:
        records = value[kind]
        if not isinstance(records, dict) or len(records) > 64:
            raise ValueError('workload collections must be bounded logical maps')
        for name, record in records.items():
            if not isinstance(name, str) or not NAME.fullmatch(name):
                raise ValueError('workload names must be native identifiers')
            if kind in {'services', 'jobs'}:
                if name in logical:
                    raise ValueError('workload services and jobs need distinct identities')
                logical.add(name)
                closed(record, CONTAINER_FIELDS | ({'timeout_seconds'} if kind == 'jobs' else set()),
                       'container', {'artifact'})
                if kind == 'jobs' and (type(record.get('timeout_seconds')) is not int
                        or not 1 <= record['timeout_seconds'] <= 3600):
                    raise ValueError('jobs require an explicit bounded deadline')
                for field in ['command', 'entrypoint', 'environment_files', 'networks', 'capabilities']:
                    if field in record and (not isinstance(record[field], list)
                            or any(not isinstance(item, str) or '\0' in item for item in record[field])):
                        raise ValueError('container argument and reference lists must contain literal strings')
                if record.get('read_only', True) is not True:
                    raise ValueError('application root filesystems must be read only')
                if any(network not in value['networks'] for network in record.get('networks', [])):
                    raise ValueError('container selects an undeclared network')
                for volume in record.get('volumes', []):
                    closed(volume, {'name', 'target', 'read_only'}, 'container volume', {'name', 'target', 'read_only'})
                    if volume['name'] not in value['volumes']:
                        raise ValueError('container selects an undeclared retained volume')
            elif kind == 'assets':
                closed(record, {'artifact', 'serving'}, 'asset', {'artifact', 'serving'})
            elif kind == 'networks':
                closed(record, {'internal'}, 'network', {'internal'})
                if type(record['internal']) is not bool:
                    raise ValueError('network isolation must be explicit')
            else:
                closed(record, set(), 'retained volume')
    for kind in ['services', 'jobs']:
        for record in value[kind].values():
            for dependency, condition in record.get('needs', {}).items():
                if dependency not in logical or condition not in {'started', 'healthy', 'completed'}:
                    raise ValueError('workload selects an undeclared dependency or condition')
                if condition == 'completed' and dependency not in value['jobs']:
                    raise ValueError('completed dependencies must select a one-shot job')
    for route in value['routes']:
        closed(route, {'host', 'path', 'target', 'strip_prefix', 'headers', 'redirect', 'proxy'},
               'route', {'host', 'path'})
        if ('target' in route) == ('redirect' in route):
            raise ValueError('route requires exactly one target or redirect')
        if 'target' in route:
            target = route['target']
            closed(target, {'kind', 'name', 'port'}, 'route target', {'kind', 'name'})
            collection = {'asset': 'assets', 'service': 'services'}.get(target['kind'])
            if collection is None or target['name'] not in value[collection]:
                raise ValueError('route selects an undeclared target')
    if not value['routes'] or not isinstance(value['readiness'], list):
        raise ValueError('workload requires routes and explicit readiness assertions')


def source(root, environment, revision):
    if not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('release requires an exact source revision')
    actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True, timeout=30).strip()
    if actual != revision:
        raise ValueError('release source differs from the actual checked-out revision')
    origin = subprocess.check_output(['git', 'remote', 'get-url', 'origin'], cwd=root, text=True, timeout=30).strip()
    found = re.fullmatch(r'(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?', origin)
    if found is None:
        raise ValueError('release source requires its actual GitHub origin')
    repository = found[1]
    if environment.get('GITHUB_REPOSITORY', repository).lower() != repository.lower():
        raise ValueError('release source differs from its workflow repository')
    repository_id = environment.get('GITHUB_REPOSITORY_ID')
    if not repository_id:
        repository_id = subprocess.check_output(['gh', 'api', 'repos/' + repository, '--jq', '.id'],
                                                cwd=root, text=True, timeout=30).strip()
    if not re.fullmatch('[1-9][0-9]{0,19}', repository_id):
        raise ValueError('release requires its immutable repository identifier')
    return {'repository': repository, 'repository_id': repository_id, 'revision': revision}


def assets(root, config):
    records = config.get('components', {}).get('assets', [])
    if not isinstance(records, list) or not 1 <= len(records) <= 16:
        raise ValueError('application requires bounded static asset declarations')
    result = {}
    for record in records:
        closed(record, {'name', 'source', 'max_bytes', 'max_file_bytes', 'max_files', 'serving_metadata'},
               'static build input', {'name', 'source', 'max_bytes', 'max_file_bytes'})
        name = record['name']
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in result:
            raise ValueError('static assets need distinct native component names')
        relative(root, record['source'], exists=True)
        if 'serving_metadata' in record:
            relative(root, record['serving_metadata'], exists=True)
        result[name] = record
    return result


def package(root, config, environment, definition):
    root = Path(root)
    authored = declaration(root)
    if authored['application'] != definition['project']:
        raise ValueError('application and build publication identities disagree')
    revision = environment['APP_VERSION']
    release = {'kind': 'komizo.release', 'version': 1, 'application': authored['application'],
               'source': source(root, environment, revision), 'artifacts': {},
               'providers': provider_provenance(),
               'workload': copy.deepcopy(authored['workload'])}
    import preview_release
    release = preview_release.project(root, config, environment, release)
    for assertion in release['workload']['readiness']:
        if 'contains' in assertion:
            assertion['contains'] = assertion['contains'].replace('{revision}', revision)
    directory = relative(root, '.artifacts/release')
    directory.parent.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError('release packet destination may not be linked')
    with tempfile.TemporaryDirectory(prefix='.release-', dir=directory.parent) as temporary:
        target = Path(temporary)
        members = {'release.json': 'release'}
        selected_assets = assets(root, config)
        if set(selected_assets) != {value['artifact'] for value in release['workload']['assets'].values()}:
            raise ValueError('authored asset references differ from exact build declarations')
        for name, record in selected_assets.items():
            path = 'static/' + name + '.tar.gz'
            metadata = packet.pack_static(relative(root, record['source'], exists=True), target / path,
                max_bytes=record['max_bytes'], max_file_bytes=record['max_file_bytes'],
                max_files=record.get('max_files', packet.MAX_MEMBERS))
            digest = 'sha256:' + metadata['sha256']
            manifest = static_manifest(digest, metadata['size'])
            release['artifacts'][name] = {'kind': 'archive',
                'reference': definition['image_base'] + '-' + name + '@sha256:' + hashlib.sha256(manifest).hexdigest(),
                'digest': digest, 'size_bytes': metadata['size'], 'media_type': STATIC_MEDIA}
            members[path] = 'static'
            if 'serving_metadata' in record:
                serving = packet.read_json(relative(root, record['serving_metadata'], exists=True), 1 << 20)
                if not isinstance(serving, dict):
                    raise ValueError('generated static serving metadata must be an explicit object')
                for value in release['workload']['assets'].values():
                    if value['artifact'] == name:
                        value['serving'] = serving
        sources = {name: definition['image_base'] + '-' + name + ':' + revision
                   for name in image_components(definition)}
        needed = {value['artifact'] for kind in ['services', 'jobs'] for value in release['workload'][kind].values()}
        if set(sources) != needed or set(sources) & set(selected_assets):
            raise ValueError('workload image references differ from exact shipping build declarations')
        oci = dependencies.helper('oci_archive')
        for name, reference in sources.items():
            path = 'images/' + name + '.oci.tar'
            metadata = oci.save_image(reference, target / path, environment=environment)
            repository = reference.split('@')[0] if '@' in reference else reference.rsplit(':', 1)[0]
            release['artifacts'][name] = {'kind': 'image', 'reference': repository + '@' + metadata['manifest_digest'],
                'digest': metadata['manifest_digest'], 'config_digest': metadata['config_digest'], 'platform': metadata['platform']}
            members[path] = 'image'
        if definition.get('stateful_contract'):
            release['stateful_contract'] = dependencies.helper('stateful_release').materialize(
                relative(root, definition['stateful_contract'], exists=True), revision)
        for name in config.get('artifacts', {}).get('release_metadata', []):
            path = relative(root, name, exists=True)
            member = path.relative_to(directory).as_posix()
            if not member.startswith('metadata/'):
                raise ValueError('release metadata requires an explicit metadata member path')
            packet.regular(path, 1 << 20)
            destination = target / member
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(packet.read_regular(path, 1 << 20))
            members[member] = 'metadata'
        validate_workload(release['workload'])
        (target / 'release.json').write_bytes(canonical(release))
        native_validate(root, target / 'release.json', environment)
        packet.write_inventory(target, members)
        if directory.exists():
            shutil.rmtree(directory)
        os.replace(target, directory)
    return release


def native_validate(root, path, environment):
    import komizo_core
    result = subprocess.run([str(komizo_core.binary(root, environment)), 'release', 'validate', str(path)],
                            cwd=root, env=environment, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError('built release does not satisfy the accepted native Komizo workload contract: ' + result.stderr[-2048:])


def image_components(definition):
    result = []
    for image in definition['inputs']['images']:
        prefix, suffix = definition['image_base'] + '-', ':{revision}'
        if not image.startswith(prefix) or not image.endswith(suffix):
            raise ValueError('shipping images require the exact source image family')
        name = image[len(prefix):-len(suffix)]
        if not NAME.fullmatch(name) or name in result or name in {'config', 'gate'}:
            raise ValueError('shipping images require distinct workload component names')
        result.append(name)
    return result


def verify(root, environment, *, directory=None, expected_source=None):
    directory = Path(root) / '.artifacts/release' if directory is None else Path(directory)
    inventory = packet.verify_inventory(directory)
    raw = packet.read_regular(directory / 'release.json', 1 << 20)
    release = json.loads(raw, object_pairs_hook=packet.unique_object,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError('non-finite release metadata')))
    if canonical(release) != raw or hashlib.sha256(raw).hexdigest() != inventory['files']['release.json']['sha256']:
        raise ValueError('release descriptor bytes are not the exact accepted canonical JSON')
    closed(release, {'kind', 'version', 'application', 'source', 'artifacts', 'workload', 'stateful_contract', 'providers'},
           'release', {'kind', 'version', 'application', 'source', 'artifacts', 'workload', 'providers'})
    validate_providers(release['providers'])
    if release['kind'] != 'komizo.release' or type(release['version']) is not int or release['version'] != 1:
        raise ValueError('release requires the native descriptor contract')
    expected_source = source(root, environment, environment['APP_VERSION']) if expected_source is None else expected_source
    if release['source'] != expected_source:
        raise ValueError('release source differs from current exact source authority')
    validate_workload(release['workload'])
    needed = {record['artifact'] for kind in ['services', 'jobs', 'assets']
              for record in release['workload'][kind].values()}
    if not isinstance(release['artifacts'], dict) or set(release['artifacts']) != needed:
        raise ValueError('release artifacts differ from its exact workload references')
    for name, artifact in release['artifacts'].items():
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError('release artifacts require native logical names')
        if not isinstance(artifact, dict) or not isinstance(artifact.get('reference'), str) or not IMAGE.fullmatch(artifact['reference']):
            raise ValueError('release artifacts require immutable OCI manifest references')
        if artifact['kind'] == 'image':
            closed(artifact, {'kind', 'reference', 'digest', 'config_digest', 'platform'}, 'image artifact',
                   {'kind', 'reference', 'digest', 'config_digest', 'platform'})
            if not DIGEST.fullmatch(artifact['digest']) or not artifact['reference'].endswith('@' + artifact['digest']):
                raise ValueError('image artifact reference differs from its manifest identity')
            metadata = dependencies.helper('oci_archive').inspect_archive(directory / 'images' / (name + '.oci.tar'))
            if any(artifact[key] != metadata[key] for key in ['config_digest', 'platform']) or artifact['digest'] != metadata['manifest_digest']:
                raise ValueError('release image differs from its accepted OCI bytes')
        elif artifact['kind'] == 'archive':
            closed(artifact, {'kind', 'reference', 'digest', 'size_bytes', 'media_type'}, 'static artifact',
                   {'kind', 'reference', 'digest', 'size_bytes', 'media_type'})
            if artifact['media_type'] != STATIC_MEDIA:
                raise ValueError('static artifact has an unknown payload media type')
            digest, size = packet.digest_file(directory / 'static' / (name + '.tar.gz'))
            manifest = static_manifest('sha256:' + digest, size)
            if (artifact['digest'] != 'sha256:' + digest or artifact['size_bytes'] != size
                    or not artifact['reference'].endswith('@sha256:' + hashlib.sha256(manifest).hexdigest())):
                raise ValueError('release static identity differs from its exact accepted archive bytes')
        else:
            raise ValueError('release contains an unknown artifact kind')
    return release
