"""Transfer exact prebuilt native test variants separately from shipping images."""
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from configuration import relative
import images


def declaration(root,config):
    value=config.get('components',{}).get('fixture_images')
    if value is None:return None
    if not isinstance(value,dict) or set(value)!={'directory'}:raise ValueError('fixture images require one generated artifact directory')
    directory=relative(root,value['directory'])
    if (not directory.is_relative_to(root/'.artifacts') or directory==root/'.artifacts'
            or directory.is_relative_to(root/'.artifacts/release')):
        raise ValueError('fixture images belong to generated artifacts outside the closed shipping release directory')
    definition=images.definition(root,config);shipping=images.components(definition)
    names=[record.get('name') for record in config.get('components',{}).get('images',[])]
    if any(not isinstance(name,str) or not re.fullmatch('[a-z][a-z0-9-]{0,63}',name) for name in names) or len(set(names))!=len(names):
        raise ValueError('fixture images require distinct typed native build components')
    extra=sorted(set(names)-set(shipping))
    if not 1<=len(extra)<=16 or not set(shipping)<=set(names):raise ValueError('fixture image declaration must cover real nonshipping variants')
    return {'directory':directory,'names':extra,'image_base':definition['image_base']}


def artifact_paths(root,config):
    value=declaration(root,config)
    return [] if value is None else [(value['directory']/name).relative_to(root).as_posix() for name in ['images.json','images.tar.gz']]


def references(root,config,revision='{revision}'):
    value=declaration(root,config)
    if value is None:return []
    if revision!='{revision}' and not re.fullmatch('[a-f0-9]{40}',revision):raise ValueError('fixture images require their exact source revision')
    return [value['image_base']+'-'+name+':'+revision for name in value['names']]


def digest(path):
    with path.open('rb') as source:return hashlib.file_digest(source,'sha256').hexdigest()


def inspect(refs,root,environment):
    values={}
    for reference in refs:
        identity=subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',reference],cwd=root,env=environment,text=True,timeout=60).strip()
        if not re.fullmatch('sha256:[a-f0-9]{64}',identity):raise ValueError('native fixture image has no immutable identity')
        values[reference]=identity
    return values


def package(root,config,environment,run):
    value=declaration(root,config)
    if value is None:return
    revision=environment['SOURCE_COMMIT'];refs=references(root,config,revision);identities=inspect(refs,root,environment)
    directory=value['directory'];directory.mkdir(parents=True,exist_ok=True)
    archive=directory/'images.tar.gz'
    with tempfile.TemporaryDirectory(prefix='fixture-images-',dir=root/'.artifacts') as scratch:
        raw=Path(scratch)/'images.tar'
        run(['docker','save','--output',str(raw),*refs],root,environment,timeout=600)
        with raw.open('rb') as source,archive.open('wb') as target,gzip.GzipFile(fileobj=target,mode='wb',compresslevel=1,mtime=0) as compressed:
            shutil.copyfileobj(source,compressed)
    (directory/'images.json').write_text(json.dumps({'version':1,'source_commit':revision,'images':identities,'archive_sha256':digest(archive)},sort_keys=True)+'\n')


def verify(root,config,environment):
    value=declaration(root,config)
    if value is None:return {}
    directory=value['directory'];paths=[directory/name for name in ['images.json','images.tar.gz']]
    if any(path.is_symlink() or not path.is_file() for path in paths) or paths[0].stat().st_size>65536:
        raise ValueError('fixture image transport requires bounded regular manifest and archive')
    data=json.loads(paths[0].read_text());refs=references(root,config,environment['SOURCE_COMMIT'])
    if (set(data)!={'version','source_commit','images','archive_sha256'} or data['version']!=1
            or data['source_commit']!=environment['SOURCE_COMMIT'] or not isinstance(data['images'],dict)
            or set(data['images'])!=set(refs) or any(not isinstance(value,str) or not re.fullmatch('sha256:[a-f0-9]{64}',value) for value in data['images'].values())
            or data['archive_sha256']!=digest(paths[1])):
        raise ValueError('native fixture image transport differs from exact source, variants or accepted archive bytes')
    return data['images']


def load(root,config,environment,accepted,run):
    value=declaration(root,config)
    if value is None:return
    identities=verify(root,config,environment)
    if any(accepted.get('image:'+reference)!=identity for reference,identity in identities.items()):
        raise ValueError('fixture image identity differs from its accepted build proof')
    run(['docker','image','load','--input',str(value['directory']/'images.tar.gz')],root,environment,timeout=600)
    if inspect(list(identities),root,environment)!=identities:raise ValueError('loaded native fixture image differs from accepted immutable bytes')
