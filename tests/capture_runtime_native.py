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
import socket
import subprocess
import sys
import tempfile
import time
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


def unix_response(path):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
        connection.settimeout(3);connection.connect(str(path))
        connection.sendall(b'GET / HTTP/1.0\r\nHost: localhost\r\n\r\n')
        body=b''
        while len(body)<8192:
            block=connection.recv(8192-len(body))
            if not block:break
            body+=block
    if b'owned-socket' not in body:raise ValueError('native origin socket returned another identity')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--caddy', type=Path)
    parser.add_argument('--orphan', type=Path)
    parser.add_argument('--created', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('native capture acceptance requires its disposable privileged unit')
    sys.path.insert(0, str(args.root / 'cookbook'))
    import capture_runtime as runtime
    selected_resources = resources()
    if args.orphan:
        selected = json.loads(args.orphan.read_bytes())
        stage = args.orphan.parent
        paths={'/var/run/postgresql':'/run/capture/postgres'} if selected.get('image_config_digest') else None
        with runtime.container(selected_resources, selected, stage, user='65534:65534',container_paths=paths,
                entrypoint=None if args.created else '/operator',
                command=None if args.created else ['run', '--config', '/run/Caddyfile', '--adapter', 'caddyfile'],
                binds=[{'source': stage / 'Caddyfile', 'target': '/run/Caddyfile', 'read_only': True}],
                control_cgroup=runtime.fixture_control(stage), timeout=45) as identity:
            print(identity, flush=True)
            os._exit(74 if args.created else 73)
    if not args.caddy or args.caddy.is_symlink() or not args.caddy.is_file():
        raise ValueError('native fixture requires the already verified static proxy binary')
    nonce = secrets.token_hex(16)
    tag = 'localhost/capture-fixture:' + nonce
    volume = 'capture-fixture-' + nonce
    foreign = origin = None
    image_created = volume_created = False
    with tempfile.TemporaryDirectory(prefix='capture-native-') as temporary:
        root = Path(temporary)
        stage = root / '.export-fixture'; stage.mkdir(mode=0o700)
        selector = runtime.fixture_control(stage)
        (stage / 'Caddyfile').write_text('{\n admin off\n auto_https off\n}\n:8080 {\n respond "fixture"\n}\n')
        (stage / 'origin.json').write_bytes(json.dumps({'admin':{'disabled':True},'apps':{'http':{'servers':{'origin':{
            'listen':['unix//var/run/postgresql/source.sock|0666'],'automatic_https':{'disable':True},
            'routes':[{'handle':[{'handler':'static_response','body':'owned-socket'}]}]}}}}}).encode())
        (root / 'Containerfile').write_text('FROM scratch\nCOPY operator /operator\nENTRYPOINT ["/operator"]\nLABEL io.komizo.capture-fixture="' + nonce + '"\n')
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
            origin=engine(['--cgroup-manager=cgroupfs','create','--name','capture-origin-'+nonce,'--pull=never',
                '--network=none','--read-only','--read-only-tmpfs=false','--image-volume=ignore','--http-proxy=false',
                '--cap-drop=ALL','--security-opt=no-new-privileges','--user','0:0','--cgroup-parent',selector['parent'],
                '--memory','67108864','--memory-swap','67108864','--cpus','0.25','--pids-limit','32','--timeout','90',
                '--tmpfs','/var/run/postgresql:rw,noexec,nosuid,nodev,size=1m,mode=1777',
                '--label','io.komizo.capture-fixture='+nonce,'--entrypoint','/operator',
                '--mount','type=bind,source='+str(stage/'origin.json')+',target=/run/origin.json,readonly',
                config,'run','--config','/run/origin.json']).decode().strip()
            if not runtime.ID.fullmatch(origin):raise ValueError('native socket origin omitted its complete ID')
            engine(['start',origin]);origin_info=json.loads(engine(['container','inspect',origin]))[0]
            origin_pid=origin_info['State']['Pid'];socket_path=Path(f'/proc/{origin_pid}/root/var/run/postgresql/source.sock')
            until=time.monotonic()+20
            while not socket_path.exists():
                if time.monotonic()>=until:raise ValueError('native socket origin did not become ready')
                time.sleep(.05)
            unix_response(socket_path)
            origin_record=copy.deepcopy(record)
            origin_record.update(container_id=origin,image_config_digest=config)
            origin_record['deployment']['release']['workload']['services']={'postgres':{'user':'0:0','tmpfs':{
                '/var/run/postgresql':{'size_bytes':1<<20,'mode':0o1777}}}}
            before=len(list(Path(selector['leases']).glob('*.json')))
            wrong=copy.deepcopy(origin_record);wrong['container_id']=foreign
            try:
                with runtime.container(selected_resources,wrong,stage,user='65534:65534',control_cgroup=selector,
                        container_paths={'/var/run/postgresql':'/run/capture/postgres'}):
                    raise AssertionError('stopped foreign socket origin was admitted')
            except ValueError:pass
            if len(list(Path(selector['leases']).glob('*.json')))!=before:
                raise ValueError('invalid socket origin reached helper creation')
            duplicate=None;changed_helper=None;refused=False
            try:
                with runtime.container(selected_resources,origin_record,stage,user='65534:65534',
                        entrypoint='/operator',command=['run','--config','/run/Caddyfile','--adapter','caddyfile'],
                        binds=[{'source':stage/'Caddyfile','target':'/run/Caddyfile','read_only':True}],control_cgroup=selector,
                        container_paths={'/var/run/postgresql':'/run/capture/postgres'},timeout=45) as changed_helper:
                    lease=json.loads(next(path for path in Path(selector['leases']).glob('*.json')
                        if json.loads(path.read_bytes())['container_id']==changed_helper).read_bytes())
                    bound=lease['origins'][0];duplicate=os.dup(bound['descriptor'])
                    os.chown(bound['source'],1,bound['gid'])
            except ValueError as error:
                if str(error)!='capture origin directory or owner changed':raise
                refused=True
            finally:
                if duplicate is not None:
                    try:os.chown(f'/proc/{os.getpid()}/fd/{duplicate}',0,0)
                    finally:os.close(duplicate)
            if not refused or not changed_helper or runtime.exists(selected_resources,changed_helper):
                raise ValueError('changed native socket owner was admitted or blocked owned helper cleanup')
            with runtime.container(selected_resources,origin_record,stage,user='65534:65534',
                    entrypoint='/operator',command=['run','--config','/run/Caddyfile','--adapter','caddyfile'],
                    binds=[{'source':stage/'Caddyfile','target':'/run/Caddyfile','read_only':True}],control_cgroup=selector,
                    container_paths={'/var/run/postgresql':'/run/capture/postgres'},timeout=45) as socket_helper:
                info=json.loads(engine(['container','inspect',socket_helper]))[0]
                mounted=Path(f"/proc/{info['State']['Pid']}/root/run/capture/postgres")
                unix_response(mounted/'source.sock')
                try:(mounted/'unexpected').write_bytes(b'must not mutate origin')
                except OSError as error:
                    if error.errno!=errno.EROFS:raise
                else:raise ValueError('native socket mount permitted an origin write')
            if runtime.exists(selected_resources,socket_helper):raise ValueError('socket helper survived owned cleanup')
            document.write_bytes(runtime.canonical(origin_record))
            orphan=subprocess.run([sys.executable,'-I','-B','-S',str(Path(__file__).resolve()),'--root',str(args.root),
                '--orphan',str(document)],capture_output=True,timeout=60)
            if orphan.returncode!=73 or not runtime.ID.fullmatch(orphan.stdout.decode().strip()):
                raise ValueError('socket helper did not survive its original FD owner')
            orphan_id=orphan.stdout.decode().strip()
            created=subprocess.run([sys.executable,'-I','-B','-S',str(Path(__file__).resolve()),'--root',str(args.root),
                '--orphan',str(document),'--created'],capture_output=True,timeout=60)
            if created.returncode!=74 or not runtime.ID.fullmatch(created.stdout.decode().strip()):
                raise ValueError('created socket helper did not retain its intent after FD owner exit')
            created_id=created.stdout.decode().strip()
            engine(['stop','--time','2',origin])
            observations=runtime.cleanup_leases(selected_resources,selector['leases'],inspect_only=True)
            if not any(item['container_id']==orphan_id and item['running'] for item in observations):
                raise ValueError('socket helper lost independent owned inspection after origin death')
            if not any(item['container_id']==created_id and not item['running'] for item in observations):
                raise ValueError('created socket helper became unrecoverable after origin and FD owner death')
            runtime.cleanup_leases(selected_resources,selector['leases'])
            if runtime.exists(selected_resources,orphan_id) or runtime.exists(selected_resources,created_id):
                raise ValueError('dead-origin socket helper survived reconciliation')
            try:
                with runtime.container(selected_resources,origin_record,stage,user='65534:65534',control_cgroup=selector,
                        container_paths={'/var/run/postgresql':'/run/capture/postgres'}):
                    raise AssertionError('dead native socket origin was admitted')
            except ValueError:pass
            if (volume_root/'original').read_bytes()!=b'owned retained bytes':raise ValueError('socket cleanup changed retained data')
            print('Native capture enforced finite cgroups, identity, readonly data and pinned sockets with dead-origin orphan cleanup')
        finally:
            runtime.cleanup_leases(selected_resources, selector['leases'])
            if origin:
                observed=json.loads(engine(['container','inspect',origin]))[0]
                if observed['Id']!=origin or observed['Config']['Labels'].get('io.komizo.capture-fixture')!=nonce:
                    raise ValueError('native socket origin ownership changed before cleanup')
                engine(['rm','--force',origin])
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
