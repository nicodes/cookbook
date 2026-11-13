"""Own bounded native capture helpers without depending on application liveness."""
import contextlib
import hashlib
import json
import itertools
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile
import time

ID=re.compile(r'[a-f0-9]{64}\Z');TOKEN=re.compile(r'[a-f0-9]{32}\Z')
DIGEST=re.compile(r'sha256:[a-f0-9]{64}\Z')
LIMITS={'memory_bytes':128<<20,'milli_cpus':250,'pids':64}
PRODUCTION_JOURNAL=Path('/var/lib/komizo/journal')
CGROUP_ROOT=Path('/sys/fs/cgroup')
PROC_ROOT=Path('/proc')
CONMON_UNKNOWN_EXIT='conmon died without writing exit file, container exit code could not be retrieved'


def process_identity(pid,user):
    fields=dict(line.split(':',1) for line in (PROC_ROOT/str(pid)/'status').read_text().splitlines() if ':' in line)
    uid,gid=map(int,user.split(':'))
    if (set(map(int,fields.get('Uid','').split()))!={uid}
            or set(map(int,fields.get('Gid','').split()))!={gid}
            or fields.get('NoNewPrivs','').strip()!='1'):
        raise ValueError('actual capture process identity or privilege boundary changed')
    for key in ['CapInh','CapPrm','CapEff','CapBnd','CapAmb']:
        value=fields.get(key,'').strip()
        if not re.fullmatch('[a-fA-F0-9]{1,16}',value) or int(value,16)!=0:
            raise ValueError('actual capture helper retained Linux capabilities')


def unique(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise ValueError('capture evidence repeats a field')
        value[key]=item
    return value


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def regular(path,maximum=1<<20):
    path=Path(path)
    if any(item.is_symlink() for item in [path,*path.parents]):raise ValueError('capture paths cannot cross links')
    before=path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>maximum:
        raise ValueError('capture evidence must be bounded regular bytes')
    with path.open('rb') as stream:
        body=stream.read(maximum+1);after=os.fstat(stream.fileno())
    if len(body)>maximum or any(getattr(before,key)!=getattr(after,key) for key in ['st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns']):
        raise ValueError('capture evidence changed while reading')
    return body


def private_directory(path,*,create=True):
    path=Path(path)
    if not path.is_absolute() or any(item.is_symlink() for item in [path,*path.parents]):
        raise ValueError('capture evidence requires an absolute private directory')
    if create:path.mkdir(parents=True,exist_ok=True,mode=0o700)
    info=path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077:
        raise ValueError('capture evidence directory is not privately owned')
    return path


def atomic(path,value):
    path=Path(path);private_directory(path.parent)
    if path.exists():regular(path)
    with tempfile.NamedTemporaryFile(prefix='.lease-',dir=path.parent,delete=False) as stream:
        temporary=Path(stream.name)
        try:
            os.fchmod(stream.fileno(),0o600);stream.write(canonical(value));stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,path)
            descriptor=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)
        finally:temporary.unlink(missing_ok=True)


def retained_volume(record,logical):
    declaration=record['deployment']['release']['workload'].get('volumes',{})
    value=record['volumes'].get(logical)
    if (logical not in declaration or not isinstance(value,dict) or set(value)!={'name','id'}
            or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]{0,254}',value.get('name',''))
            or not DIGEST.fullmatch(value.get('id',''))):raise ValueError('capture volume lacks protected instance ownership')
    return dict(value)


def kernel_limits(path,*,delegated=False):
    path=Path(path)
    if (not path.is_absolute() or not path.resolve().is_relative_to(CGROUP_ROOT.resolve())
            or any(item.is_symlink() for item in [path,*path.parents])):
        raise ValueError('capture control parent is outside the actual cgroup hierarchy')
    memory=(path/'memory.max').read_text().strip();pids=(path/'pids.max').read_text().strip()
    swap=(path/'memory.swap.max').read_text().strip()
    cpu=(path/'cpu.max').read_text().split()
    if (not memory.isdigit() or int(memory)<=0 or not swap.isdigit()
            or not pids.isdigit() or int(pids)<=0
            or len(cpu)!=2 or any(not value.isdigit() or int(value)<=0 for value in cpu)):
        raise ValueError('capture control parent requires finite kernel memory, CPU and PID limits')
    if delegated and not os.access(path/'cgroup.procs',os.W_OK):raise ValueError('capture fixture has no actual cgroup delegation')
    return path


def process_cgroup():
    rows=(PROC_ROOT/'self/cgroup').read_text().splitlines()
    if len(rows)!=1 or not rows[0].startswith('0::/') or len(rows[0])>16384:
        raise ValueError('capture requires actual unified cgroup evidence')
    selected=rows[0][3:]
    if any(part in {'.','..',''} for part in selected.split('/')[1:]):
        raise ValueError('capture cgroup path is not an absolute kernel identity')
    return CGROUP_ROOT/selected.lstrip('/')


def protected_owner(path):
    info=Path(path).stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
        raise ValueError('capture task cgroup is not protected root authority')


def production_control(task):
    current=process_cgroup();parent=current.parent
    if (current.name!='runner' or not re.fullmatch(r'komizo-(?:backup|prepare|reconcile)-'+task+r'\.service',parent.name)):
        raise ValueError('capture process is outside its exact protected task service')
    kernel_limits(parent,delegated=True)
    protected_owner(parent);protected_owner(current)
    if ((parent/'cgroup.procs').read_text().strip()
            or str(os.getpid()) not in (current/'cgroup.procs').read_text().split()
            or not {'cpu','memory','pids'}<=(set((parent/'cgroup.controllers').read_text().split()))
            or not os.access(parent/'cgroup.subtree_control',os.W_OK)):
        raise ValueError('capture task service lacks its protected empty delegated parent')
    return parent


def fixture_control(staging):
    """Select only an actually delegated finite fixture unit; never fake production."""
    if any(name in os.environ for name in ['KOMIZO_OPERATION_ID','KOMIZO_TASK_OPERATION_ID']):
        raise ValueError('production tasks cannot select a fixture control scope')
    path=process_cgroup()
    # A trusted disposable systemd unit uses an ordinary delegated subgroup so
    # the source process and helper are siblings under its finite delegated unit.
    if path.name=='capture-fixture':path=path.parent
    kernel_limits(path,delegated=True)
    parent='/'+path.relative_to(CGROUP_ROOT).as_posix()
    return {'parent':parent,'path':str(path),'leases':str(private_directory(Path(staging).absolute()/'helper-leases'))}


def control(selector,staging):
    operation=os.environ.get('KOMIZO_OPERATION_ID')
    task=os.environ.get('KOMIZO_TASK_OPERATION_ID')
    if operation is not None or task is not None:
        if os.geteuid()!=0 or not isinstance(operation,str) or not TOKEN.fullmatch(operation) or selector is not None:
            raise ValueError('production capture control cannot be overridden')
        if not isinstance(task,str) or not TOKEN.fullmatch(task):
            raise ValueError('production capture requires the protected supervising task operation')
        path=production_control(task)
        parent='/'+path.relative_to(CGROUP_ROOT).as_posix()
        leases=private_directory(PRODUCTION_JOURNAL/operation/'capture-helpers')
    else:
        if (not isinstance(selector,dict) or set(selector)!={'parent','path','leases'}
                or not isinstance(selector['parent'],str)
                or not re.fullmatch(r'(?:[a-zA-Z0-9_.-]+\.slice|/[a-zA-Z0-9_./-]+)',selector['parent'])
                or '..' in selector['parent'].split('/')):
            raise ValueError('capture fixture requires an explicit owned delegated control parent')
        parent=selector['parent'];path=Path(selector['path']);leases=private_directory(Path(selector['leases']))
    kernel_limits(path,delegated=operation is None)
    return parent,str(path),leases


def run(resources,arguments,*,timeout=30):
    with tempfile.TemporaryFile() as output,tempfile.TemporaryFile() as errors:
        selectors=['--cgroup-manager=cgroupfs'] if arguments and arguments[0]=='create' and '--cgroup-parent' in arguments and arguments[arguments.index('--cgroup-parent')+1].startswith('/') else []
        result=subprocess.run(['/usr/bin/podman','--remote=false',*selectors,*arguments],env=resources.environment(),
            stdin=subprocess.DEVNULL,stdout=output,stderr=errors,timeout=timeout)
        output.seek(0);body=output.read((1<<20)+1)
    if result.returncode or len(body)>1<<20:raise ValueError('bounded native capture command failed')
    return body


def _inspection_timeout(deadline):
    remaining=30 if deadline is None else min(30,deadline-time.time())
    if remaining<=0:raise TimeoutError('capture helper inspection deadline expired')
    return remaining


def verify_volumes(resources,lease,*,deadline=None):
    for mount in lease['mounts']:
        if mount['type']!='volume':continue
        values=json.loads(run(resources,['volume','inspect',mount['source']],timeout=_inspection_timeout(deadline)))
        if not isinstance(values,list) or len(values)!=1 or values[0].get('Name')!=mount['source'] or values[0].get('Driver')!='local':
            raise ValueError('capture volume has no exact native identity')
        fields={key:values[0].get(key) for key in ['Name','CreatedAt','Driver','Mountpoint','Options']}
        if 'sha256:'+hashlib.sha256(canonical(fields)).hexdigest()!=mount['id']:
            raise ValueError('capture retained volume fingerprint changed')


def inspect(resources,lease):return _inspect(resources,lease,verify_process=True)


def _inspect_native(resources,lease,*,deadline=None):
    raw=run(resources,['container','inspect',lease['container_id'] or lease['name']],timeout=_inspection_timeout(deadline))
    values=json.loads(raw)
    if not isinstance(values,list) or len(values)!=1:raise ValueError('capture helper has ambiguous native identity')
    value=values[0];labels=value.get('Config',{}).get('Labels',{});identity=value.get('Id','')
    actual=value.get('Image','');actual=actual if actual.startswith('sha256:') else 'sha256:'+actual
    if (not ID.fullmatch(identity) or lease['container_id'] not in {'',identity}
            or value.get('Name','').lstrip('/')!=lease['name'] or actual!=lease['config_digest']
            or labels.get('io.komizo.capture.ownership')!=lease['ownership']
            or labels.get('io.komizo.capture.operation')!=lease['operation_id']
            or labels.get('io.komizo.application')!=lease['application']
            or value.get('Config',{}).get('User')!=lease['user']):
        raise ValueError('capture helper differs from its durable ownership intent')
    host=value.get('HostConfig',{})
    if host.get('NetworkMode')!=lease['network']:raise ValueError('capture helper private network scope changed')
    verify_volumes(resources,lease,deadline=deadline)
    if (host.get('ReadonlyRootfs') is not True or host.get('Privileged') is True
            or host.get('Memory')!=LIMITS['memory_bytes'] or host.get('MemorySwap')!=LIMITS['memory_bytes']
            or host.get('PidsLimit')!=LIMITS['pids'] or host.get('CpuPeriod',0)<=0
            or abs(host.get('CpuQuota',0)/host['CpuPeriod']*1000-LIMITS['milli_cpus'])>0.001
            or not any('no-new-privileges' in option for option in host.get('SecurityOpt',[]))):
        raise ValueError('capture helper resource or privilege boundary changed')
    observed=sorted([(m.get('Type'),m.get('Name') if m.get('Type')=='volume' else m.get('Source'),m.get('Destination'),m.get('RW'))
        for m in value.get('Mounts',[]) if m.get('Type') in {'volume','bind'}])
    expected=sorted([(m['type'],m['source'],m['target'],not m['read_only']) for m in lease['mounts']])
    if observed!=expected:raise ValueError('capture helper retained data or private staging mounts changed')
    return value


def _inspect(resources,lease,*,verify_process):
    value=_inspect_native(resources,lease)
    if verify_process and value.get('State',{}).get('Running'):
        pid=value['State'].get('Pid')
        if type(pid) is not int or pid<=0:raise ValueError('capture helper omitted actual process identity')
        missing_paths={str(PROC_ROOT/str(pid)/'cgroup')}
        try:
            rows=(PROC_ROOT/str(pid)/'cgroup').read_text().splitlines()
            if len(rows)!=1 or not rows[0].startswith('0::/'):raise ValueError('capture helper lacks unified cgroup evidence')
            actual_path=CGROUP_ROOT/rows[0][3:].lstrip('/')
            if not actual_path.is_relative_to(Path(lease['control_path'])):raise ValueError('capture helper escaped its control parent')
            if ('..' in actual_path.parts or not actual_path.resolve().is_relative_to(Path(lease['control_path']).resolve())):
                raise ValueError('capture helper escaped its resolved control parent')
            missing_paths={str(actual_path/name) for name in ['memory.max','pids.max','memory.swap.max','cpu.max']}
            kernel_limits(actual_path)
            memory=int((actual_path/'memory.max').read_text());pids=int((actual_path/'pids.max').read_text())
            quota,period=map(int,(actual_path/'cpu.max').read_text().split())
            if memory!=LIMITS['memory_bytes'] or pids!=LIMITS['pids'] or abs(quota/period*1000-LIMITS['milli_cpus'])>0.001 or (actual_path/'memory.swap.max').read_text().strip()!='0':
                raise ValueError('capture helper actual kernel allocation differs from its finite limits')
            if 'EffectiveCaps' not in value or value['EffectiveCaps'] not in [None,[]]:
                raise ValueError('capture helper retained Linux capabilities')
            missing_paths={str(PROC_ROOT/str(pid)/'status')}
            process_identity(pid,lease['user'])
        except FileNotFoundError as error:
            # A finite helper can exit between any of its owned live-evidence
            # reads. Re-admit its exact terminal identity once, not liveness.
            if (str(error.filename) not in missing_paths or lease['phase']!='running'
                    or lease['container_id']!=value['Id']):raise
            validate_lease(lease)
            deadline=lease['deadline_unix']
            if os.environ.get('KOMIZO_DEADLINE_UNIX'):
                deadline=min(deadline,int(os.environ['KOMIZO_DEADLINE_UNIX']))
            if deadline<=time.time():raise
            completed=_inspect_native(resources,lease,deadline=deadline)
            state=completed.get('State',{})
            if (deadline<=time.time() or completed['Id']!=value['Id'] or not isinstance(state,dict)
                    or state.get('Status') not in ('stopped','exited') or state.get('Running') is not False
                    or type(state.get('Pid')) is not int or state['Pid']!=0
                    or type(state.get('ExitCode')) is not int or not 0<=state['ExitCode']<=255):raise
            return completed
    return value


def _old_task_quiesced(lease,value):
    """Admit stale metadata only from a dead, distinct, protected old task."""
    task=os.environ.get('KOMIZO_TASK_OPERATION_ID')
    if (os.geteuid()!=0 or os.environ.get('KOMIZO_OPERATION_ID')!=lease['operation_id']
            or not isinstance(task,str) or not TOKEN.fullmatch(task)):
        return False
    old=Path(lease['control_path'])
    if (not old.is_relative_to(CGROUP_ROOT) or '..' in old.parts
            or not re.fullmatch(r'komizo-(?:backup|prepare|reconcile)-[a-f0-9]{32}\.service',old.name)
            or lease['control_parent']!='/'+old.relative_to(CGROUP_ROOT).as_posix()
            or value.get('HostConfig',{}).get('CgroupParent')!=lease['control_parent']
            or any(path.is_symlink() for path in [old,*old.parents])):
        return False
    current=production_control(task)
    if current==old:return False
    state=value.get('State',{})
    for field in ['Pid','ConmonPid']:
        # Native Podman omits its integer ConmonPid field when it is zero.
        pid=state.get(field,0) if field=='ConmonPid' else state.get(field)
        if type(pid) is not int or pid<0 or pid and (PROC_ROOT/str(pid)).exists():
            return False
    try:
        old.stat()
    except FileNotFoundError:
        return True
    protected_owner(old)
    rows=(old/'cgroup.events').read_text().splitlines()
    return [line.split() for line in rows if line.startswith('populated ')]==[['populated','0']]


def _unknown_stopped(value):
    state=value.get('State',{})
    conmon=state.get('ConmonPid',0)
    return (state.get('Status')=='exited' and state.get('Running') is False
        and type(state.get('Pid')) is int and state['Pid']==0
        and type(conmon) is int and conmon==0
        and type(state.get('ExitCode')) is int and state['ExitCode']==-1
        and state.get('Error')==CONMON_UNKNOWN_EXIT)


def validate_lease(value):
    keys={'kind','version','application','release_digest','operation_id','ownership','name','container_id',
        'config_digest','user','mounts','network','control_parent','control_path','deadline_unix','phase'}
    if (not isinstance(value,dict) or set(value)!=keys or value.get('kind')!='komizo.capture-helper'
            or type(value.get('version')) is not int or value['version']!=1
            or not re.fullmatch('[a-z][a-z0-9-]{0,62}',value.get('application',''))
            or not DIGEST.fullmatch(value.get('release_digest','')) or not TOKEN.fullmatch(value.get('operation_id',''))
            or not TOKEN.fullmatch(value.get('ownership','')) or value.get('name')!='kmz-capture-'+value['ownership']
            or value.get('container_id')!='' and not ID.fullmatch(value.get('container_id',''))
            or not DIGEST.fullmatch(value.get('config_digest','')) or not re.fullmatch('[0-9]{1,10}:[0-9]{1,10}',value.get('user',''))
            or not isinstance(value.get('control_parent'),str) or not isinstance(value.get('control_path'),str)
            or not Path(value['control_path']).is_absolute() or not Path(value['control_path']).is_relative_to(CGROUP_ROOT)
            or '..' in Path(value['control_path']).parts
            or value.get('network')!='none' and not re.fullmatch('container:[a-f0-9]{64}',value.get('network',''))
            or value.get('phase') not in {'intent','created','running','cleaned'}
            or type(value.get('deadline_unix')) is not int or not isinstance(value.get('mounts'),list)):
        raise ValueError('capture helper lease is not a closed durable intent')
    for mount in value['mounts']:
        if (not isinstance(mount,dict) or set(mount)!={'type','source','target','read_only','id'}
                or mount['type'] not in {'volume','bind'} or type(mount['read_only']) is not bool
                or not isinstance(mount['source'],str) or not isinstance(mount['target'],str)
                or ',' in mount['source'] or ',' in mount['target'] or any(char in mount['target'] for char in '\r\n\0')
                or not mount['target'].startswith('/') or '..' in mount['target'].split('/')
                or mount['type']=='volume' and (not mount['read_only'] or not DIGEST.fullmatch(mount['id']))
                or mount['type']=='bind' and mount['id'] is not None):
            raise ValueError('capture helper lease has invalid finite mounts')
    targets=[item['target'] for item in value['mounts']]
    if len(set(targets))!=len(targets) or any(a!=b and b.startswith(a.rstrip('/')+'/') for a in targets for b in targets):
        raise ValueError('capture helper mounts overlap their authority')
    return value


def exists(resources,identity):
    result=subprocess.run(['/usr/bin/podman','--remote=false','container','exists',identity],
        env=resources.environment(),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
    if result.returncode not in {0,1}:raise ValueError('capture helper presence is uncertain')
    return result.returncode==0


def cleanup_leases(resources,directory,*,inspect_only=False,ownership=None):
    """Retire one context's lease, or explicitly reconcile the whole operation."""
    if ownership is not None and (not isinstance(ownership,str) or not TOKEN.fullmatch(ownership)):
        raise ValueError('capture cleanup requires an exact ownership token')
    directory=private_directory(Path(directory),create=False);observations=[]
    paths=list(itertools.islice(directory.iterdir(),129))
    if len(paths)>128:raise ValueError('capture helper journal exceeds its finite inventory')
    records=[]
    for path in paths:
        if path.name.startswith('.lease-'):
            info=path.lstat()
            if (not re.fullmatch(r'\.lease-[a-z0-9_]{8}',path.name) or not stat.S_ISREG(info.st_mode)
                    or info.st_nlink!=1 or info.st_uid!=os.geteuid() or info.st_mode&0o077 or info.st_size>1<<20):
                raise ValueError('capture helper has an unsafe pending lease temporary')
            continue
        records.append(path)
    if len(records)>64:raise ValueError('capture helper journal exceeds its finite inventory')
    for path in sorted(records):
        if not re.fullmatch(r'[a-f0-9]{32}\.json',path.name):raise ValueError('capture helper journal contains an unknown member')
        if ownership is not None and path.name!=ownership+'.json':continue
        info=path.stat()
        if info.st_uid!=os.geteuid() or info.st_mode&0o077:raise ValueError('capture helper lease is not privately owned')
        lease=validate_lease(json.loads(regular(path),object_pairs_hook=unique))
        if lease['ownership']+'.json'!=path.name:raise ValueError('capture helper lease name changed')
        value=_inspect(resources,lease,verify_process=False) if exists(resources,lease['container_id'] or lease['name']) else None
        quiesced=value is not None and _old_task_quiesced(lease,value)
        if value is not None and value.get('State',{}).get('Running') and not quiesced:
            value=inspect(resources,lease)
        observations.append({'name':lease['name'],'container_id':None if value is None else value['Id'],
            'phase':lease['phase'],'present':value is not None,'running':bool(value and value.get('State',{}).get('Running') and not quiesced)})
        if inspect_only:continue
        if value is not None and lease['phase']=='cleaned':raise ValueError('a cleaned helper unexpectedly reappeared')
        if value is not None:
            if not lease['container_id']:
                lease['container_id']=value['Id'];lease['phase']='created';atomic(path,lease)
            try:
                run(resources,['rm','--force','--volumes',value['Id']],timeout=30)
            except ValueError:
                # Podman can commit this exact dead-conmon state and then fail
                # forced removal. Re-admit identity and kernel absence before
                # one non-force metadata removal; never invent a successful exit.
                stopped=_inspect(resources,lease,verify_process=False)
                if not _unknown_stopped(stopped) or not _old_task_quiesced(lease,stopped):raise
                run(resources,['rm','--volumes',value['Id']],timeout=30)
            if exists(resources,value['Id']):raise ValueError('capture helper removal remains uncertain')
        lease['phase']='cleaned';atomic(path,lease)
    return observations


@contextlib.contextmanager
def container(resources,record,staging,*,artifact='postgres',user,mounts=None,binds=(),entrypoint=None,
        command=None,environment=None,network='none',control_cgroup=None,timeout=180):
    staging=private_directory(Path(staging).absolute());resources.validate(record,record['application'],'postgres')
    parent,control_path,leases=control(control_cgroup,staging)
    if type(timeout) is not int or not 1<=timeout<=900:raise ValueError('capture helper requires a finite timeout')
    if not re.fullmatch('[0-9]{1,10}:[0-9]{1,10}',user) or network not in {'none','postgres'}:
        raise ValueError('capture helper requires explicit numeric authority and private network scope')
    selected=record['deployment']['release']['artifacts'].get(artifact,{})
    if selected.get('kind')!='image' or not DIGEST.fullmatch(selected.get('config_digest','')):
        raise ValueError('capture helper image is not bound by the protected descriptor')
    if command is not None and (not isinstance(command,list) or any(not isinstance(item,str) or '\0' in item for item in command)
            or not isinstance(entrypoint,str) or not entrypoint.startswith('/') or '..' in entrypoint.split('/')):
        raise ValueError('started capture helper requires one explicit native program')
    ownership=secrets.token_hex(16);operation=os.environ.get('KOMIZO_OPERATION_ID') or secrets.token_hex(16)
    deadline=int(time.time())+timeout
    if os.environ.get('KOMIZO_DEADLINE_UNIX'):
        deadline=min(deadline,int(os.environ['KOMIZO_DEADLINE_UNIX']))
    if deadline<=time.time():raise ValueError('capture helper deadline expired')
    lease={'kind':'komizo.capture-helper','version':1,'application':record['application'],'release_digest':record['release_digest'],
        'operation_id':operation,'ownership':ownership,'name':'kmz-capture-'+ownership,'container_id':'',
        'config_digest':selected['config_digest'],'user':user,'mounts':[],'network':'none' if network=='none' else 'container:'+record['container_id'],'control_parent':parent,'control_path':control_path,
        'deadline_unix':deadline,'phase':'intent'}
    for logical,target in (mounts or {}).items():
        volume=retained_volume(record,logical);lease['mounts'].append({'type':'volume','source':volume['name'],'target':target,'read_only':True,'id':volume['id']})
    for bind in binds:
        if not isinstance(bind,dict) or set(bind)!={'source','target','read_only'}:raise ValueError('capture bind requires closed private staging authority')
        source=Path(bind['source']).absolute()
        if not source.is_relative_to(staging) or any(path.is_symlink() for path in [source,*source.parents]):raise ValueError('capture bind escaped private owned staging')
        if not source.is_dir():regular(source,64<<20)
        lease['mounts'].append({'type':'bind','source':str(source),'target':bind['target'],'read_only':bind['read_only'],'id':None})
    values={} if environment is None else environment
    if any(not re.fullmatch('[A-Z][A-Z0-9_]*',key) or not isinstance(value,str) or any(char in value for char in '\r\n\0') for key,value in values.items()):
        raise ValueError('capture helper environment must be bounded private assignments')
    envfile=staging/('.helper-'+ownership+'.env')
    path=leases/(ownership+'.json');validate_lease(lease);verify_volumes(resources,lease)
    arguments=['create','--name',lease['name'],'--pull=never','--read-only','--read-only-tmpfs=false','--image-volume=ignore',
        '--cap-drop=ALL','--security-opt=no-new-privileges','--http-proxy=false','--user',user,'--cgroup-parent',parent,
        '--memory',str(LIMITS['memory_bytes']),'--memory-swap',str(LIMITS['memory_bytes']),
        '--cpus',str(LIMITS['milli_cpus']/1000),'--pids-limit',str(LIMITS['pids']),'--timeout',str(max(1,deadline-int(time.time()))),
        '--network','none' if network=='none' else 'container:'+record['container_id'],'--env-file',str(envfile),
        '--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777']
    for key,value in [('ownership',ownership),('operation',operation)]:arguments+=['--label','io.komizo.capture.'+key+'='+value]
    arguments+=['--label','io.komizo.application='+record['application']]
    for mount in lease['mounts']:
        arguments+=['--mount','type='+mount['type']+',source='+mount['source']+',target='+mount['target']+(',readonly' if mount['read_only'] else '')]
    if entrypoint is not None:arguments+=['--entrypoint',entrypoint]
    arguments+=[selected['config_digest'],*(command or [])]
    try:
        with envfile.open('x') as stream:
            os.fchmod(stream.fileno(),0o600);stream.write(''.join(key+'='+value+'\n' for key,value in values.items()))
        atomic(path,lease)
        identity=run(resources,arguments,timeout=30).decode().strip()
        if not ID.fullmatch(identity):raise ValueError('capture helper did not record one complete native identity')
        lease['container_id']=identity;lease['phase']='created';atomic(path,lease);inspect(resources,lease)
        if command is not None:
            run(resources,['start',identity],timeout=30);lease['phase']='running';atomic(path,lease);inspect(resources,lease)
        yield identity
    finally:
        try:
            if path.exists():cleanup_leases(resources,leases,ownership=ownership)
        finally:envfile.unlink(missing_ok=True)
