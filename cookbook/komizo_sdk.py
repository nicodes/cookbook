"""Explicitly prepare and then only inspect accepted immutable Komizo source."""
import json
import os
from pathlib import Path
import re
import subprocess

from configuration import relative, read_source_data

SDK='.artifacts/sdk/komizo'
REPOSITORY='https://github.com/nicodes/komizo-actions.git'
SHA=re.compile('[a-f0-9]{40}')


def bounded(path,maximum=32768):
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents) or not path.is_file() or path.stat().st_size>maximum:
        raise ValueError('Komizo SDK declarations must be bounded regular source files')
    return path.read_bytes()


def read(path,maximum=32768):
    bounded(path, maximum)
    return read_source_data(path, maximum=maximum)


def declaration(root,config):
    references=[]
    for role in ['delivery','preview']:
        value=config.get(role)
        if value is None:continue
        if not isinstance(value,dict) or set(value)!={'definition'}:
            raise ValueError('Komizo SDK selects only native delivery/Preview declarations')
        data=read(relative(root,value['definition'],exists=True))
        revision=data.get('komizo_ref') if isinstance(data,dict) else None
        if not isinstance(revision,str) or not SHA.fullmatch(revision):
            raise ValueError('Komizo SDK requires an immutable declared source pin')
        references.append(revision)
    if not references:return None
    if len(set(references))!=1:
        raise ValueError('delivery and Preview must use the same accepted Komizo SDK source')
    return references[0]


def source_root(root,config):
    return relative(root,SDK+'/source') if declaration(root,config) else None


def run(arguments,root,environment,timeout=180):
    subprocess.run(arguments,cwd=root,env=environment,check=True,timeout=timeout)


def capture(arguments,root,environment,timeout=30):
    return subprocess.check_output(arguments,cwd=root,env=environment,text=True,timeout=timeout).strip()


def git(source,*arguments):
    return ['git','-c','core.hooksPath='+os.devnull,'-c','core.fsmonitor=false',
            '-c','credential.helper=','-C',str(source),*arguments]


def clean_source(root,source,environment):
    if source.is_symlink() or any(parent.is_symlink() for parent in source.parents) or (source/'.git').is_symlink():
        raise ValueError('Komizo SDK source must not traverse links')
    if Path(capture(git(source,'rev-parse','--show-toplevel'),root,environment)).resolve()!=source.resolve():
        raise ValueError('Komizo SDK source must be its own prepared checkout')
    if capture(git(source,'remote','get-url','origin'),root,environment)!=REPOSITORY:
        raise ValueError('Komizo SDK checkout has another source authority')
    if capture(git(source,'status','--porcelain','--untracked-files=all','--ignored=matching'),root,environment):
        raise ValueError('Komizo SDK source contains modified or unexpected files; prepare a fresh SDK')


def check(root,config,environment):
    """Offline admission: no fetch, install, application code or network request."""
    revision=declaration(root,config)
    if revision is None:return None
    source=relative(root,SDK+'/source',exists=True)
    clean_source(root,source,environment)
    expected={'version':1,'repository':REPOSITORY,'source':revision,'accepted_main':True}
    if json.loads(bounded(relative(root,SDK+'/source.json',exists=True)))!=expected:
        raise ValueError('run mise run setup for the exact accepted Komizo SDK')
    if capture(git(source,'rev-parse','HEAD'),root,environment)!=revision:
        raise ValueError('Komizo SDK source differs from the declared immutable pin')
    return source


def setup(root,config,environment,execute=run):
    """Only explicit setup may verify acceptance and fetch the fixed public source."""
    revision=declaration(root,config)
    if revision is None:return None
    destination=relative(root,SDK)
    destination.mkdir(parents=True,mode=0o700,exist_ok=True)
    source=relative(root,SDK+'/source')
    if not source.exists():
        execute(['git','init',str(source)],root,environment)
        execute(git(source,'remote','add','origin',REPOSITORY),root,environment)
    clean_source(root,source,environment)
    relation=capture(['gh','api','repos/nicodes/komizo-actions/compare/'+revision+'...main','--jq','.status'],root,environment)
    if relation not in {'ahead','identical'}:
        raise ValueError('Komizo SDK source must first be accepted on main')
    execute(git(source,'fetch','--depth','1','--no-recurse-submodules','origin',revision),root,environment,180)
    execute(git(source,'checkout','--detach',revision),root,environment)
    expected={'version':1,'repository':REPOSITORY,'source':revision,'accepted_main':True}
    receipt=relative(root,SDK+'/source.json')
    receipt.write_text(json.dumps(expected,sort_keys=True)+'\n')
    return check(root,config,environment)
