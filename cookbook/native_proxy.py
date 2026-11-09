"""Build and admit the reviewed native proxy as an independent Linux companion."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform as host_platform
import re
import subprocess
import sys
import tempfile
import tomllib

sys.dont_write_bytecode=True
import artifact_packet
from configuration import relative
import dependencies
import komizo_core
import workload_fixture

PLATFORMS={'Linux/x86_64':('linux/amd64',62,'amd64'),'Linux/arm64':('linux/arm64',183,'arm64')}
SOURCES=('caddy.go.mod','caddy.go.sum','caddy-build.sh')
GO_VERSION='1.27.3'
CADDY_VERSION='2.11.6'
MAX_BINARY=256<<20


def selected_platform(platform):
    if platform not in PLATFORMS:raise ValueError('native proxy requires one reviewed Linux platform')
    return PLATFORMS[platform]


def enabled(config):
    sources=config.get('package',{}).get('extra_file_sources',{})
    if not isinstance(sources,dict):raise ValueError('native companions require a finite source map')
    enabled=False
    for name,source in sources.items():
        if isinstance(source,dict) and source.get('provider')=='caddy':
            if (set(source)!={'provider','platform'} or source['platform'] not in PLATFORMS
                    or name!='caddy_'+source['platform'].replace('/','_')
                    or name not in config.get('package',{}).get('extra_files',[])):
                raise ValueError('native proxy companion requires its exact flat name and reviewed platform')
            enabled=True
    return enabled


def output(root,platform):
    selected_platform(platform)
    return relative(Path(root),'.artifacts/native-proxy/caddy_'+platform.replace('/','_'))


def receipt_path(binary):
    return Path(binary).with_name(Path(binary).name+'.json')


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()


def source_bytes():
    tools=dependencies.tools_root();identity=dependencies.tools_identity()
    if identity['repository']!='nicodes/tools' or not re.fullmatch('[a-f0-9]{40}',identity['revision']):
        raise ValueError('native proxy requires its immutable generic source provider')
    files={}
    for name in SOURCES:
        body=artifact_packet.read_regular(tools/'helpers'/name,1<<20)
        committed=subprocess.check_output(['git','--no-replace-objects','-c','core.hooksPath='+os.devnull,
            '-c','core.fsmonitor=false','-C',str(tools),'show',identity['revision']+':helpers/'+name],timeout=30)
        if body!=committed:raise ValueError('proxy build inputs differ from the selected immutable Git blobs')
        files[name]=body
    if not re.search(rb'^require github\.com/caddyserver/caddy/v2 v'+CADDY_VERSION.encode().replace(b'.',rb'\.')+rb'$',files['caddy.go.mod'],re.M):
        raise ValueError('native proxy source differs from the reviewed upstream version')
    if workload_fixture.go_version()!=GO_VERSION:
        raise ValueError('native proxy requires the reviewed exact Go compiler')
    return identity,files


def expected(platform):
    target,_,_=selected_platform(platform);provider,files=source_bytes()
    return {'kind':'komizo.proxy-build','version':1,'tools_revision':provider['revision'],
        'platform':target,'go_version':GO_VERSION,'caddy_version':CADDY_VERSION,
        'source_sha256':{name:'sha256:'+hashlib.sha256(body).hexdigest() for name,body in files.items()}}


def verify(root,platform):
    path=output(root,platform);body=artifact_packet.read_regular(path,MAX_BINARY)
    raw=artifact_packet.read_regular(receipt_path(path),32768)
    receipt=json.loads(raw,object_pairs_hook=artifact_packet.unique_object)
    if not isinstance(receipt,dict) or type(receipt.get('version')) is not int or raw!=canonical(receipt) or receipt!=expected(platform)|{'binary_sha256':'sha256:'+hashlib.sha256(body).hexdigest()}:
        raise ValueError('native proxy bytes or canonical source receipt changed')
    elf(body,platform)
    if artifact_packet.regular(path).st_mode&0o022 or not artifact_packet.regular(path).st_mode&0o111:
        raise ValueError('native proxy requires an ordinary protected executable')
    return body,raw


def elf(body,platform):
    _,machine,_=selected_platform(platform)
    if (len(body)<64 or body[:6]!=b'\x7fELF\x02\x01' or int.from_bytes(body[16:18],'little') not in {2,3}
            or int.from_bytes(body[18:20],'little')!=machine):
        raise ValueError('native proxy is not the exact selected Linux ELF architecture')
    offset=int.from_bytes(body[32:40],'little');size=int.from_bytes(body[54:56],'little');count=int.from_bytes(body[56:58],'little')
    if count<1 or count>4096 or size!=56 or offset<64 or offset+size*count>len(body):
        raise ValueError('native proxy requires a bounded ordinary ELF program table')
    for index in range(count):
        if int.from_bytes(body[offset+size*index:offset+size*index+4],'little')==3:
            raise ValueError('native proxy may not depend on a host ELF interpreter')


def environment(values,platform):
    _,_,arch=selected_platform(platform)
    values=workload_fixture.compiler_environment(values)
    for key in list(values):
        if key.startswith('GOVULNCHECK_'):values.pop(key,None)
    for key in ['GOWORK','GOEXPERIMENT','GODEBUG','GO111MODULE','GOTOOLDIR','LD_PRELOAD','LD_LIBRARY_PATH']:
        values.pop(key,None)
    return values|{'GOWORK':'off','GOOS':'linux','GOARCH':arch,'CGO_ENABLED':'0','GOVULNDB':'https://vuln.go.dev'}


def _tools(root,values,run):
    clean=workload_fixture.compiler_environment(values)
    clean['GOWORK']='off'
    # Setup is explicit and does not prepare the application's core renderer.
    komizo_core.install_tool('go',GO_VERSION,clean,run)
    pin=tomllib.loads((Path(__file__).resolve().parents[1]/'mise.toml').read_text())['tools']['go:golang.org/x/vuln/cmd/govulncheck']
    if not isinstance(pin,str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',pin):
        raise ValueError('native proxy audit requires its exact provider scanner pin')
    komizo_core.install_tool('go:golang.org/x/vuln/cmd/govulncheck',pin,clean,run)
    return pin


def prepare(root,config,values,run):
    if sys.platform!='linux' or not enabled(config) or values.get('NATIVE_CLI_ARCHIVES'):return
    selected=json.loads(values['COOKBOOK_NATIVE_COMPONENTS']) if values.get('COOKBOOK_NATIVE_COMPONENTS') else None
    if selected is not None and not any(value.get('role')=='host' and value.get('name') in selected
            for value in config.get('components',{}).get('native',[])):return
    _tools(root,values,run)


def buildinfo(text,platform):
    _,_,arch=selected_platform(platform)
    lines=text.splitlines()
    if not lines or lines[0].rsplit(' ',1)[-1]!='go'+GO_VERSION:
        raise ValueError('native proxy was built by a different Go compiler')
    rows=[line.strip().split('\t') for line in lines[1:]]
    if [row for row in rows if row and row[0]=='path'] != [['path','github.com/caddyserver/caddy/v2/cmd/caddy']]:
        raise ValueError('native proxy has an unexpected native entrypoint')
    modules=[row for row in rows if len(row)>=2 and row[0] in {'mod','dep'} and row[1]=='github.com/caddyserver/caddy/v2']
    if len(modules)!=1 or len(modules[0])!=4 or modules[0][2]!='v'+CADDY_VERSION or not modules[0][3].startswith('h1:'):
        raise ValueError('native proxy has a different upstream module identity')
    if any(row and row[0]=='=>' for row in rows):raise ValueError('native proxy may not replace reviewed module sources')
    flags=[row[1] for row in rows if len(row)==2 and row[0]=='build']
    for key,value in {'GOOS':'linux','GOARCH':arch,'CGO_ENABLED':'0','-trimpath':'true'}.items():
        if [item for item in flags if item.split('=',1)[0]==key]!=[key+'='+value]:
            raise ValueError('native proxy has unexpected compiler target or build settings')
    return modules[0][0]


def _capture(command,**options):
    return subprocess.check_output(command,**options)


def _audit(path,values):
    pin=tomllib.loads((Path(__file__).resolve().parents[1]/'mise.toml').read_text())['tools']['go:golang.org/x/vuln/cmd/govulncheck']
    raw=_capture(['mise','exec','go:golang.org/x/vuln/cmd/govulncheck@'+pin,'--',
        'govulncheck','-mode=binary','-json',str(path)],cwd=path.parent,env=values,timeout=600)
    if len(raw)>16<<20:raise ValueError('native proxy audit exceeds its finite report bound')
    verdict=dependencies.helper('scan-image').judge_report(raw.decode())
    if verdict['reached']:raise ValueError('native proxy has vulnerable linked symbols')


def atomic(path,body,mode):
    path=Path(path);artifact_packet.regular(path) if path.exists() else None
    with tempfile.NamedTemporaryFile(prefix='.proxy-',dir=path.parent,delete=False) as stream:
        temporary=Path(stream.name)
        try:
            stream.write(body);stream.flush();os.fsync(stream.fileno());os.fchmod(stream.fileno(),mode)
            os.replace(temporary,path)
        finally:temporary.unlink(missing_ok=True)


def build(root,platform,values,run):
    root=Path(root);selected_platform(platform);identity=expected(platform)
    _,files=source_bytes();clean=environment(values,platform)
    compiler=_capture(['mise','exec','go@'+GO_VERSION,'--','go','env','GOVERSION'],
        cwd=root,env=clean,text=True,timeout=30).strip()
    if compiler!='go'+GO_VERSION:
        raise ValueError('native proxy compiler differs from the reviewed exact SDK')
    destination=output(root,platform);destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with tempfile.TemporaryDirectory(prefix='.build-',dir=destination.parent) as directory:
        working=Path(directory)
        clean=clean|{'GOMODCACHE':str(working/'modules'),'GOCACHE':str(working/'cache'),'GOPATH':str(working/'go'),
            'GOPROXY':'https://proxy.golang.org','GOSUMDB':'sum.golang.org','GOPRIVATE':'','GONOSUMDB':'','GONOPROXY':'','GOINSECURE':''}
        for name,target in [('caddy.go.mod','go.mod'),('caddy.go.sum','go.sum'),('caddy-build.sh','build.sh')]:
            (working/target).write_bytes(files[name])
        run(['mise','exec','go@'+GO_VERSION,'--','sh','build.sh'],working,clean,timeout=900)
        run(['mise','exec','go@'+GO_VERSION,'--','go','mod','verify'],working,clean,timeout=300)
        binary=working/'caddy';body=artifact_packet.read_regular(binary,MAX_BINARY);elf(body,platform)
        info=_capture(['mise','exec','go@'+GO_VERSION,'--','go','version','-m',str(binary)],
            cwd=working,env=clean,text=True,timeout=30)
        layout=buildinfo(info,platform)
        # This executes only a binary for the native runner's architecture.
        host={'x86_64':'Linux/x86_64','aarch64':'Linux/arm64','arm64':'Linux/arm64'}.get(host_platform.machine())
        if sys.platform=='linux' and platform==host:
            version=_capture([str(binary),'version'],cwd=working,env=clean,text=True,timeout=15)
            # Upstream Version() reads dependencies, not Go's main-module row.
            # The exact main module above authenticates this source version.
            if not ((layout=='mod' and version.strip()=='unknown') or re.fullmatch(r'v'+re.escape(CADDY_VERSION)+r'(?:\s[^\r\n]*)?\n?',version)):
                raise ValueError('native proxy reports a different upstream version')
        _audit(binary,clean)
        for name,target in [('caddy.go.mod','go.mod'),('caddy.go.sum','go.sum'),('caddy-build.sh','build.sh')]:
            if artifact_packet.read_regular(working/target,1<<20)!=files[name]:
                raise ValueError('native proxy build changed its reviewed module closure')
        if expected(platform)!=identity:raise ValueError('native proxy provider changed during build')
        if artifact_packet.read_regular(binary,MAX_BINARY)!=body:raise ValueError('native proxy binary changed during admission')
        receipt=canonical(identity|{'binary_sha256':'sha256:'+hashlib.sha256(body).hexdigest()})
        atomic(destination,body,0o755);atomic(receipt_path(destination),receipt,0o644)
    return verify(root,platform)


def main(arguments=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('operation',choices=['build','verify'])
    parser.add_argument('--root',type=Path,required=True);parser.add_argument('--platform',choices=list(PLATFORMS),required=True)
    args=parser.parse_args(arguments)
    def run(command,cwd,env,**options):subprocess.run(command,cwd=cwd,env=env,check=True,**options)
    if args.operation=='build':_tools(args.root,dict(os.environ),run);build(args.root,args.platform,dict(os.environ),run)
    else:verify(args.root,args.platform)

if __name__=='__main__':main()
