"""Publish independently admitted native companions without embedding them."""
import hashlib
import json
from pathlib import Path
import re

import native_components
import artifact_packet


def records(config):
    package=config.get('package',{});extra=package.get('extra_files',[])
    sources=package.get('extra_file_sources',{})
    if (not isinstance(extra,list) or len(extra)>64 or any(not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',name) for name in extra) or len(extra)!=len(set(extra))
            or not isinstance(sources,dict) or set(sources)-set(extra)):
        raise ValueError('native companion sources require finite declared flat release names')
    graph={value['name']:value for value in native_components.records(config)} if sources else {}
    result={}
    for name,source in sources.items():
        if isinstance(source,dict) and set(source)=={'provider','platform'} and source.get('provider')=='caddy':
            if not isinstance(source['platform'],str) or source['platform'] not in {'Linux/x86_64','Linux/arm64'} or name!='caddy_'+source['platform'].replace('/','_') or not any(value['role']=='host' for value in graph.values()):
                raise ValueError('reviewed proxy companion requires a native Linux host producer')
            result[name]=({'name':'caddy','provider':'caddy'},source['platform'])
            continue
        if (not isinstance(source,dict) or set(source)!={'component','platform'}
                or not isinstance(source.get('component'),str) or source.get('component') not in graph
                or not isinstance(source.get('platform'),str) or source.get('platform') not in native_components.payload_helper().PLATFORMS):
            raise ValueError('native companion selects one declared component and platform')
        if graph[source['component']]['role']=='cli':
            raise ValueError('native companions must be built independently of the publishing CLI')
        result[name]=(graph[source['component']],source['platform'])
    names=[*extra,*[name+'.json' for name in result]]
    if len(names)!=len(set(names)) or set(names)&{'checksums.txt','release.json'}:
        raise ValueError('native companion receipts must have distinct release names')
    return result


def names(config):
    return [*config.get('package',{}).get('extra_files',[]),*[name+'.json' for name in records(config)]]


def admitted(root,config,environment):
    payload=native_components.payload_helper();result={}
    for name,(component,platform) in records(config).items():
        if component.get('provider')=='caddy':
            import native_proxy
            result[name]=native_proxy.verify(root,platform)
            continue
        hashes,_=native_components.dependencies(root,config,component,platform,environment)
        source=native_components.output(root,component,platform)
        accepted=payload.verify(source,environment['COMMIT'],environment['VERSION'],component['role'],platform,hashes)
        body=payload.bytes_of(source)
        receipt=payload.bytes_of(payload.receipt_path(source),65536)
        if hashlib.sha256(body).hexdigest()!=accepted['sha256'] or json.loads(receipt)!=accepted:
            raise ValueError('native companion bytes changed after admission')
        result[name]=(body,receipt)
    return result


def materialize(root,config,environment,destination):
    payload=native_components.payload_helper()
    destination=Path(destination)
    if (not destination.is_dir() or not destination.resolve().is_relative_to(Path(root).resolve())
            or any(path.is_symlink() for path in [destination,*destination.parents])):
        raise ValueError('native companion destination must be an owned directory without links')
    for name,(body,receipt) in admitted(root,config,environment).items():
        payload.atomic_write(Path(destination)/name,body)
        payload.atomic_write(Path(destination)/(name+'.json'),receipt)


def verify(root,config,environment,destination):
    payload=native_components.payload_helper()
    for name,(body,receipt) in admitted(root,config,environment).items():
        if payload.bytes_of(Path(destination)/name)!=body or payload.bytes_of(Path(destination)/(name+'.json'),65536)!=receipt:
            raise ValueError('published native companion or source receipt changed')


def input_paths(root,config):
    result=[]
    for component,platform in records(config).values():
        source=source_path(root,component,platform)
        result.extend([source.relative_to(root).as_posix(),source.with_name(source.name+'.json').relative_to(root).as_posix()])
    return list(dict.fromkeys(result))


def source_path(root,component,platform):
    if component.get('provider')=='caddy':
        import native_proxy
        return native_proxy.output(root,platform)
    return native_components.output(root,component,platform)


def producer_paths(root,config,platform,selected):
    graph=native_components.records(config)
    if not any(value['role']=='host' and value['name'] in selected for value in graph):return []
    result=[]
    for component,target in records(config).values():
        if component.get('provider')=='caddy' and target==platform:
            path=source_path(root,component,target).relative_to(root).as_posix()
            result.extend([path,path+'.json'])
    return result


def produce(root,config,platform,environment,run):
    selected=[value['name'] for value in native_components.selection(config,environment)]
    if producer_paths(root,config,platform,selected):
        import native_proxy
        if not environment.get('NATIVE_CLI_ARCHIVES'):native_proxy.build(root,platform,environment,run)
        native_proxy.verify(root,platform)


def verify_publication(root,config,proof,source,version,destination):
    """Re-admit companions from accepted matrix hashes on a fresh publisher."""
    payload=native_components.payload_helper()
    proofs=proof['native_matrix']['proofs']
    for name,(component,platform) in records(config).items():
        body=payload.bytes_of(Path(destination)/name)
        receipt_bytes=payload.bytes_of(Path(destination)/(name+'.json'),65536)
        receipt=json.loads(receipt_bytes,object_pairs_hook=artifact_packet.unique_object)
        if component.get('provider')=='caddy':
            import native_proxy
            expected=native_proxy.expected(platform)|{'binary_sha256':'sha256:'+hashlib.sha256(body).hexdigest()}
            if receipt_bytes!=native_proxy.canonical(receipt):raise ValueError('native proxy receipt must retain exact canonical bytes')
        else:
            expected=payload.identity(source,version,component['role'],platform,receipt.get('dependencies'))
            expected['sha256']=hashlib.sha256(body).hexdigest()
        if receipt!=expected:raise ValueError('published native companion identity differs from its accepted source')
        system,arch=platform.split('/')
        producers={component['name']} if component.get('provider')!='caddy' else {value['name'] for value in native_components.records(config) if value['role']=='host'}
        matches=[value for value in proofs if producers.intersection(value['entry']['components'])
            and value['entry']['platform']==system and value['entry']['arch']==arch]
        path=source_path(root,component,platform).relative_to(root).as_posix()
        if len(matches)!=1 or matches[0]['build']['artifacts'].get(path)!=hashlib.sha256(body).hexdigest() or matches[0]['build']['artifacts'].get(path+'.json')!=hashlib.sha256(receipt_bytes).hexdigest():
            raise ValueError('published native companion is missing its exact tested matrix bytes')
