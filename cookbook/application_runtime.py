"""Boot an accepted workload on disposable native resources for product assertions."""
import contextlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

import application_restore
import dependencies
import deployment
import native_runtime
import workload_fixture

LABEL = 'cookbook.runtime.owner'
IDENTITY = re.compile(r'[a-f0-9]{64}\Z')


def memory_bytes(value):
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)\s*(B|kB|KiB|MB|MiB|GB|GiB)', value.strip())
    if match is None:
        raise ValueError('native fixture returned an unsupported memory measurement')
    units = {'B':1,'kB':1000,'KiB':1024,'MB':1000**2,'MiB':1024**2,'GB':1000**3,'GiB':1024**3}
    return int(float(match[1]) * units[match[2]])


def assert_limits(record, info):
    """Check actual native allocation independently from argument generation."""
    host=info['HostConfig'];limits=record['limits']
    if (host.get('ReadonlyRootfs') is not True or host.get('Privileged') is True
            or info.get('Config',{}).get('User')!=record['user']
            or host.get('Memory')!=limits['memory_bytes']
            or host.get('MemorySwap')!=limits['memory_swap_bytes']
            or host.get('PidsLimit')!=limits['pids']):
        raise ValueError('native runtime allocation differs from its accepted hardening or resource limits')
    quota,period=host.get('CpuQuota',0),host.get('CpuPeriod',0)
    if period<=0 or quota<=0 or abs(quota/period*1000-limits['milli_cpus'])>0.001:
        raise ValueError('native CPU quota differs from its accepted allocation')
    for name,expected in record.get('ulimits',{}).items():
        matches=[item for item in host.get('Ulimits',[]) if item['Name'].lower().removeprefix('rlimit_')==name]
        if len(matches)!=1 or matches[0].get('Soft')!=expected['soft'] or matches[0].get('Hard')!=expected['hard']:
            raise ValueError('native file descriptor allocation differs from its accepted limit')
    if not any(item.split('=',1)[0].split(':',1)[0]=='no-new-privileges' for item in host.get('SecurityOpt',[])):
        raise ValueError('native runtime did not enforce its privilege boundary')
    if set(host.get('Tmpfs') or {})!=set(record.get('tmpfs',{})):
        raise ValueError('native runtime added or omitted an accepted writable tmpfs')
    declared={value['target'] for value in record.get('volumes',[])}
    if any(value.get('Type')=='volume' and value.get('Destination') not in declared for value in info.get('Mounts',[])):
        raise ValueError('native runtime created an undeclared writable image volume')
    effective=info.get('EffectiveCaps')
    if 'EffectiveCaps' in info and effective is None:
        effective=[]
    if not isinstance(effective,list) or set(effective)!={'CAP_'+name for name in record.get('capabilities',[])}:
        raise ValueError('native runtime capability set differs from its accepted least authority')


def ordered(workload):
    """Retain the accepted dependency DAG rather than assuming a service count."""
    services=workload['services'];jobs=workload['jobs'];nodes=services|jobs;active=set();complete=set();result=[]
    def visit(name):
        if name in complete:return
        if name in active:raise ValueError('application runtime dependencies contain a cycle')
        if name not in nodes:raise ValueError('application runtime selects an undeclared dependency')
        active.add(name)
        for dependency,condition in nodes[name].get('needs',{}).items():
            if condition=='completed' and dependency not in jobs:raise ValueError('completed runtime dependency must be a job')
            if condition=='healthy' and not services.get(dependency,{}).get('health',{}).get('command'):
                raise ValueError('healthy runtime dependency requires its actual service probe')
            visit(dependency)
        active.remove(name);complete.add(name);result.append(name)
    for name in sorted(nodes):visit(name)
    return result


class Metrics:
    def __init__(self, runtime):
        self.runtime=runtime;self.samples=[];self.errors=[];self.caddy=None
        self.stopping=threading.Event();self.thread=threading.Thread(target=self.monitor,daemon=True)

    def caddy_started(self,pid):
        status=Path('/proc')/str(pid)/'stat'
        self.caddy=(pid,status.read_text().rsplit(')',1)[1].split()[19])

    def sample(self):
        identities=list(self.runtime.active)
        if not identities:return
        result=self.runtime.execute(['stats','--no-stream','--no-trunc','--format','{{.ContainerID}} {{.MemUsage}}',*identities],required=False,timeout=10)
        if result.returncode:return
        values={}
        for row in result.stdout.splitlines():
            identity,separator,usage=row.partition(' ')
            if not separator or identity not in identities:raise ValueError('native stats selected an unowned container')
            values[self.runtime.active.get(identity,identity)]=memory_bytes(usage.split('/',1)[0])
        if self.caddy:
            pid,start=self.caddy
            if (Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()[19]!=start:
                raise ValueError('owned Caddy process identity changed')
            values['caddy']=int((Path('/proc')/str(pid)/'statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')
        complete=set(self.runtime.services)<=set(values) and 'caddy' in values
        self.samples.append({'at':time.monotonic(),'bytes':sum(values.values()),'components':values,'complete':complete})

    def monitor(self):
        while not self.stopping.wait(.2):
            try:self.sample()
            except (OSError,ValueError,subprocess.SubprocessError) as error:self.errors.append(type(error).__name__)

    def start(self):self.thread.start()

    def stop(self):
        self.stopping.set();self.thread.join(timeout=15)
        if self.thread.is_alive():raise ValueError('native resource observation did not stop')

    def report(self):
        samples=list(self.samples);complete=[row for row in samples if row['complete']]
        return {'samples':samples,'measurement_errors':list(self.errors),'observations':len(samples),
            'complete_observations':len(complete),'peak_bytes':max([row['bytes'] for row in samples],default=0),
            'complete_peak_bytes':max([row['bytes'] for row in complete],default=0),
            'latest':complete[-1] if complete else None}


class Runtime:
    def __init__(self,root,config,environment,profile):
        self.root=Path(root);self.config=config;self.environment=dict(environment)
        self.profile=application_restore.load_config(profile) if isinstance(profile,(str,Path)) else dict(profile)
        self.release=deployment.verify(self.root,self.environment)
        if self.release['application']!=self.profile['project'] or 'bootstrap' not in self.profile:
            raise ValueError('runtime fixture requires its source-owned application bootstrap profile')
        self.nonce=secrets.token_hex(16);self.prefix=self.release['application']+'-runtime-'+self.nonce[:12]
        self.containers=[];self.services={};self.active={};self.networks={};self.volumes={}
        self.metrics=Metrics(self);self.directory=None;self.fixture=None;self.upstreams={}
        self.owner_password=secrets.token_hex(32);self.runtime_password=secrets.token_hex(32)
        services=self.release['workload']['services']
        if 'postgres' not in services or self.profile['api_service'] not in services:
            raise ValueError('runtime fixture requires declared database and API services')
        postgres=services['postgres'];self.database=self.profile.get('database',postgres.get('environment',{}).get('POSTGRES_DB'))
        self.owner_database=postgres.get('environment',{}).get('POSTGRES_DB',self.database)
        self.owner_role=postgres.get('environment',{}).get('POSTGRES_USER')
        if any(not isinstance(value,str) or not re.fullmatch('[a-z][a-z0-9_]{0,62}',value) for value in [self.database,self.owner_database,self.owner_role]):
            raise ValueError('runtime fixture requires explicit native database and owner role identifiers')
        jobs=self.release['workload']['jobs'];bootstrap=self.profile['bootstrap']
        if set(jobs)!={bootstrap['job']}:
            raise ValueError('runtime profile must describe every actual one-shot job')
        self.order=ordered(self.release['workload'])

    def execute(self,args,*,required=True,timeout=60,body=None):
        result=native_runtime.run(args,environment=self.environment,input=body,capture_output=True,text=True,timeout=timeout)
        if result.returncode and required:raise ValueError('owned application runtime command failed')
        return result

    def inspect(self,identity):
        values=json.loads(self.execute(['container','inspect',identity]).stdout)
        if (not isinstance(values,list) or len(values)!=1 or values[0].get('Id')!=identity
                or values[0].get('Config',{}).get('Labels',{}).get(LABEL)!=self.nonce):
            raise ValueError('runtime fixture container ownership changed')
        return values[0]

    def resource(self,service):
        identity=self.services[service];info=self.inspect(identity)
        artifact=self.release['artifacts'][self.release['workload']['services'][service]['artifact']]
        actual=info.get('Image','');actual=actual if actual.startswith('sha256:') else 'sha256:'+actual
        if actual!=artifact['config_digest']:raise ValueError('runtime fixture image differs from the accepted release')
        return {'container_id':identity,'image_ref':artifact['reference'],'image_config_digest':actual,
            'inspect':info,'release_digest':'sha256:'+__import__('hashlib').sha256(deployment.canonical(self.release)).hexdigest()}

    def credentials(self,name,values):
        if any(not re.fullmatch('[A-Z][A-Z0-9_]*',key) or not isinstance(value,str)
                or any(char in value for char in '\r\n\0') for key,value in values.items()):
            raise ValueError('runtime fixture parameters must be bounded literal values')
        path=self.directory/(name+'.env');path.write_text(''.join(key+'='+value+'\n' for key,value in values.items()));path.chmod(0o600)
        return path

    def arguments(self,name,record,environment,*,publish=False):
        limit=record['limits'];uid,_,gid=record['user'].partition(':');gid=gid or uid
        arguments=['create','--pull=never','--name',self.prefix+'-'+name,'--label',LABEL+'='+self.nonce,
            '--read-only','--read-only-tmpfs=false','--image-volume=ignore','--user',record['user'],'--cap-drop=ALL','--security-opt=no-new-privileges',
            '--memory',str(limit['memory_bytes']),'--memory-swap',str(limit['memory_swap_bytes']),
            '--cpus',str(limit['milli_cpus']/1000),'--pids-limit',str(limit['pids'])]
        if record.get('capabilities'):arguments+=['--cap-add',','.join(record['capabilities'])]
        networks=record.get('networks',[])
        if not networks:arguments+=['--network=none']
        for network in networks:arguments+=['--network',self.networks[network]]
        if networks:arguments+=['--network-alias',name]
        for target,value in record.get('tmpfs',{}).items():
            execution='exec' if value.get('executable',False) else 'noexec'
            arguments+=['--tmpfs',f'{target}:rw,nosuid,nodev,{execution},size={value["size_bytes"]},mode={value["mode"]:o}']
        for volume in record.get('volumes',[]):
            arguments+=['--mount',f'type=volume,source={self.volumes[volume["name"]]},target={volume["target"]}'+(',readonly' if volume['read_only'] else '')]
        for name_,value in record.get('ulimits',{}).items():arguments+=['--ulimit',f'{name_}={value["soft"]}:{value["hard"]}']
        for key,value in record.get('sysctls',{}).items():arguments+=['--sysctl',key+'='+value]
        if 'stop_timeout_seconds' in record:arguments+=['--stop-timeout',str(record['stop_timeout_seconds'])]
        if record.get('init'):arguments+=['--init']
        if record.get('working_directory'):arguments+=['--workdir',record['working_directory']]
        if record.get('entrypoint'):arguments+=['--entrypoint',json.dumps(record['entrypoint'])]
        if publish:arguments+=['--publish','127.0.0.1::'+str(self.profile['api_port'])]
        arguments+=['--env-file',str(self.credentials(name,environment))]
        artifact=self.release['artifacts'][record['artifact']]
        return arguments+[artifact['config_digest'],*record.get('command',[])]

    def create(self,name,record,environment,*,publish=False,mounts=()):
        owned_name=self.prefix+'-'+name;self.containers.append(owned_name)
        arguments=self.arguments(name,record,environment,publish=publish)
        if mounts:
            image_index=arguments.index(self.release['artifacts'][record['artifact']]['config_digest'])
            arguments[image_index:image_index]=mounts
        identity=self.execute(arguments).stdout.strip()
        if not IDENTITY.fullmatch(identity):raise ValueError('runtime fixture did not create one exact native container')
        self.active[identity]=name
        self.execute(['start',identity])
        return identity

    def wait(self,identity,command,timeout=90):
        deadline=time.monotonic()+timeout
        while True:
            if not self.inspect(identity).get('State',{}).get('Running'):
                raise ValueError('application runtime stopped before readiness')
            result=self.execute(['exec',identity,*command],required=False,timeout=10)
            if result.returncode==0:return
            if time.monotonic()>=deadline:raise ValueError('application runtime readiness deadline exceeded')
            time.sleep(.2)

    def prepare(self):
        for name,artifact in self.release['artifacts'].items():
            if artifact['kind']=='image':dependencies.helper('oci_archive').load_image(self.root/'.artifacts/release/images'/(name+'.oci.tar'),environment=self.environment)
        for name,record in self.release['workload']['networks'].items():
            actual=self.prefix+'-'+name;arguments=['network','create','--label',LABEL+'='+self.nonce]
            if record['internal']:arguments+=['--internal']
            self.networks[name]=actual;self.execute([*arguments,actual])
        for name in self.release['workload']['volumes']:
            actual=self.prefix+'-'+name;self.volumes[name]=actual;self.execute(['volume','create','--label',LABEL+'='+self.nonce,actual])
        # Application volumes start empty, but retain the declared numeric owner.
        for service,record in self.release['workload']['services'].items():
            if service=='postgres':continue
            for volume in record.get('volumes',[]):
                if volume['read_only']:continue
                owner=record['user']
                initialization={'artifact':self.release['workload']['services']['postgres']['artifact'],
                    'user':'0:0','limits':self.release['workload']['services']['postgres']['limits'],
                    'capabilities':['CHOWN'],'entrypoint':['/bin/chown'],'command':[owner,'/owned'],
                    'volumes':[{'name':volume['name'],'target':'/owned','read_only':False}]}
                identity=self.create('initialize-'+volume['name'],initialization,{})
                if self.execute(['wait',identity],timeout=30).stdout.strip()!='0':
                    raise ValueError('owned fixture volume initialization failed')
                self.active.pop(identity,None)

    def boot(self):
        workload=self.release['workload'];services=workload['services'];jobs=workload['jobs'];bootstrap=self.profile['bootstrap']
        owner_dsn=f'postgres://{self.owner_role}:{self.owner_password}@postgres:5432/{self.owner_database}?sslmode=disable'
        runtime_dsn=f'postgres://{self.profile["runtime_role"]}:{self.runtime_password}@postgres:5432/{self.database}?sslmode=disable'
        self.metrics.start()
        for name in self.order:
            job=name in jobs;record=(jobs if job else services)[name]
            parameters=dict(record.get('environment',{}));mounts=[]
            if name=='postgres':
                parameters|={'POSTGRES_PASSWORD':self.owner_password,'POSTGRES_DB':self.owner_database,'POSTGRES_USER':self.owner_role}
            elif name==bootstrap['job']:
                parameters|=bootstrap['environment']|{bootstrap['dsn_variable']:owner_dsn,bootstrap['runtime_dsn_variable']:runtime_dsn}
            elif name==self.profile['api_service']:
                parameters|=self.profile['env']
                if self.profile.get('dsn_transport','environment')=='file':
                    file=self.directory/'runtime.url';file.write_text(runtime_dsn+'\n');file.chmod(0o444)
                    parameters[self.profile['dsn_variable']]=self.profile['dsn_file']
                    mounts=['--mount',f'type=bind,src={file},dst={self.profile["dsn_file"]},readonly']
                else:parameters[self.profile['dsn_variable']]=runtime_dsn
            identity=self.create(name,record,parameters,publish=name==self.profile['api_service'],mounts=mounts)
            if job:
                code=self.execute(['wait',identity],timeout=record['timeout_seconds']).stdout.strip()
                if code!='0':raise ValueError('accepted application migration job failed')
                self.active.pop(identity,None)
            else:
                self.services[name]=identity
                if record.get('health',{}).get('command'):self.wait(identity,record['health']['command'])
                if name=='postgres':self.wait(identity,['pg_isready','-h','127.0.0.1','-U',self.owner_role,'-d',self.owner_database])
        api=self.services[self.profile['api_service']]
        port=self.execute(['port',api,str(self.profile['api_port'])+'/tcp']).stdout.strip()
        if not re.fullmatch(r'127\.0\.0\.1:[1-9][0-9]{0,4}',port) or int(port.rsplit(':',1)[1])>65535:
            raise ValueError('runtime API publication escaped owned loopback transport')
        self.upstreams={self.profile['api_service']:port}
        self.runtime_dsn=runtime_dsn
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}));deadline=time.monotonic()+90
        while True:
            try:
                with opener.open('http://'+port+self.profile['health_path'],timeout=3) as response:
                    if response.status==200:break
            except (OSError,urllib.error.URLError):pass
            if not self.inspect(api)['State']['Running'] or time.monotonic()>=deadline:
                raise ValueError('accepted API did not become ready on its owned loopback listener')
            time.sleep(.2)

    def cleanup(self):
        failures=[]
        for name in reversed(self.containers):
            exists=self.execute(['container','exists',name],required=False)
            if exists.returncode==1:continue
            if exists.returncode:failures.append('container ownership inspection failed');continue
            info=json.loads(self.execute(['container','inspect',name]).stdout)[0]
            if info.get('Config',{}).get('Labels',{}).get(LABEL)!=self.nonce:
                failures.append('container ownership changed');continue
            self.execute(['rm','--force','--volumes',info['Id']])
        for kind,records in [('volume',self.volumes),('network',self.networks)]:
            for name in reversed(list(records.values())):
                exists=self.execute([kind,'exists',name],required=False)
                if exists.returncode==1:continue
                if exists.returncode:failures.append('resource ownership inspection failed');continue
                info=json.loads(self.execute([kind,'inspect',name]).stdout)[0]
                if info.get('Labels',{}).get(LABEL)!=self.nonce:failures.append('resource ownership changed');continue
                self.execute([kind,'rm',name])
        if failures:raise ValueError('; '.join(failures))


@contextlib.contextmanager
def context(root,config,environment,profile=None):
    runtime=Runtime(root,config,environment,profile or Path(root)/'deploy/restore.toml')
    with tempfile.TemporaryDirectory(prefix='application-runtime-') as directory:
        runtime.directory=Path(directory)
        try:
            runtime.prepare();runtime.boot()
            with workload_fixture.context(root,config,environment,upstreams=runtime.upstreams,on_started=runtime.metrics.caddy_started) as fixture:
                runtime.fixture=fixture;runtime.metrics.sample()
                try:yield runtime
                finally:runtime.metrics.stop()
        finally:
            if runtime.metrics.thread.is_alive():runtime.metrics.stop()
            runtime.cleanup()
