"""Verify the actual pinned PostgreSQL/libpq namespace boundary on Linux."""
import argparse
import json
import hashlib
import os
from pathlib import Path
import re
import secrets
import selectors
import sys
import threading
import types
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



def helpers():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cookbook'))
    import capture_runtime
    import postgres_client
    return capture_runtime, postgres_client


def fixture_resources(record, config):
    # Explicit reviewed fixture configuration, never an inherited engine selector.
    # Production installed_resource.environment continues to remove selectors.
    if (config.read_bytes()!=b'[containers]\napparmor_profile=\"komizo-workload\"\n'
            or config.is_symlink() or config.stat().st_uid!=0 or config.stat().st_mode & 0o077):
        raise ValueError('fixture AppArmor default configuration changed')
    def validate(value, application, service):
        if value != record or application != 'client-fixture' or service != 'postgres':
            raise ValueError('leased client lost its exact synthetic resource scope')
        release = record['deployment']['release']
        digest = 'sha256:' + hashlib.sha256(json.dumps(release,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if record['release_digest'] != digest:
            raise ValueError('synthetic fixture descriptor changed')
        return value
    def inspect(value, raw):
        validate(value,'client-fixture','postgres')
        values=json.loads(raw)
        if (not isinstance(values,list) or len(values)!=1 or values[0]['Id']!=record['container_id']
                or values[0]['Image'].removeprefix('sha256:')!=record['image_config_digest'].removeprefix('sha256:')
                or values[0]['Config']['Labels'].get(LABEL)!=record['nonce']
                or values[0]['Config']['User']!='999:999' or values[0]['State']['Running'] is not True):
            raise ValueError('leased client server differs from the actual owned PostgreSQL process')
        return values[0]
    return types.SimpleNamespace(validate=validate,inspect=inspect,
        environment=lambda:{**ENVIRONMENT,'CONTAINERS_CONF':str(config)})


def process_evidence(container, task_path):
    value=json.loads(run(['container','inspect',container]).stdout)[0]
    init=value['State']['Pid']
    rows=Path(f'/proc/{init}/cgroup').read_text().splitlines()
    if len(rows)!=1 or not rows[0].startswith('0::/'):
        raise ValueError('leased helper omitted unified cgroup identity')
    group=Path('/sys/fs/cgroup')/rows[0][3:].lstrip('/')
    if not group.is_relative_to(task_path) or container not in group.name:
        raise ValueError('leased helper escaped the exact protected task')
    if any(m.get('Type') in {'bind','volume'} for m in value.get('Mounts',[])):
        raise ValueError('PostgreSQL client acquired a retained data or staging mount')
    facts=[]
    for path in group.rglob('cgroup.procs'):
        for pid in path.read_text().split():
            try:
                root=Path('/proc')/pid
                comm=(root/'comm').read_text().strip()
                if comm not in {'psql','pg_dump'}:
                    continue
                status=dict(line.split(':',1) for line in (root/'status').read_text().splitlines() if ':' in line)
                membership=(root/'cgroup').read_text().strip()
                profile=(root/'attr/current').read_text().strip()
                approved={'komizo-workload (enforce)'}|{left+'//&'+right+' ('+mode+')'
                    for left,right in [('komizo-workload','crun'),('crun','komizo-workload')]
                    for mode in ['enforce','mixed']}
                observed={key:status[key].strip() for key in ['Uid','Gid','NoNewPrivs','CapInh','CapPrm','CapEff','CapBnd','CapAmb']}
                observed.update({'profile':profile,'membership':membership,'helper_group':str(group),'pid':pid,'program':comm})
                if (set(status['Uid'].split())!={'65534'} or set(status['Gid'].split())!={'65534'}
                        or status['NoNewPrivs'].strip()!='1' or not membership.startswith('0::/')
                        or not (Path('/sys/fs/cgroup')/membership[3:].lstrip('/')).is_relative_to(group)
                        or any(int(status[key].strip(),16) for key in ['CapInh','CapPrm','CapEff','CapBnd','CapAmb'])
                        or profile not in approved or 'komizo-workload (enforce)' not in PROFILES.read_text().splitlines()):
                    raise ValueError('actual libpq client escaped its exact boundary: '+json.dumps(observed,sort_keys=True))
                start=(root/'stat').read_text().rsplit(')',1)[1].split()[19]
                facts.append({'pid':int(pid),'start':start,'program':comm,'cgroup':membership[3:]})
            except FileNotFoundError:
                continue
    return facts


def worker(mode, stage):
    runtime,pg=helpers()
    # The generic runtime suppresses failures that could contain product values.
    # This dedicated synthetic fixture has no credentials, so retain bounded
    # native stderr once without replaying a failed create/start/exec.
    original_run=subprocess.run
    def diagnostic_run(arguments,*args,**kwargs):
        result=original_run(arguments,*args,**kwargs)
        errors=kwargs.get('stderr')
        if result.returncode and arguments[:1]==['/usr/bin/podman'] and hasattr(errors,'seek'):
            position=errors.tell();errors.seek(0);detail=errors.read(8192);errors.seek(position)
            print('Synthetic native client failure: '+detail.decode(errors='replace'),file=sys.stderr,flush=True)
        return result
    subprocess.run=diagnostic_run
    pg.runtime_module=lambda:runtime
    record=json.loads((stage/'resource.json').read_bytes())
    resources=fixture_resources(record,stage.parent/'containers.conf')
    if mode=='reconcile':
        journal=runtime.PRODUCTION_JOURNAL/os.environ['KOMIZO_OPERATION_ID']/'capture-helpers'
        if journal.exists():
            runtime.cleanup_leases(resources,journal,inspect_only=True)
            runtime.cleanup_leases(resources,journal)
        return
    task_path=runtime.production_control(os.environ['KOMIZO_TASK_OPERATION_ID'])
    expected={'memory.max':str(384<<20),'memory.swap.max':'0','pids.max':'128'}
    if any((task_path/name).read_text().strip()!=value for name,value in expected.items()):
        raise ValueError('actual task kernel limits differ from declared client proof')
    quota,period=map(int,(task_path/'cpu.max').read_text().split())
    if quota*2!=period:
        raise ValueError('actual task CPU quota differs from declared client proof')
    with pg.context(resources,record,stage,database='probe',role='probe_owner',timeout=120) as client:
        if not re.fullmatch(r'pg_dump \(PostgreSQL\) 18\.6(?: .*)?',client.version()):
            raise ValueError('leased client did not use the exact pinned PostgreSQL tool')
        with client.session() as session:
            session.begin()
            snapshot=session.export_snapshot()
            if mode=='positive':
                if session.query('SELECT count(*) FROM capture_probe')!='2':
                    raise ValueError('leased snapshot did not read actual synthetic rows')
                client.dump(snapshot,stage/'complete.dump')
                if not (stage/'complete.dump').read_bytes().startswith(b'PGDMP'):
                    raise ValueError('leased pg_dump did not emit a real custom archive')
                journal=runtime.PRODUCTION_JOURNAL/os.environ['KOMIZO_OPERATION_ID']/'capture-helpers'
                outer=next(path for path in journal.glob('*.json')
                    if json.loads(path.read_bytes())['container_id']==client.container_id)
                before=outer.read_bytes();media_helpers=[]
                for interrupted in [False,True]:
                    try:
                        with runtime.container(resources,record,stage,user='65534:65534',
                                mounts={'media':'/media'},entrypoint='/bin/sleep',command=['60'],timeout=60) as media:
                            media_helpers.append(media)
                            if runtime.run(resources,['exec',media,'/bin/cat','/media/probe'],timeout=15)!=b'synthetic media\n':
                                raise ValueError('nested media helper did not read its exact retained fixture')
                            if interrupted:raise RuntimeError('controlled media interruption')
                    except RuntimeError as error:
                        if not interrupted or str(error)!='controlled media interruption':raise
                    if present('container',media) or outer.read_bytes()!=before:
                        raise ValueError('inner media exit changed the live outer PostgreSQL lease')
                    if session.query('SELECT count(*) FROM capture_probe')!='2':
                        raise ValueError('inner media exit interrupted the actual outer snapshot session')
                    session.verify_snapshot()
                session.commit()
                runtime.atomic(stage/'completed.json',{'snapshot':snapshot,'marker':client.application_name,
                    'helper':client.container_id,'server':record['container_id'],'media_helpers':media_helpers})
                return
            runtime.atomic(stage/'snapshot.json',{'snapshot':snapshot,'marker':client.application_name,
                'helper':client.container_id,'task_path':str(task_path)})
            errors=[]
            def dump():
                try:client.dump(snapshot,stage/'blocked.dump')
                except BaseException as error:errors.append(type(error).__name__)
            thread=threading.Thread(target=dump,daemon=True)
            thread.start()
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                facts=process_evidence(client.container_id,task_path)
                if {item['program'] for item in facts}=={'psql','pg_dump'}:
                    runtime.atomic(stage/'clients.json',{'helper':client.container_id,'marker':client.application_name,
                        'task_path':str(task_path),'processes':facts})
                    break
                if not thread.is_alive():
                    raise ValueError('dump did not remain behind its independently owned table lock: '+str(errors))
                time.sleep(.02)
            else:raise TimeoutError('actual psql/pg_dump kernel containment was not observed')
            # Parent kills this exact unit while both clients are alive. A normal
            # return would falsely claim that interrupted capture completed.
            thread.join(timeout=60)
            raise ValueError('blocked dump unexpectedly outlived the parent kill control')


def launch_worker(mode, stage, operation, task, output):
    unit=('komizo-reconcile-' if mode=='reconcile' else 'komizo-backup-')+task+'.service'
    command=['systemd-run','--unit',unit,'--wait','--pipe','--collect','-p','Delegate=yes',
        '-p','DelegateSubgroup=runner','-p','UMask=0077','-p','MemoryMax=384M','-p','MemorySwapMax=0',
        '-p','CPUQuota=50%','-p','TasksMax=128','-p','RuntimeMaxSec=180','-p','KillMode=control-group',
        '--setenv=KOMIZO_OPERATION_ID='+operation,'--setenv=KOMIZO_TASK_OPERATION_ID='+task,
        '/usr/bin/python3','-I','-B','-S',str(Path(__file__).resolve()),'--worker',mode,'--stage',str(stage)]
    process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,env=ENVIRONMENT)
    return unit,process


def wait_file(path,process,output,seconds=30):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        if path.is_file():
            return json.loads(path.read_bytes())
        if process.poll() is not None:
            output.seek(0)
            raise ValueError('leased capture task exited before its required real evidence: '+output.read(16384).decode(errors='replace'))
        time.sleep(.025)
    raise TimeoutError('leased capture task omitted its bounded evidence')


def wait_task(process,output,*,killed=False):
    result=process.wait(timeout=60)
    if bool(result)!=killed:
        output.seek(0)
        raise ValueError('unexpected leased-task result: '+output.read(16384).decode(errors='replace'))


def query(server,sql):
    result=run(['exec',server,'psql','-w','-XAtq','-v','ON_ERROR_STOP=1','-U','probe_owner','-d','probe','-c',sql])
    return result.stdout.decode().strip()


def leased_controls(directory,server,image,nonce,media_volume):
    runtime,_=helpers()
    (directory/'containers.conf').write_text('[containers]\napparmor_profile="komizo-workload"\n')
    (directory/'containers.conf').chmod(0o600)
    release={'artifacts':{'postgres':{'kind':'image','config_digest':'sha256:'+image}},
        'workload':{'services':{'postgres':{'artifact':'postgres','user':'999:999',
        'command':['postgres','-c','unix_socket_directories=/var/run/postgresql,@/komizo-capture']}},'volumes':{'media':{}}}}
    media_fields={key:media_volume.get(key) for key in ['Name','CreatedAt','Driver','Mountpoint','Options']}
    record={'application':'client-fixture','service':'postgres','container_id':server['Id'],'nonce':nonce,
        'image_config_digest':'sha256:'+image,'release_digest':'sha256:'+hashlib.sha256(runtime.canonical(release)).hexdigest(),
        'deployment':{'release':release},'volumes':{'media':{'name':media_volume['Name'],
            'id':'sha256:'+hashlib.sha256(runtime.canonical(media_fields)).hexdigest()}}}
    original=(server['Id'],server['State']['Pid'],Path(f"/proc/{server['State']['Pid']}/stat").read_text().rsplit(')',1)[1].split()[19])
    query(server['Id'],'CREATE TABLE capture_probe(id integer primary key); INSERT INTO capture_probe VALUES(1),(2)')
    operations=[];units=[]
    blocker=None
    try:
        for mode in ['positive','blocked']:
            stage=directory/mode;stage.mkdir(mode=0o700)
            (stage/'resource.json').write_bytes(runtime.canonical(record));(stage/'resource.json').chmod(0o600)
            operation,task=secrets.token_hex(16),secrets.token_hex(16)
            operations.append((operation,stage))
            if (runtime.PRODUCTION_JOURNAL/operation).exists():
                raise ValueError('native client operation journal is not fresh')
            if mode=='blocked':
                blocker=subprocess.Popen(['/usr/bin/podman','--remote=false','exec','--interactive',server['Id'],
                    'psql','-w','-XAtq','-v','ON_ERROR_STOP=1','-U','probe_owner','-d','probe'],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=ENVIRONMENT)
                blocker.stdin.write(b"BEGIN; LOCK TABLE capture_probe IN ACCESS EXCLUSIVE MODE; SELECT 'locked';\n")
                blocker.stdin.flush()
                with selectors.DefaultSelector() as selected:
                    selected.register(blocker.stdout,selectors.EVENT_READ)
                    if not selected.select(15) or blocker.stdout.readline().strip()!=b'locked':
                        raise ValueError('independent owner did not hold the actual table lock')
            with tempfile.TemporaryFile() as output:
                unit,process=launch_worker(mode,stage,operation,task,output);units.append(unit)
                if mode=='positive':
                    wait_task(process,output)
                    completed=json.loads((stage/'completed.json').read_bytes())
                    if completed['server']!=server['Id'] or present('container',completed['helper']):
                        raise ValueError('normal leased capture did not clean up its exact helper')
                    if (len(set(completed['media_helpers']))!=2
                            or any(present('container',identity) for identity in completed['media_helpers'])):
                        raise ValueError('nested media helper cleanup did not retain exact lifetime evidence')
                    continue
                facts=wait_file(stage/'clients.json',process,output)
                marker=facts['marker']
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    blocked=query(server['Id'],"SELECT count(*) FROM pg_stat_activity WHERE application_name='"+marker+"' AND wait_event_type='Lock'")
                    if blocked=='1':break
                    time.sleep(.025)
                else:raise ValueError('dump was not actually blocked by the independent table owner')
                state=subprocess.check_output(['systemctl','show',unit,'-p','ControlGroup','--value'],env=ENVIRONMENT,text=True,timeout=15).strip()
                if Path('/sys/fs/cgroup'+state)!=Path(facts['task_path']):
                    raise ValueError('kill target differs from observed exact task cgroup')
                subprocess.run(['systemctl','kill','--kill-whom=all','--signal=KILL',unit],check=True,timeout=15,env=ENVIRONMENT)
                wait_task(process,output,killed=True)
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    if query(server['Id'],"SELECT count(*) FROM pg_stat_activity WHERE application_name='"+marker+"'")=='0':break
                    time.sleep(.025)
                else:raise ValueError('disconnected capture backend survived the hard task kill')
                deadline=time.monotonic()+5
                while True:
                    remaining=[]
                    for item in facts['processes']:
                        path=Path('/proc')/str(item['pid'])/'stat'
                        try:
                            if path.read_text().rsplit(')',1)[1].split()[19]==item['start']:remaining.append(item)
                        except FileNotFoundError:pass
                    if not remaining:break
                    if time.monotonic()>=deadline:raise ValueError('actual libpq executable survived its task cgroup kill')
                    time.sleep(.025)
                before=(stage/'blocked.dump').stat().st_size
                blocker.stdin.write(b'ROLLBACK;\n');blocker.stdin.close();blocker.wait(timeout=10);blocker=None
                time.sleep(.3)
                if (stage/'blocked.dump').stat().st_size!=before or (stage/'completed.json').exists():
                    raise ValueError('aborted dump wrote late bytes or claimed capture completion')
                now=json.loads(run(['container','inspect',server['Id']]).stdout)[0]
                kernel=Path(f"/proc/{original[1]}/stat")
                if (now['Id'],now['State']['Pid'],kernel.read_text().rsplit(')',1)[1].split()[19])!=original or not now['State']['Running']:
                    raise ValueError('hard capture kill changed the PostgreSQL server identity')
                if query(server['Id'],'SELECT count(*) FROM capture_probe')!='2':
                    raise ValueError('hard capture kill changed retained PostgreSQL rows')
    finally:
        if blocker is not None:
            try:
                blocker.stdin.write(b'ROLLBACK;\n');blocker.stdin.close();blocker.wait(timeout=5)
            except (BrokenPipeError,OSError,subprocess.TimeoutExpired):
                blocker.kill();blocker.wait(timeout=5)
        for unit in units:
            subprocess.run(['systemctl','stop',unit],env=ENVIRONMENT,check=False,timeout=30,
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for operation,stage in operations:
            with tempfile.TemporaryFile() as output:
                unit,process=launch_worker('reconcile',stage,operation,secrets.token_hex(16),output)
                wait_task(process,output)
            journal=runtime.PRODUCTION_JOURNAL/operation/'capture-helpers'
            if journal.exists():
                for path in journal.iterdir():
                    lease=runtime.validate_lease(json.loads(runtime.regular(path)))
                    if lease['application']!='client-fixture' or lease['operation_id']!=operation or lease['phase']!='cleaned' or present('container',lease['container_id'] or lease['name']):
                        raise ValueError('reconciliation left an unresolved or foreign capture lease')
                    path.unlink()
                journal.rmdir();journal.parent.rmdir()
    print('Nested read-only media helpers preserved the live PG snapshot on normal/error exit; explicit recovery and hard kill removed owned clients without PG restart, late writes or data changes')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker',choices=['positive','blocked','reconcile'])
    parser.add_argument('--stage',type=Path)
    args=parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('native client fixture requires its disposable privileged runner')
    if args.worker:
        worker(args.worker,args.stage)
        return
    nonce = secrets.token_hex(16)
    names = {name: 'kmz-client-' + name + '-' + nonce for name in ['server', 'same', 'other', 'foreign']}
    volume = 'kmz-client-data-' + nonce
    media = 'kmz-client-media-' + nonce
    for name in names.values():
        if present('container', name):
            raise ValueError('native fixture container intent is not fresh')
    for selected in [volume,media]:
        if present('volume', selected):raise ValueError('native fixture volume intent is not fresh')
    run(['pull', IMAGE], timeout=180)
    image_record=json.loads(run(['image','inspect',IMAGE]).stdout)[0]
    image=image_record['Id'].removeprefix('sha256:')
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
            run(['volume','create','--label',LABEL+'='+nonce,media])
            media_volume=json.loads(run(['volume','inspect',media]).stdout)[0]
            media_data=Path('/var/lib/containers/storage/volumes')/media/'_data'
            if (media_volume['Name']!=media or media_volume['Labels'].get(LABEL)!=nonce
                    or media_volume['Mountpoint']!=str(media_data)
                    or any(path.is_symlink() for path in [media_data,*media_data.parents]) or any(media_data.iterdir())):
                raise ValueError('native media fixture storage is not newly owned and empty')
            media_data.chmod(0o755)
            (media_data/'probe').write_bytes(b'synthetic media\n');(media_data/'probe').chmod(0o444)
            run(['run', '--detach', '--name', names['server'], '--label', LABEL + '=' + nonce,
                '--pull=never', '--image-volume=ignore', '--read-only', '--read-only-tmpfs=false',
                '--cap-drop=ALL', '--user=999:999',
                '--security-opt=no-new-privileges', '--security-opt=apparmor=komizo-workload', '--http-proxy=false', '--network=none',
                '--memory=268435456', '--memory-swap=268435456', '--cpus=1', '--pids-limit=64',
                '--timeout=600', '--env-file', str(auth), '--mount', 'type=volume,source=' + volume + ',target=/var/lib/postgresql',
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
            run(['create','--name',names['foreign'],'--label',LABEL+'='+nonce,'--pull=never',
                '--image-volume=ignore','--entrypoint','/bin/sleep','sha256:'+image,'1'])
            foreign=json.loads(run(['container','inspect',names['foreign']]).stdout)[0]
            leased_controls(directory,server,image,nonce,media_volume)
            if (media_data/'probe').read_bytes()!=b'synthetic media\n' or set(path.name for path in media_data.iterdir())!={'probe'}:
                raise ValueError('nested capture changed the retained synthetic media bytes')
            retained=json.loads(run(['container','inspect',names['foreign']]).stdout)[0]
            if any(retained[key]!=foreign[key] for key in ['Id','Image','Name','State']):
                raise ValueError('leased task cleanup changed an unrelated stopped fixture')
            after=json.loads(run(['image','inspect',IMAGE]).stdout)[0]
            if any(after.get(key)!=image_record.get(key) for key in ['Id','RepoTags','RepoDigests','RootFS']):
                raise ValueError('leased task cleanup changed borrowed image identity or aliases')
            print('Actual PostgreSQL18.6/libpq abstract socket connected only within the serving network namespace; clients had no data mounts')
        finally:
            for name in reversed(list(names.values())):
                if present('container', name):
                    value = json.loads(run(['container', 'inspect', name]).stdout)[0]
                    if (value['Name'].lstrip('/') != name or value['Config']['Labels'].get(LABEL) != nonce
                            or value['Image'].removeprefix('sha256:') != image):
                        raise ValueError('native client fixture ownership changed before cleanup')
                    run(['rm', '--force', '--volumes', value['Id']])
            for selected in [media,volume]:
                if present('volume', selected):
                    value = json.loads(run(['volume', 'inspect', selected]).stdout)[0]
                    if value['Name'] != selected or value['Labels'].get(LABEL) != nonce:
                        raise ValueError('native client fixture volume changed before cleanup')
                    run(['volume', 'rm', selected])
            if loaded:
                subprocess.run(['/usr/sbin/apparmor_parser', '-R', str(PROFILE)], check=True, timeout=30,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENVIRONMENT)


if __name__ == '__main__':
    main()
