"""Admit the publisher's pinned Caddy release as an independent Linux companion."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile

sys.dont_write_bytecode=True
import artifact_packet
from configuration import relative

CADDY_VERSION='2.11.6'
RELEASE='https://github.com/caddyserver/caddy/releases/download/v'+CADDY_VERSION+'/'
# Trust anchor: the release archive and its `caddy` binary, as published by the Caddy project, per platform:
# (release architecture, ELF machine, archive sha256, binary sha256).
PLATFORMS={
    'Linux/x86_64':('amd64',62,'22c84f8d2d4e4e0e2d422f8049fdd0fc1ed8d5665d0fe166f506c7fd863b4555',
        '9855a8971f1d3cd705b6d606962271f4acec82a349e88d7d280d92d055e7f11d'),
    'Linux/arm64':('arm64',183,'6a59a88914db644d410c4773286784096a97b59ea74152311442c19e988071ac',
        '38717b9d12fb46e5c7ac2270968567ba4bf0f5c561c144c857b852e33d67e649')}
MAX_ARCHIVE=96<<20
MAX_BINARY=256<<20


def selected_platform(platform):
    if platform not in PLATFORMS:raise ValueError('native proxy requires one reviewed Linux platform')
    return PLATFORMS[platform]


def asset(platform):
    return 'caddy_'+CADDY_VERSION+'_linux_'+selected_platform(platform)[0]+'.tar.gz'


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


def expected(platform):
    arch,_,archive,binary=selected_platform(platform)
    return {'kind':'komizo.proxy-download','version':1,'platform':'linux/'+arch,'caddy_version':CADDY_VERSION,
        'source_url':RELEASE+asset(platform),'archive_sha256':'sha256:'+archive,'binary_sha256':'sha256:'+binary}


def verify(root,platform):
    path=output(root,platform);body=artifact_packet.read_regular(path,MAX_BINARY)
    raw=artifact_packet.read_regular(receipt_path(path),32768)
    receipt=json.loads(raw,object_pairs_hook=artifact_packet.unique_object)
    if not isinstance(receipt,dict) or type(receipt.get('version')) is not int or raw!=canonical(receipt) or receipt!=expected(platform) or hashlib.sha256(body).hexdigest()!=selected_platform(platform)[3]:
        raise ValueError('native proxy bytes or canonical source receipt changed')
    elf(body,platform)
    if artifact_packet.regular(path).st_mode&0o022 or not artifact_packet.regular(path).st_mode&0o111:
        raise ValueError('native proxy requires an ordinary protected executable')
    return body,raw


def elf(body,platform):
    _,machine,_,_=selected_platform(platform)
    if (len(body)<64 or body[:6]!=b'\x7fELF\x02\x01' or int.from_bytes(body[16:18],'little') not in {2,3}
            or int.from_bytes(body[18:20],'little')!=machine):
        raise ValueError('native proxy is not the exact selected Linux ELF architecture')
    offset=int.from_bytes(body[32:40],'little');size=int.from_bytes(body[54:56],'little');count=int.from_bytes(body[56:58],'little')
    if count<1 or count>4096 or size!=56 or offset<64 or offset+size*count>len(body):
        raise ValueError('native proxy requires a bounded ordinary ELF program table')
    for index in range(count):
        if int.from_bytes(body[offset+size*index:offset+size*index+4],'little')==3:
            raise ValueError('native proxy may not depend on a host ELF interpreter')


def atomic(path,body,mode):
    path=Path(path);artifact_packet.regular(path) if path.exists() else None
    with tempfile.NamedTemporaryFile(prefix='.proxy-',dir=path.parent,delete=False) as stream:
        temporary=Path(stream.name)
        try:
            stream.write(body);stream.flush();os.fsync(stream.fileno());os.fchmod(stream.fileno(),mode)
            os.replace(temporary,path)
        finally:temporary.unlink(missing_ok=True)


def extract(archive):
    """Read only the `caddy` regular file; nothing in the archive is written to disk."""
    with tarfile.open(fileobj=io.BytesIO(archive),mode='r:gz') as bundle:
        members=[member for member in bundle if member.name in {'caddy','./caddy'}]
        if len(members)!=1 or not members[0].isreg() or members[0].size>MAX_BINARY:
            raise ValueError('native proxy archive requires exactly one ordinary caddy executable')
        return bundle.extractfile(members[0]).read()


def fetch(root,platform):
    """Download, pin-check and admit the release binary; reuse an already admitted copy."""
    root=Path(root);_,_,archive,binary=selected_platform(platform)
    try:return verify(root,platform)
    except (ValueError,OSError):pass
    destination=output(root,platform);destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with tempfile.TemporaryDirectory(prefix='.download-',dir=destination.parent) as directory:
        staged=Path(directory)/asset(platform)
        artifact_packet.download_executable(RELEASE+asset(platform),staged,archive,MAX_ARCHIVE)
        body=extract(staged.read_bytes())
    if hashlib.sha256(body).hexdigest()!=binary:raise ValueError('native proxy binary checksum mismatch')
    elf(body,platform)
    atomic(destination,body,0o755);atomic(receipt_path(destination),canonical(expected(platform)),0o644)
    return verify(root,platform)


def main(arguments=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('operation',choices=['fetch','verify'])
    parser.add_argument('--root',type=Path,required=True);parser.add_argument('--platform',choices=list(PLATFORMS),required=True)
    args=parser.parse_args(arguments)
    if args.operation=='fetch':fetch(args.root,args.platform)
    else:verify(args.root,args.platform)

if __name__=='__main__':main()
