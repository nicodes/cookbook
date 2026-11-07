#!/usr/bin/env python3
"""Read caller-declared native diagnostic locations without executing caller code."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib



def generated_report(root, path, config):
    """Admit a narrow generated report location, never a tracked source tree."""
    reports = len(path.parts) >= 2 and re.fullmatch('[A-Za-z][A-Za-z0-9_-]*-reports', path.parts[0])
    project = config.get('components', {}).get('godot', {}).get('project', 'app')
    build = False
    if isinstance(project, str) and (project == '.' or re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*', project)):
        project_path = Path(project)
        if not any(part in {'.', '..', '.git'} for part in project_path.parts):
            owned_build = project_path / 'build'
            build = path.is_relative_to(owned_build) and path != owned_build
            if build:
                source = root / project_path / 'project.godot'
                if (not source.is_file() or any(item.is_symlink() for item in [source, *source.parents])):
                    raise ValueError('native build reports require the configured owned Godot project')
    if not (reports or build):
        return False
    try:
        prefix = subprocess.check_output(['git', '-C', str(root), 'rev-parse', '--show-prefix'],
            stderr=subprocess.DEVNULL, timeout=30)
        tracked = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z', '--', path.as_posix()],
            stderr=subprocess.DEVNULL, timeout=30)
        if build:
            subprocess.check_output(['git', '-C', str(root), 'ls-files', '--error-unmatch', '--',
                (Path(project) / 'project.godot').as_posix()], stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError('native report locations require an actual source checkout') from error
    if prefix.strip() or tracked:
        raise ValueError('native report locations may not select tracked source files')
    return True


def declaration(root, config, *, inspect=False, status=None, entry=None):
    if config.get('recipe')!='godot-game':return []
    values=config.get('artifacts',{})
    registered=values.get('evidence_paths',[])
    retained=values.get('evidence_retention_days',{})
    if (not isinstance(registered,list) or len(registered)>64 or any(not isinstance(path,str) for path in registered)
            or len(set(registered))!=len(registered) or not isinstance(retained,dict) or len(retained)>64
            or set(retained)-set(registered)):
        raise ValueError('native outputs select only explicitly registered evidence locations')
    if status not in {None,'success','failure','cancelled'}:raise ValueError('native output retention requires the actual job status')
    if entry is not None and (not isinstance(entry,str) or not re.fullmatch('[a-z][a-z0-9-]{0,79}',entry)):
        raise ValueError('native output requirements need a literal worker identity')
    root=Path(root).absolute()
    policies={}
    for name,policy in retained.items():
        if type(policy) is int:policy={'days':policy,'when':'always'}
        if (not isinstance(policy,dict) or set(policy)-{'days','when','required_for'} or type(policy.get('days')) is not int
                or not 0<=policy['days']<=90 or policy.get('when','always') not in {'always','success','failure'}):
            raise ValueError('native output retention requires bounded days and success/failure/always admission')
        required_for=policy.get('required_for',[])
        gates=config.get('ci',{}).get('godot',{}).get('gates',[])
        known={'gate-'+row['name'] for row in gates if isinstance(row,dict) and isinstance(row.get('name'),str)
            and re.fullmatch('[a-z][a-z0-9-]{1,63}',row['name'])} if isinstance(gates,list) else set()
        if (not isinstance(required_for,list) or len(required_for)>32
                or ('required_for' in policy and not required_for)
                or any(not isinstance(value,str) or value not in known for value in required_for)
                or len(set(required_for))!=len(required_for)):
            raise ValueError('required native reports select distinct actual declared gates')
        if inspect and required_for and (entry is None or status is None):
            raise ValueError('required native report inspection needs actual worker and job status')
        required=inspect and entry in required_for and policy.get('when','always') in {'always',status}
        path=Path(name)
        if (not name or len(name)>240 or not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*/?',name)
                or any(part in {'.','..','.git'} for part in path.parts)
                or not ((path.parts[0] in {'.artifacts','tmp'} and len(path.parts)>1)
                    or re.fullmatch('[A-Za-z][A-Za-z0-9_-]*-evidence',path.parts[0])
                    or generated_report(root,path,config))):
            raise ValueError('native outputs require explicit generated output locations')
        target=root/path
        if any(value.is_symlink() for value in [target,*target.parents]):
            raise ValueError('native output locations may not traverse links')
        if any(path==other or path.is_relative_to(other) or other.is_relative_to(path) for other in policies):
            raise ValueError('native output locations must be distinct nonoverlapping paths')
        policies[path]=policy
        if required and not target.exists():
            raise ValueError('required native report is missing: '+name)
        if inspect and target.exists():
            count,total,files=0,0,0
            entries=target.rglob('*') if target.is_dir() else [target]
            for value in entries:
                count+=1
                if count>20000 or value.is_symlink() or not (value.is_dir() or value.is_file()):
                    raise ValueError('native output must be a bounded unlinked regular file or tree')
                if value.is_file():
                    files+=1
                    size=value.stat().st_size;total+=size
                    if size>2<<30 or total>16<<30:raise ValueError('native output exceeds the retained evidence size bound')
            if required and not files:
                raise ValueError('required native report contains no files: '+name)
    days={value['days'] for value in policies.values()}
    if len(days)>3:raise ValueError('native output retention supports at most three native retention groups')
    groups=[]
    for retention in sorted(days):
        paths=sorted(path.as_posix() for path,policy in policies.items() if policy['days']==retention
            and (status is None or policy.get('when','always') in {'always',status}))
        if paths:groups.append({'days':retention,'paths':paths})
    return groups


def read(root, *, inspect=False, expected=None, status=None, entry=None):
    root=Path(root)
    source=root/'cookbook.toml'
    if source.is_symlink() or not source.is_file() or source.stat().st_size>262144:
        raise ValueError('native diagnostic declarations require bounded regular cookbook data')
    body=source.read_bytes()
    if expected is not None and (not re.fullmatch('[a-f0-9]{64}',expected) or hashlib.sha256(body).hexdigest()!=expected):
        raise ValueError('native diagnostic declaration changed during validation')
    return declaration(root,tomllib.loads(body.decode()),inspect=inspect,status=status,entry=entry)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path.cwd())
    parser.add_argument('--inspect',action='store_true')
    parser.add_argument('--expected-sha256')
    parser.add_argument('--status',choices=['success','failure','cancelled'])
    parser.add_argument('--entry')
    args=parser.parse_args()
    groups=read(args.root,inspect=args.inspect,expected=args.expected_sha256,status=args.status,entry=args.entry)
    if os.environ.get('GITHUB_OUTPUT'):
        identity=hashlib.sha256((args.root/'cookbook.toml').read_bytes()).hexdigest()
        with open(os.environ['GITHUB_OUTPUT'],'a') as output:
            def paths(name,values):output.write(name+'<<COOKBOOK_NATIVE_EVIDENCE_PATHS\n'+'\n'.join(values)+'\nCOOKBOOK_NATIVE_EVIDENCE_PATHS\n')
            paths('paths',[path for group in groups for path in group['paths']])
            for index in range(1,4):
                group=groups[index-1] if index<=len(groups) else {'days':0,'paths':[]}
                paths('paths-'+str(index),group['paths'])
                output.write('retention-days-'+str(index)+'='+str(group['days'])+'\n')
            output.write('declaration-sha256='+identity+'\n')
    else:print(json.dumps({'scope':'native-owned-outputs','groups':groups}))
