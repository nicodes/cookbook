"""Exercise fresh migration and encrypted recovery using only synthetic source data."""
import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import time

import application_restore
import artifact_packet
import dependencies
import deployment
import native_runtime


def execute(arguments, environment, *, body=None, timeout=60):
    result = native_runtime.run(arguments, environment=environment, input=body,
        capture_output=True, timeout=timeout)
    if result.returncode:
        raise ValueError('synthetic recovery command failed; no private command output is emitted')
    return result.stdout


@contextlib.contextmanager
def fresh_database(artifact, database, directory, environment):
    """One unpublished namespace, fresh data, synthetic owner and no capabilities."""
    name = 'cookbook-firstboot-' + secrets.token_hex(12)
    uid = os.getuid() or 65534
    data = directory / 'database'
    data.mkdir(mode=0o700)
    if os.geteuid() == 0:
        os.chown(data, uid, uid)
    password = secrets.token_hex(32)
    values = environment | {'POSTGRES_USER': 'fixture_owner', 'POSTGRES_PASSWORD': password,
        'POSTGRES_DB': database, 'PGDATA': '/var/lib/postgresql/data'}
    args = ['run', '--detach', '--pull=never', '--name', name, '--network=none', '--read-only',
        '--cap-drop=ALL', '--security-opt=no-new-privileges', '--user', f'{uid}:{uid}',
        '--memory=256m', '--memory-swap=256m', '--cpus=1', '--pids-limit=128',
        '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=32m',
        '--tmpfs', '/var/run/postgresql:rw,noexec,nosuid,nodev,mode=1777,size=8m',
        '--mount', f'type=bind,src={data},dst=/var/lib/postgresql/data']
    if os.geteuid() != 0:
        args += ['--userns=keep-id']
    for key in ['POSTGRES_USER', 'POSTGRES_PASSWORD', 'POSTGRES_DB', 'PGDATA']:
        args += ['--env', key]
    args += [artifact['config_digest'], 'postgres', '-c', 'listen_addresses=127.0.0.1']
    try:
        execute(args, values)
        deadline = time.monotonic() + 90
        while True:
            ready = native_runtime.run(['exec', name, 'pg_isready', '-h', '127.0.0.1', '-U', 'fixture_owner', '-d', database],
                environment=values, capture_output=True, timeout=5)
            if ready.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise ValueError('fresh synthetic database startup deadline exceeded')
            time.sleep(.2)
        yield name, 'postgres://fixture_owner:' + password + '@127.0.0.1:5432/' + database + '?sslmode=disable'
    finally:
        cleaned = native_runtime.run(['rm', '--force', '--volumes', name], environment=environment, capture_output=True, timeout=30)
        if cleaned.returncode:
            raise ValueError('synthetic database cleanup failed')


def bootstrap(release, config, database, dsn, directory, environment, *, malformed=False):
    profile = config['bootstrap']
    job = release['workload']['jobs'].get(profile['job'])
    if job is None:
        raise ValueError('synthetic first boot requires its actual accepted migration job')
    name = 'cookbook-migrate-' + secrets.token_hex(12)
    uid = int(job['user'].split(':')[0])
    values = job.get('environment', {}) | profile['environment'] | {
        profile['dsn_variable']: dsn,
        profile['password_variable']: 'malformed\x01password' if malformed else secrets.token_hex(32)}
    envfile = directory / ('malformed.env' if malformed else 'migration.env')
    envfile.write_text(''.join(key + '=' + value + '\n' for key, value in values.items()))
    envfile.chmod(0o600)
    arguments = application_restore.container_arguments(job, database, name)
    for item in job.get('volumes', []):
        path = directory / 'volumes' / item['name']
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.geteuid() == 0:
            os.chown(path, uid, uid)
        arguments += ['--mount', f'type=bind,src={path},dst={item["target"]}' + (',readonly' if item['read_only'] else '')]
    arguments += ['--env-file', str(envfile), release['artifacts'][job['artifact']]['config_digest'], *job.get('command', [])]
    try:
        execute(arguments, environment)
        status = execute(['wait', '--condition=exited', name], environment, timeout=job['timeout_seconds'] + 10).decode().strip()
        if status != '0' and not malformed:
            raise ValueError('accepted migration job failed against an empty synthetic database')
        if status == '0' and malformed:
            raise ValueError('migration accepted malformed bootstrap credentials')
        return status
    finally:
        cleaned = native_runtime.run(['rm', '--force', '--volumes', name], environment=environment, capture_output=True, timeout=30)
        if cleaned.returncode:
            raise ValueError('synthetic migration cleanup failed')


def capture(root, release, config, directory, environment):
    postgres = release['workload']['services'].get('postgres')
    if postgres is None:
        raise ValueError('synthetic recovery requires its declared PostgreSQL service')
    engine = release['artifacts'][postgres['artifact']]
    actual = native_runtime.inspect([engine['config_digest']], environment)[0]
    major = [item.partition('=')[2] for item in actual['Config'].get('Env', []) if item.startswith('PG_MAJOR=')]
    if len(major) != 1 or not major[0].isdigit():
        raise ValueError('accepted database image does not declare its PostgreSQL major')
    target = directory / 'capture'
    target.mkdir(mode=0o700)
    owner_database = postgres.get('environment', {}).get('POSTGRES_DB', config['database'])
    with fresh_database(engine, owner_database, directory, environment) as (database, dsn):
        # Failure must precede creation of a serving process; no application is
        # launched until a successful execution of the actual declared job.
        bootstrap(release, config, database, dsn, directory, environment, malformed=True)
        absent = execute(['exec', database, 'psql', '-XAt', '-U', 'fixture_owner', '-d', owner_database,
            '-c', "SELECT count(*) FROM pg_roles WHERE rolname='" + config['runtime_role'] + "'"], environment)
        if absent.strip() != b'0':
            raise ValueError('failed first boot left a runtime role behind')
        bootstrap(release, config, database, dsn, directory, environment)
        # Nonempty sentinel verifies that a truncated or empty dump cannot pass.
        execute(['exec', '-i', database, 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-U', 'fixture_owner', '-d', config['database']],
            environment, body=b'CREATE TABLE cookbook_recovery_sentinel(id int primary key); INSERT INTO cookbook_recovery_sentinel VALUES(1);')
        dump = execute(['exec', database, 'pg_dump', '-Fc', '--no-owner', '--no-tablespaces',
            '-U', 'fixture_owner', '-d', config['database']], environment, timeout=180)
        (target / 'database.dump').write_bytes(dump)
    if config.get('blobs'):
        job = release['workload']['jobs'][config['bootstrap']['job']]
        mounts = [item for item in job.get('volumes', []) if not item['read_only']]
        if len(mounts) != 1:
            raise ValueError('synthetic media capture needs one explicit owned writable migration volume')
        shutil.copytree(directory / 'volumes' / mounts[0]['name'], target / 'files')
    files = {}
    for path in target.rglob('*'):
        if path.is_file():
            path.chmod(0o600)
            digest, size = artifact_packet.digest_file(path)
            files[path.relative_to(target).as_posix()] = {'size': size, 'sha256': digest}
    (target / 'capture.json').write_text(json.dumps({'snapshot_id': 'AAAA-BBBB-1', 'reclamation_fenced': True,
        'completed_at': datetime.now(timezone.utc).isoformat(), 'database': config['database'],
        'major': int(major[0]), 'files': files}))
    images = {name: {'reference': value['reference'], 'image_id': value['config_digest']}
        for name, value in release['artifacts'].items() if value['kind'] == 'image'}
    metadata = {'project': release['application'], 'revision': release['source']['revision'],
        'roles': config.get('roles', [config['runtime_role']]), 'images': images,
        'engine': {'reference': engine['reference'], 'image_id': engine['config_digest'], 'major': int(major[0])},
        'deployment': {'release': release, 'release_digest': 'sha256:' + hashlib.sha256(deployment.canonical(release)).hexdigest(),
            'provider': release['providers']}}
    if 'role_namespace' in config:
        metadata['role_namespace'] = config['role_namespace']
    return dependencies.helper('postgres-recovery').package_capture(target, directory / 'package', metadata)


def run(root, environment):
    root = Path(root)
    release = deployment.verify(root, environment)
    profile = application_restore.load_config(root / 'deploy/restore.toml')
    if release['providers'] != deployment.provider_provenance():
        raise ValueError('synthetic restore must exercise the exact accepted source providers')
    with tempfile.TemporaryDirectory(prefix='application-synthetic-') as scratch:
        scratch = Path(scratch)
        package = capture(root, release, profile, scratch, environment)
        key, certificate = scratch / 'key.pem', scratch / 'recipient.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-noenc', '-keyout', str(key),
            '-out', str(certificate), '-days', '1', '-subj', '/CN=synthetic-source-restore'],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        key.chmod(0o600)
        sealed = dependencies.helper('snapshot').seal(package, certificate)
        pair = scratch / 'pair.tar'
        with tarfile.open(pair, 'w') as stream:
            for name in ['snapshot.cms', 'receipt.json']:
                stream.add(sealed / name, arcname=name)
        result = application_restore.restore(release['application'], profile, str(pair), key,
            packet=root / '.artifacts/release', environment=environment)
        result['scope'] = 'current-source synthetic restoration'
        result['current_source_ci_acceptance'] = False
        result['first_boot'] = {'empty_database_migration': 'passed', 'malformed_credentials_refused': 'passed',
            'serving_after_success_only': 'passed'}
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        print(json.dumps(run(Path.cwd(), dict(os.environ)), sort_keys=True))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
