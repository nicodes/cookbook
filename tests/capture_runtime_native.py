"""Exercise capture ownership, actual kernel bounds and orphan cleanup in CI."""
import argparse
import copy
import errno
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import types


def engine(arguments):
    result = subprocess.run(['/usr/bin/podman', '--remote=false', *arguments],
        env={'PATH': '/usr/bin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8'},
        check=True, capture_output=True, timeout=60)
    return result.stdout


def resources():
    def validate(record, application, service):
        if application != 'capture-fixture' or service != 'postgres' or record['application'] != application:
            raise ValueError('native fixture resource scope differs')
        return record
    return types.SimpleNamespace(validate=validate,
        environment=lambda: {'PATH': '/usr/bin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--caddy', type=Path)
    parser.add_argument('--orphan', type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('native capture acceptance requires its disposable privileged unit')
    sys.path.insert(0, str(args.root / 'cookbook'))
    import capture_runtime as runtime
    selected_resources = resources()
    if args.orphan:
        selected = json.loads(args.orphan.read_bytes())
        stage = args.orphan.parent
        with runtime.container(selected_resources, selected, stage, user='65534:65534',
                entrypoint='/operator', command=['run', '--config', '/run/Caddyfile', '--adapter', 'caddyfile'],
                binds=[{'source': stage / 'Caddyfile', 'target': '/run/Caddyfile', 'read_only': True}],
                control_cgroup=runtime.fixture_control(stage), timeout=45) as identity:
            print(identity, flush=True)
            os._exit(73)
    if not args.caddy or args.caddy.is_symlink() or not args.caddy.is_file():
        raise ValueError('native fixture requires the already verified static proxy binary')
    nonce = secrets.token_hex(16)
    tag = 'localhost/capture-fixture:' + nonce
    volume = 'capture-fixture-' + nonce
    foreign = None
    image_created = volume_created = False
    with tempfile.TemporaryDirectory(prefix='capture-native-') as temporary:
        root = Path(temporary)
        stage = root / '.export-fixture'; stage.mkdir(mode=0o700)
        selector = runtime.fixture_control(stage)
        (stage / 'Caddyfile').write_text('{\n admin off\n auto_https off\n}\n:8080 {\n respond "fixture"\n}\n')
        (root / 'Containerfile').write_text('FROM scratch\nCOPY operator /operator\nLABEL io.komizo.capture-fixture="' + nonce + '"\n')
        shutil.copyfile(args.caddy, root / 'operator'); (root / 'operator').chmod(0o555)
        record = None
        try:
            engine(['build', '--format', 'oci', '--pull=never', '--tag', tag, str(root)])
            image_created = True
            inspected = json.loads(engine(['image', 'inspect', tag]))[0]
            config = inspected['Id']; config = config if config.startswith('sha256:') else 'sha256:' + config
            engine(['volume', 'create', '--label', 'io.komizo.capture-fixture=' + nonce, volume]); volume_created = True
            actual_volume = json.loads(engine(['volume', 'inspect', volume]))[0]
            volume_root = Path(actual_volume['Mountpoint']); (volume_root / 'original').write_bytes(b'owned retained bytes')
            volume_fields = {key: actual_volume.get(key) for key in ['Name', 'CreatedAt', 'Driver', 'Mountpoint', 'Options']}
            fingerprint = 'sha256:' + hashlib.sha256(runtime.canonical(volume_fields)).hexdigest()
            record = {'application': 'capture-fixture', 'service': 'postgres', 'container_id': 'e' * 64,
                'release_digest': 'sha256:' + 'a' * 64, 'volumes': {'files': {'name': volume, 'id': fingerprint}},
                'deployment': {'release': {'workload': {'volumes': {'files': {}}},
                    'artifacts': {'postgres': {'kind': 'image', 'config_digest': config}}}}}
            foreign = engine(['create', '--name', 'capture-preserve-' + nonce, '--pull=never', '--image-volume=ignore',
                '--label', 'io.komizo.capture-fixture=' + nonce, '--entrypoint', '/operator', config, 'version']).decode().strip()
            with runtime.container(selected_resources, record, stage, user='65534:65534', mounts={'files': '/blobs'},
                    entrypoint='/operator', command=['run', '--config', '/run/Caddyfile', '--adapter', 'caddyfile'],
                    binds=[{'source': stage / 'Caddyfile', 'target': '/run/Caddyfile', 'read_only': True}],
                    control_cgroup=selector, timeout=45) as identity:
                observed = json.loads(engine(['container', 'inspect', identity]))[0]
                if observed['State']['Running'] is not True:
                    raise ValueError('native helper exited before kernel evidence was observed')
                pid = observed['State']['Pid']
                status = dict(line.split(':', 1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)
                if set(map(int, status['Uid'].split())) != {65534} or int(status['CapEff'].strip(), 16) != 0:
                    raise ValueError('actual capture UID or effective capabilities differ')
                retained = Path(f'/proc/{pid}/root/blobs/original')
                if retained.read_bytes() != b'owned retained bytes':
                    raise ValueError('native helper did not receive the exact retained volume')
                try:
                    retained.write_bytes(b'unexpected mutation')
                except OSError as error:
                    if error.errno != errno.EROFS:
                        raise
                else:
                    raise ValueError('actual native capture mount permitted a retained-data write')
            if engine(['container', 'exists', foreign]) != b'':
                raise ValueError('unrelated fixture identity unexpectedly changed')
            if runtime.exists(selected_resources, identity):
                raise ValueError('completed native helper was not removed')
            wrong = copy.deepcopy(record); wrong['volumes']['files']['id'] = 'sha256:' + 'f' * 64
            try:
                with runtime.container(selected_resources, wrong, stage, user='65534:65534', mounts={'files': '/blobs'},
                        control_cgroup=selector):
                    raise AssertionError('changed retained-volume fingerprint was admitted')
            except ValueError:
                pass
            document = stage / 'resource.json'; document.write_bytes(runtime.canonical(record))
            orphan = subprocess.run([sys.executable, '-I', '-B', '-S', str(Path(__file__).resolve()), '--root', str(args.root),
                '--orphan', str(document)], capture_output=True, timeout=60)
            if orphan.returncode != 73 or not runtime.ID.fullmatch(orphan.stdout.decode().strip()):
                raise ValueError('interrupted native helper did not retain its durable identity')
            orphan_id = orphan.stdout.decode().strip()
            before = runtime.cleanup_leases(selected_resources, selector['leases'], inspect_only=True)
            if not any(item['container_id'] == orphan_id and item['running'] for item in before):
                raise ValueError('native helper did not survive its capture process for reconciliation')
            runtime.cleanup_leases(selected_resources, selector['leases'])
            if runtime.exists(selected_resources, orphan_id) or (volume_root / 'original').read_bytes() != b'owned retained bytes':
                raise ValueError('orphan cleanup changed retained bytes or left its helper')
            print('Native capture enforced actual finite cgroups, UID/capabilities, read-only retained data and durable orphan cleanup')
        finally:
            runtime.cleanup_leases(selected_resources, selector['leases'])
            if foreign:
                observed = json.loads(engine(['container', 'inspect', foreign]))[0]
                if observed['Id'] != foreign or observed['Config']['Labels'].get('io.komizo.capture-fixture') != nonce:
                    raise ValueError('unrelated fixture ownership changed before cleanup')
                engine(['rm', foreign])
            if volume_created:
                observed = json.loads(engine(['volume', 'inspect', volume]))[0]
                if observed['Name'] != volume or observed['Labels'].get('io.komizo.capture-fixture') != nonce:
                    raise ValueError('native fixture volume ownership changed before cleanup')
                engine(['volume', 'rm', volume])
            if image_created:
                observed = json.loads(engine(['image', 'inspect', tag]))[0]
                if observed['Config']['Labels'].get('io.komizo.capture-fixture') != nonce:
                    raise ValueError('native fixture image ownership changed before cleanup')
                engine(['image', 'rm', tag])


if __name__ == '__main__':
    main()
