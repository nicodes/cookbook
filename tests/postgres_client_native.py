"""Verify the actual pinned PostgreSQL/libpq namespace boundary on Linux."""
import json
import hashlib
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time


IMAGE = 'docker.io/library/postgres@sha256:4ef4dbc939d61acea57712655ddb4b4ab27419c913f94cca0cd57cb3ea3c2280'
LABEL = 'io.komizo.postgres-client-fixture'
ENVIRONMENT = {'PATH': '/usr/bin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8'}
PROFILE = Path(__file__).with_name('fixtures') / 'apparmor-workload'
PROFILE_SHA256 = '06065f42a27508c46fef9f68f93a44351ab97555a60859f10829099880a5c799'
PROFILES = Path('/sys/kernel/security/apparmor/profiles')


def run(arguments, *, body=None, timeout=30, check=True):
    result = subprocess.run(['/usr/bin/podman', '--remote=false', *arguments], input=body,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENVIRONMENT, timeout=timeout)
    if len(result.stdout) > 1 << 20 or len(result.stderr) > 1 << 20:
        raise ValueError('native client fixture output exceeded its bound')
    if check and result.returncode:
        # These exact synthetic objects contain no credentials or product data.
        raise ValueError('native client fixture command failed: ' + result.stderr[:8192].decode(errors='replace'))
    return result


def present(kind, name):
    result = run([kind, 'exists', name], check=False)
    if result.returncode not in {0, 1}:
        raise ValueError('native fixture object presence is uncertain')
    return result.returncode == 0


def server_failure(name, readiness):
    value = json.loads(run(['container', 'inspect', name]).stdout)[0]
    state = {key: value['State'].get(key) for key in ['Status', 'Running', 'Pid', 'ExitCode', 'OOMKilled', 'Error']}
    logs = run(['logs', '--tail', '30', name], check=False)
    state['logs'] = (logs.stdout + logs.stderr)[:16384].decode(errors='replace')
    state['readiness'] = {'status': readiness.returncode, 'stdout': readiness.stdout[:4096].decode(errors='replace'),
        'stderr': readiness.stderr[:4096].decode(errors='replace')}
    top = run(['top', name, 'hpid', 'user', 'args'], check=False)
    state['processes'] = (top.stdout + top.stderr)[:8192].decode(errors='replace')
    raise ValueError('synthetic PostgreSQL startup failed: ' + json.dumps(state, sort_keys=True))


def main():
    if os.geteuid() != 0:
        raise ValueError('native client fixture requires its disposable privileged runner')
    nonce = secrets.token_hex(16)
    names = {name: 'kmz-client-' + name + '-' + nonce for name in ['server', 'same', 'other']}
    volume = 'kmz-client-data-' + nonce
    for name in names.values():
        if present('container', name):
            raise ValueError('native fixture container intent is not fresh')
    if present('volume', volume):
        raise ValueError('native fixture volume intent is not fresh')
    run(['pull', IMAGE], timeout=180)
    image = json.loads(run(['image', 'inspect', IMAGE]).stdout)[0]['Id'].removeprefix('sha256:')
    if not re.fullmatch('[a-f0-9]{64}', image):
        raise ValueError('native fixture image omitted its immutable configuration')
    with tempfile.TemporaryDirectory(prefix='postgres-client-native-') as temporary:
        directory = Path(temporary)
        auth = directory / 'postgres.env'
        auth.write_text('POSTGRES_USER=probe_owner\nPOSTGRES_DB=probe\nPOSTGRES_PASSWORD=synthetic-only-fixture\n')
        auth.chmod(0o600)
        loaded = False
        try:
            if (hashlib.sha256(PROFILE.read_bytes()).hexdigest() != PROFILE_SHA256
                    or any(line.startswith('komizo-workload ') for line in PROFILES.read_text().splitlines())):
                raise ValueError('native fixture refuses a changed or already installed workload profile')
            subprocess.run(['/usr/sbin/apparmor_parser', '-r', str(PROFILE)], check=True, timeout=30,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENVIRONMENT)
            loaded = True
            if 'komizo-workload (enforce)' not in PROFILES.read_text().splitlines():
                raise ValueError('the reviewed native workload profile is not enforcing')
            run(['volume', 'create', '--label', LABEL + '=' + nonce, volume])
            owned = json.loads(run(['volume', 'inspect', volume]).stdout)[0]
            data = Path('/var/lib/containers/storage/volumes') / volume / '_data'
            if (owned['Name'] != volume or owned['Labels'].get(LABEL) != nonce or owned['Mountpoint'] != str(data)
                    or any(path.is_symlink() for path in [data, *data.parents]) or any(data.iterdir())):
                raise ValueError('native PostgreSQL fixture storage is not newly owned and empty')
            os.chown(data, 999, 999)
            data.chmod(0o700)
            run(['run', '--detach', '--name', names['server'], '--label', LABEL + '=' + nonce,
                '--pull=never', '--image-volume=ignore', '--read-only', '--read-only-tmpfs=false',
                '--cap-drop=ALL', '--user=999:999',
                '--security-opt=no-new-privileges', '--security-opt=apparmor=komizo-workload', '--http-proxy=false', '--network=none',
                '--memory=268435456', '--memory-swap=268435456', '--cpus=1', '--pids-limit=64',
                '--timeout=180', '--env-file', str(auth), '--mount', 'type=volume,source=' + volume + ',target=/var/lib/postgresql',
                '--tmpfs', '/var/run/postgresql:rw,nosuid,nodev,noexec,size=16m,mode=1777',
                '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777', 'sha256:' + image,
                'postgres', '-c', 'unix_socket_directories=/var/run/postgresql,@/komizo-capture',
                '-c', 'shared_buffers=8MB', '-c', 'max_connections=8', '-c', 'work_mem=1MB',
                '-c', 'maintenance_work_mem=16MB'], timeout=60)
            deadline = time.monotonic() + 45
            while True:
                readiness = run(['exec', names['server'], 'pg_isready', '-h', '127.0.0.1', '-U', 'probe_owner', '-d', 'probe'], check=False)
                if not readiness.returncode:
                    break
                if time.monotonic() >= deadline:
                    server_failure(names['server'], readiness)
                time.sleep(.1)
            server = json.loads(run(['container', 'inspect', names['server']]).stdout)[0]
            if (server['Name'].lstrip('/') != names['server'] or server['Config']['Labels'].get(LABEL) != nonce
                    or server['Image'].removeprefix('sha256:') != image or not re.fullmatch('[a-f0-9]{64}', server['Id'])):
                raise ValueError('native server does not match its exact owned intent')
            for selected, network in [('same', 'container:' + server['Id']), ('other', 'none')]:
                result = run(['run', '--name', names[selected], '--label', LABEL + '=' + nonce,
                    '--pull=never', '--image-volume=ignore', '--read-only', '--read-only-tmpfs=false',
                    '--cap-drop=ALL', '--security-opt=no-new-privileges', '--security-opt=apparmor=komizo-workload', '--http-proxy=false',
                    '--user=65534:65534', '--network=' + network, '--memory=67108864', '--memory-swap=67108864',
                    '--cpus=.25', '--pids-limit=32', '--timeout=10', '--env=PGCONNECT_TIMEOUT=3',
                    '--entrypoint=psql', 'sha256:' + image, '-w', '-XAtq', '-h', '@/komizo-capture',
                    '-U', 'probe_owner', '-d', 'probe', '-c', 'SELECT current_database()'], check=False)
                if selected == 'same' and (result.returncode or result.stdout.strip() != b'probe'):
                    raise ValueError('libpq could not use the actual same-namespace abstract socket: ' + result.stderr[:8192].decode(errors='replace'))
                if selected == 'other' and not result.returncode:
                    raise ValueError('another network namespace reached the PostgreSQL abstract socket')
            print('Actual PostgreSQL18.6/libpq abstract socket connected only within the serving network namespace; clients had no data mounts')
        finally:
            for name in reversed(list(names.values())):
                if present('container', name):
                    value = json.loads(run(['container', 'inspect', name]).stdout)[0]
                    if (value['Name'].lstrip('/') != name or value['Config']['Labels'].get(LABEL) != nonce
                            or value['Image'].removeprefix('sha256:') != image):
                        raise ValueError('native client fixture ownership changed before cleanup')
                    run(['rm', '--force', '--volumes', value['Id']])
            if present('volume', volume):
                value = json.loads(run(['volume', 'inspect', volume]).stdout)[0]
                if value['Name'] != volume or value['Labels'].get(LABEL) != nonce:
                    raise ValueError('native client fixture volume changed before cleanup')
                run(['volume', 'rm', volume])
            if loaded:
                subprocess.run(['/usr/sbin/apparmor_parser', '-R', str(PROFILE)], check=True, timeout=30,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENVIRONMENT)


if __name__ == '__main__':
    main()
