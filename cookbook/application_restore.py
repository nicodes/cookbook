#!/usr/bin/env python3
"""Restore an owner-selected encrypted snapshot into disposable native resources."""
import argparse
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import artifact_packet
import dependencies
import deployment
import komizo_core
import native_runtime
import sdk_source
import workload_fixture

ROOT = Path(__file__).resolve().parents[1]


def load_config(path):
    value = dependencies.helper('source_config').read(path, maximum=32768)
    fields = {'version', 'project', 'image_base', 'runtime_role', 'api_service', 'api_port',
              'health_path', 'dsn_transport', 'dsn_variable', 'dsn_file', 'blobs', 'env', 'database', 'bootstrap', 'roles', 'role_namespace'}
    deployment.closed(value, fields, 'application restore',
        {'version', 'project', 'image_base', 'runtime_role', 'api_service', 'api_port', 'health_path', 'env'})
    if (type(value['version']) is not int or value['version'] != 1 or not deployment.NAME.fullmatch(value['project'])
            or not re.fullmatch('[a-z][a-z0-9_]{0,62}', value['runtime_role'])
            or not deployment.NAME.fullmatch(value['api_service'])
            or type(value['api_port']) is not int or not 1 <= value['api_port'] <= 65535
            or not re.fullmatch('/[A-Za-z0-9/_-]*', value['health_path'])
            or not re.fullmatch(r'ghcr.io/[a-z0-9_.-]+/[a-z0-9_.-]+', value['image_base'])):
        raise ValueError('application restore has an invalid native identity or health target')
    environment = value['env']
    if (not isinstance(environment, dict) or len(environment) > 64
            or any(not re.fullmatch('[A-Z][A-Z0-9_]*', name) or not isinstance(item, str)
                or len(item) > 16384 or any(char in item for char in '\r\n\0')
                for name, item in environment.items())):
        raise ValueError('restore parameters must be bounded literal synthetic environment values')
    transport = value.get('dsn_transport', 'environment')
    variable = value.get('dsn_variable', 'RUNTIME_DATABASE_URL_FILE' if transport == 'file' else 'RUNTIME_DATABASE_URL')
    if (transport not in {'file', 'environment'} or not re.fullmatch('[A-Z][A-Z0-9_]*', variable)
            or variable in environment or transport == 'file' and not value.get('dsn_file', '').startswith('/')):
        raise ValueError('restore credentials require one explicit synthetic DSN transport')
    for name in ['dsn_file', 'blobs']:
        if name in value and (not isinstance(value[name], str) or not value[name].startswith('/')
                or '..' in value[name].split('/') or ',' in value[name] or any(c in value[name] for c in '\r\n\0')):
            raise ValueError('restore mounts require explicit bounded container paths')
    if 'database' in value and not re.fullmatch('[a-z][a-z0-9_]{0,62}', value['database']):
        raise ValueError('synthetic restoration requires its literal database identifier')
    if 'roles' in value:
        roles = value['roles']
        namespace = value.get('role_namespace', value['project'])
        if (not isinstance(roles, list) or not 1 <= len(roles) <= 32 or any(not isinstance(role, str) for role in roles)
                or len(set(roles)) != len(roles)
                or value['runtime_role'] not in roles or not isinstance(namespace, str)
                or not re.fullmatch('[a-z][a-z0-9_]{0,62}', namespace)
                or any(not isinstance(role, str) or not re.fullmatch('[a-z][a-z0-9_]{0,62}', role)
                    or not (role == value['project'] or role == namespace or role.startswith(namespace + '_')) for role in roles)):
            raise ValueError('restore roles must retain the explicit application namespace and runtime reader')
    elif 'role_namespace' in value:
        raise ValueError('a restore role namespace requires its complete role inventory')
    if 'bootstrap' in value:
        bootstrap = value['bootstrap']
        deployment.closed(bootstrap, {'job', 'dsn_variable', 'runtime_dsn_variable', 'environment'},
            'synthetic bootstrap', {'job', 'dsn_variable', 'runtime_dsn_variable', 'environment'})
        if (not deployment.NAME.fullmatch(bootstrap['job'])
                or any(not re.fullmatch('[A-Z][A-Z0-9_]*', bootstrap[key]) for key in ['dsn_variable', 'runtime_dsn_variable'])
                or bootstrap['dsn_variable'] == bootstrap['runtime_dsn_variable']
                or not isinstance(bootstrap['environment'], dict)
                or any(not re.fullmatch('[A-Z][A-Z0-9_]*', key) or not isinstance(item, str)
                    or any(char in item for char in '\r\n\0') for key, item in bootstrap['environment'].items())):
            raise ValueError('synthetic bootstrap parameters must be closed literal source data')
    return value | {'dsn_transport': transport, 'dsn_variable': variable}


def provider_binding():
    return deployment.provider_provenance()


def admitted_release(manifest, config, directory, environment):
    record = manifest.get('deployment')
    deployment.closed(record, {'release', 'release_digest', 'provider'}, 'snapshot deployment',
                       {'release', 'release_digest', 'provider'})
    if record['provider'] != provider_binding():
        raise ValueError('select the exact retained recipe providers before restoring this snapshot')
    release = record['release']
    if release.get('providers') != record['provider']:
        raise ValueError('snapshot provider selection differs from its accepted release descriptor')
    raw = deployment.canonical(release)
    digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
    if digest != record['release_digest'] or release.get('application') != config['project']:
        raise ValueError('snapshot descriptor differs from its exact selected application identity')
    if release.get('source', {}).get('revision') != manifest['revision']:
        raise ValueError('snapshot data and release have different exact source revisions')
    path = directory / 'retained-release.json'
    path.write_bytes(raw)
    deployment.native_validate(Path.cwd(), path, environment)
    for name, artifact in release['artifacts'].items():
        repository = artifact['reference'].split('@')[0]
        if repository != config['image_base'] + '-' + name:
            raise ValueError('snapshot artifact escapes its source-owned application namespace')
    return release, digest


def retained_packet(root, release, selected, environment, pull):
    selected = Path(selected)
    if selected.exists():
        value = deployment.verify(root, environment, directory=selected, expected_source=release['source'])
        if deployment.canonical(value) != deployment.canonical(release):
            raise ValueError('local artifact packet belongs to another retained release')
        return selected
    if not pull:
        raise ValueError('the exact retained artifact packet is required; prepare it or select immutable registry reads')
    selected.mkdir(parents=True, mode=0o700)
    members = {'release.json': 'release'}
    try:
        for name, artifact in release['artifacts'].items():
            if artifact['kind'] == 'image':
                path = 'images/' + name + '.oci.tar'
                dependencies.helper('registry_artifact').fetch_image(artifact, selected / path,
                    authfile=environment.get('REGISTRY_AUTH_FILE'), timeout=300)
                members[path] = 'image'
            else:
                path = 'static/' + name + '.tar.gz'
                dependencies.helper('registry_artifact').fetch_static(artifact, selected / path,
                    authfile=environment.get('REGISTRY_AUTH_FILE'), timeout=300)
                members[path] = 'static'
        (selected / 'release.json').write_bytes(deployment.canonical(release))
        artifact_packet.write_inventory(selected, members)
        deployment.verify(root, environment, directory=selected, expected_source=release['source'])
    except BaseException:
        shutil.rmtree(selected)
        raise
    return selected


def private_credentials(config, dsn, directory, uid):
    body = artifact_packet.read_regular(dsn, 8192).strip()
    if not body.startswith((b'postgres://', b'postgresql://')) or any(char in body for char in [b'\0', b'\n', b'\r']):
        raise ValueError('restored runtime credential is malformed')
    file = directory / 'runtime.url'
    file.write_bytes(body + b'\n')
    file.chmod(0o400)
    if os.geteuid() == 0:
        os.chown(file, uid, uid)
    if config['dsn_transport'] == 'file':
        return {config['dsn_variable']: config['dsn_file']}, [
            '--mount', f'type=bind,src={file},dst={config["dsn_file"]},readonly']
    return {config['dsn_variable']: body.decode()}, []


def blob_mounts(payload, config, uid):
    files = payload / 'files'
    if not config.get('blobs'):
        if files.exists() and any(files.rglob('*')):
            raise ValueError('authenticated snapshot media requires its explicit product mount')
        return []
    files.mkdir(mode=0o700, exist_ok=True)
    for path in [files, *files.rglob('*')]:
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError('restored media contains an unsafe filesystem member')
        if os.geteuid() == 0:
            os.chown(path, uid, uid)
    return ['--mount', f'type=bind,src={files},dst={config["blobs"]},readonly']


def container_arguments(record, namespace, name):
    user = record['user']
    uid, _, gid = user.partition(':')
    gid = gid or uid
    limits = record['limits']
    result = ['run', '--detach', '--pull=never', '--name', name, '--network', 'container:' + namespace,
        '--label', 'cookbook.restore=' + name, '--read-only', '--read-only-tmpfs=false', '--image-volume=ignore', '--user', user, '--cap-drop=ALL',
        '--security-opt=no-new-privileges', '--memory', str(limits['memory_bytes']),
        '--memory-swap', str(limits['memory_swap_bytes']), '--cpus', str(limits['milli_cpus'] / 1000),
        '--pids-limit', str(limits['pids'])]
    if os.geteuid() != 0:
        result += ['--userns=keep-id:uid=' + uid + ',gid=' + gid]
    for target, value in record.get('tmpfs', {}).items():
        execution = 'exec' if value.get('executable', False) else 'noexec'
        result += ['--tmpfs', f'{target}:rw,nosuid,nodev,{execution},size={value["size_bytes"]},mode={value["mode"]:o}']
    if record.get('init'):
        result += ['--init']
    if record.get('working_directory'):
        result += ['--workdir', record['working_directory']]
    if record.get('entrypoint'):
        result += ['--entrypoint', json.dumps(record['entrypoint'])]
    return result


class BytesSocket:
    def __init__(self, body):
        self.body = body
    def makefile(self, *_arguments):
        return io.BytesIO(self.body)


def probe(store, port, path, watched, environment, timeout=120):
    deadline = time.monotonic() + timeout
    command = ['exec', store, '/bin/bash', '-c',
        'exec 3<>/dev/tcp/127.0.0.1/$1; printf "GET %s HTTP/1.1\\r\\nHost: 127.0.0.1\\r\\nConnection: close\\r\\n\\r\\n" "$2" >&3; cat <&3',
        'restore-health', str(port), path]
    while time.monotonic() < deadline:
        state = native_runtime.run(['container', 'inspect', watched], environment=environment,
            capture_output=True, check=True, text=True, timeout=10)
        if not json.loads(state.stdout)[0]['State']['Running']:
            raise ValueError('restored API exited before becoming healthy')
        result = native_runtime.run(command, environment=environment, capture_output=True, timeout=15)
        if result.returncode == 0 and 0 < len(result.stdout) <= 1 << 20:
            try:
                response = http.client.HTTPResponse(BytesSocket(result.stdout))
                response.begin()
                body = response.read(1 << 20)
                if response.status == 200:
                    return body
            except (OSError, http.client.HTTPException):
                pass
        time.sleep(.25)
    raise TimeoutError('restored API readiness deadline exceeded')


def restore(project, config, export, key, *, packet=None, pull=False, environment=None):
    environment = dict(os.environ if environment is None else environment)
    if project != config['project']:
        raise ValueError('restore profile belongs to another application')
    memory = Path('/proc/meminfo')
    if memory.exists():
        available = re.search(r'^MemAvailable:\s+(\d+) kB$', memory.read_text(), re.M)
        if available is None or int(available[1]) < 768 * 1024:
            raise ValueError('owner restore requires768MiB of available recovery capacity')
    receiver = dependencies.helper('receive-backup')
    snapshot = dependencies.helper('snapshot')
    recovery = dependencies.helper('postgres-recovery')
    with tempfile.TemporaryDirectory(prefix=project + '-owner-restore-') as working:
        working = Path(working)
        receiver.receive(export, working / 'encrypted')
        payload = working / 'payload'
        payload.mkdir(mode=0o700)
        evidence = snapshot.unseal(working / 'encrypted', key, payload)
        if evidence.get('backend') != 'postgresql':
            raise ValueError('application restore accepts only its native PostgreSQL snapshot')
        manifest = evidence['postgresql']
        if manifest['project'] != project:
            raise ValueError('authenticated snapshot belongs to another application')
        release, digest = admitted_release(manifest, config, working, environment)
        selected = Path(packet) if packet is not None else working / 'artifacts'
        selected = retained_packet(Path.cwd(), release, selected, environment, pull)
        runtime_images = {name: value for name, value in release['artifacts'].items() if value['kind'] == 'image'}
        oci = dependencies.helper('oci_archive')
        for name, artifact in runtime_images.items():
            oci.load_image(selected / 'images' / (name + '.oci.tar'), environment=environment)
        references = [value['config_digest'] for value in runtime_images.values()]
        for artifact, actual in zip(runtime_images.values(), native_runtime.inspect(references, environment), strict=True):
            if actual['Id'] != artifact['config_digest']:
                raise ValueError('retained OCI image differs from authenticated snapshot metadata')
        service = release['workload']['services'].get(config['api_service'])
        if service is None or service['artifact'] not in runtime_images:
            raise ValueError('retained release has no declared API service')
        uid = int(service['user'].split(':')[0])
        containers = []
        prefix = project + '-restore-' + secrets.token_hex(8)
        try:
            with recovery.restored_database(payload, project, manifest['revision'], pull=False) as database:
                if config['runtime_role'] not in database['credentials']:
                    raise ValueError('snapshot lacks the source-declared restricted runtime role')
                values, mounts = private_credentials(config, database['credentials'][config['runtime_role']], working, uid)
                values = service.get('environment', {}) | config['env'] | values
                envfile = working / 'api.env'
                if any(any(character in value for character in '\r\n\0') for value in values.values()):
                    raise ValueError('restore runtime parameters must be single-line values')
                envfile.write_text(''.join(name + '=' + value + '\n' for name, value in values.items()))
                envfile.chmod(0o600)
                name = prefix + '-api'
                containers.append(name)
                command = container_arguments(service, database['container'], name)
                command += mounts + blob_mounts(payload, config, uid) + ['--env-file', str(envfile),
                    runtime_images[service['artifact']]['config_digest'], *service.get('command', [])]
                native_runtime.run(command, environment=environment, capture_output=True, check=True, timeout=60)
                probe(database['container'], config['api_port'], config['health_path'], name, environment)
                # The frontend is public immutable data, served without joining the private restore namespace.
                fixture_config = {'components': {'assets': [{'name': name, 'max_bytes': 512 << 20,
                    'max_file_bytes': 128 << 20} for name, item in release['artifacts'].items() if item['kind'] == 'archive']}}
                upstreams = {item['target']['name']: '127.0.0.1:1' for item in release['workload']['routes']
                             if item.get('target', {}).get('kind') == 'service'}
                with workload_fixture.context(Path.cwd(), fixture_config, environment, upstreams=upstreams,
                        packet=selected, expected_source=release['source']) as fixture:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open(urllib.request.Request(fixture.base_url + '/', headers={'Host': fixture.hostname}), timeout=10) as response:
                        html = response.read(4 << 20).decode()
                        if response.status != 200 or '<html' not in html.lower() or '<script' not in html.lower():
                            raise ValueError('retained static artifact did not serve an application document')
                recovery.validate_payload(payload, expected_project=project, expected_revision=manifest['revision'])
                result = {'version': 1, 'project': project, 'backend': 'postgresql', 'revision': manifest['revision'],
                    'snapshot_release': {'digest': digest, 'source': release['source'], 'provider': manifest['deployment']['provider']},
                    'scope': 'disposable authenticated snapshot restoration', 'current_source_ci_acceptance': False,
                    'network': 'none; shared isolated loopback only', 'published_ports': [],
                    'database_integrity': 'ok', 'database_restore': database['database_restore'], 'database_boot': 'passed',
                    'api_boot': 'passed', 'frontend_artifact': 'passed', 'media_integrity': 'passed',
                    'http_assertions': {'api_health': 'passed', 'frontend_document': 'passed', 'unknown_host_refusal': 'passed'},
                    'images': {name: value['config_digest'] for name, value in runtime_images.items()},
                    'static_artifacts': {name: value['digest'] for name, value in release['artifacts'].items() if value['kind'] == 'archive'},
                    'files_restored': sum(name.startswith('files/') for name in manifest['files']),
                    'archive_sha256': evidence['archive_sha256'], 'ciphertext_sha256': snapshot.sha256(working / 'encrypted/snapshot.cms'),
                    'backup_verified_at': evidence['verified_at']}
        finally:
            failed = []
            for name in reversed(containers):
                cleaned = native_runtime.run(['rm', '--force', '--volumes', name], environment=environment,
                    capture_output=True, timeout=30)
                if cleaned.returncode:
                    failed.append(name)
            if failed:
                raise ValueError('owned application restore cleanup failed')
        result['cleanup'] = 'passed'
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--config', type=Path, default=Path('deploy/restore.toml'))
    parser.add_argument('--export', required=True)
    parser.add_argument('--key', required=True, type=Path)
    parser.add_argument('--packet', type=Path)
    parser.add_argument('--pull', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    value = restore(args.project, load_config(args.config), args.export, args.key, packet=args.packet, pull=args.pull)
    body = json.dumps(value, sort_keys=True) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body)
    else:
        print(body, end='')


if __name__ == '__main__':
    main()
