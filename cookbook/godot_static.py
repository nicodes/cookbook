"""Hash-close native static host configuration before trusted publication packaging."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tomllib

from configuration import relative, parse_source_data
from recipe_contract import release_definition

import dependencies

COOKBOOK = Path(__file__).resolve().parents[1]
TOOLS = dependencies.tools_root()
CONFIG_ARTIFACT='.artifacts/release/static-config.json'
MANIFEST_ARTIFACT='.artifacts/release/godot-release-manifest.json'
SDK='.artifacts/godot-static-sdk'
PLACEHOLDER='sha256:'+'0'*64
DOCKERFILE='FROM scratch\nCOPY compose.yml hostnames /config/\nCMD ["/config/compose.yml"]\n'


def sha256(data):return hashlib.sha256(data).hexdigest()

def read(path,maximum=262144):
    if (path.is_symlink() or not path.is_file() or any(parent.is_symlink() for parent in path.parents)
            or path.stat().st_size>maximum):raise ValueError('static configuration requires bounded regular files')
    return path.read_bytes()


def declaration(root,config):
    selected=config.get('delivery')
    if not isinstance(selected,dict) or 'definition' not in selected:return None
    data=parse_source_data(read(relative(root,selected['definition'],exists=True),32768), maximum=32768)
    if data.get('mode')!='godot-static':return None
    if config.get('recipe')!='godot-game':raise ValueError('static configuration publication requires a Godot game')
    static=data.get('static')
    required={'config','compose','release_manifest','record','recovery','production'}
    if not isinstance(static,dict) or not required<=set(static) or set(static)-required-{'recovery_test','production_inputs','production_current_source'}:
        raise ValueError('static delivery requires exact native config, compose, manifest and lifecycle data')
    if not re.fullmatch('[a-f0-9]{40}',data.get('komizo_ref','')):raise ValueError('static parser SDK requires its accepted immutable Komizo pin')
    for key in ['config','compose']:
        name=static[key]
        if (not isinstance(name,str) or not name.startswith('deploy/') or Path(name).suffix not in ({'.toml'} if key == 'config' else {'.yml','.yaml'})):
            raise ValueError('static host configuration belongs in native deploy source')
        relative(root,name,exists=True)
    relative(root,static['release_manifest'])
    if static['config']==static['compose']:raise ValueError('static native manifest and compose source must be distinct')
    if any(type(static[key]) is not bool for key in ['record','recovery','production']) or (static['recovery'] and not static['record']):
        raise ValueError('static lifecycle capabilities must preserve explicit real release records')
    if static['recovery']:
        selected=[entry for entry in config.get('tests',{}).get('integration',[]) if entry.get('name',entry['path'])==static.get('recovery_test')]
        if len(selected)!=1 or selected[0].get('runner') not in {'bash','python'}:
            raise ValueError('static recovery must select one existing owned native shipping fixture')
    if static['production']:
        names=static.get('production_inputs',{'source':'expected-source-commit','release':'expected-release-id'})
        if (not isinstance(names,dict) or set(names)!={'source','release'} or len(set(names.values()))!=2
                or any(not isinstance(name,str) or not re.fullmatch('[a-z][a-z0-9_-]{0,63}',name) for name in names.values())
                or type(static.get('production_current_source',True)) is not bool):
            raise ValueError('static public verification must preserve its native dispatch and source policy')
    definition=parse_source_data(read(relative(root,release_definition(config),exists=True)))
    repository=definition.get('image_base','').removeprefix('ghcr.io/')
    if not re.fullmatch('[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository):raise ValueError('static images require their exact native repository family')
    if definition['image_base']!='ghcr.io/'+repository or definition.get('project')!=data.get('project'):
        raise ValueError('static delivery and artifact image authority disagree')
    return {**static,'repository':repository,'komizo_ref':data['komizo_ref']}


def run(argv,root,environment,timeout=180):
    return subprocess.run(argv,cwd=root,env=environment,check=True,timeout=timeout)


def capture(argv,root,environment,timeout=60):
    return subprocess.check_output(argv,cwd=root,env=environment,text=True,timeout=timeout)


def setup(root,config,environment,execute=run):
    selected=declaration(root,config)
    if selected is None:return
    destination=relative(root,SDK);destination.mkdir(parents=True,exist_ok=True)
    import komizo_sdk
    source=komizo_sdk.setup(root,config,environment,execute)
    requirements=source/'requirements-yaml.txt';read(requirements,65536)
    interpreter=destination/'parser/bin/python'
    receipt=destination/'parser.json'
    pins=tomllib.loads((root/'mise.toml').read_text())['tools']
    python=pins['python'] if isinstance(pins['python'],str) else pins['python']['version']
    expected={'source':selected['komizo_ref'],'requirements_sha256':sha256(requirements.read_bytes()),'python':python}
    if receipt.is_file() and json.loads(read(receipt))==expected and interpreter.is_file():return
    execute([sys.executable,'-m','venv','--copies',str(destination/'parser')],root,environment)
    execute([str(interpreter),'-I','-m','pip','--isolated','install','--disable-pip-version-check','--no-deps','--require-hashes','--only-binary=:all:',
             '--index-url','https://pypi.org/simple','-r',str(requirements)],root,environment,180)
    receipt.write_text(json.dumps(expected,sort_keys=True)+'\n')


def checked_sdk(root,selected,environment,config=None):
    destination=relative(root,SDK,exists=True)
    import komizo_sdk
    source=komizo_sdk.check(root,config,environment)
    interpreter=relative(root,SDK+'/parser/bin/python',exists=True)
    receipt=json.loads(read(destination/'parser.json'))
    pins=tomllib.loads((root/'mise.toml').read_text())['tools'];python=pins['python'] if isinstance(pins['python'],str) else pins['python']['version']
    expected={'source':selected['komizo_ref'],'requirements_sha256':sha256(read(source/'requirements-yaml.txt',65536)),'python':python}
    if receipt!=expected:raise ValueError('static parser SDK setup is absent or stale')
    return interpreter,source


def bind_compose(text,gate_ref):
    if not re.fullmatch(r'ghcr\.io/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+-gate@sha256:[a-f0-9]{64}',gate_ref):
        raise ValueError('static configuration requires the exact verified immutable gate reference')
    result,count=re.subn(r'(?m)^([ \t]+image:[ \t]+)[^\r\n]+$',lambda found:found[1]+gate_ref,text)
    if count!=1:raise ValueError('static configuration must contain exactly one native gate image field')
    return result


def validate_stack(stack,repository,gate_ref):
    helpers=str(TOOLS/'helpers')
    if helpers not in sys.path:sys.path.insert(0,helpers)
    spec=importlib.util.spec_from_file_location('static_rollback_contract',TOOLS/'helpers/godot_rollback_config.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.REPO=repository;module.validate_stack(stack,gate_ref)


def normalized(root,text,environment):
    with tempfile.TemporaryDirectory(prefix='static-compose-') as directory:
        compose=Path(directory)/'compose.yml';compose.write_text(text)
        return json.loads(capture(['docker','compose','--project-name','godot-static','-f',str(compose),'config','--format','json','--no-interpolate'],root,environment))


def manifest(root,selected,source):
    data=json.loads(read(relative(root,selected['release_manifest'],exists=True)))
    if data.get('source_commit')!=source or not re.fullmatch('[a-f0-9]{64}',data.get('release_id','')):
        raise ValueError('static public release manifest differs from this immutable source')
    return data


def stage(root,config,environment,execute=run):
    selected=declaration(root,config)
    if selected is None:return
    source=environment['SOURCE_COMMIT']
    if not re.fullmatch('[a-f0-9]{40}',source):raise ValueError('static blueprints require the full source revision')
    interpreter,sdk=checked_sdk(root,selected,environment,config)
    configs={}
    for key in ['config','compose']:
        name=selected[key];path=relative(root,name,exists=True)
        subprocess.run(['git','ls-files','--error-unmatch','--',name],cwd=root,check=True,stdout=subprocess.DEVNULL,timeout=10)
        body=read(path);configs[key]={'path':name,'sha256':sha256(body),'text':body.decode()}
    with tempfile.TemporaryDirectory(prefix='static-hostnames-') as directory:
        names=Path(directory)/'hostnames'
        compose=capture([str(interpreter),'-I',str(sdk/'publish-config/parse-config.py'),str(root/selected['config']),str(names)],root,environment).strip()
        if Path(compose).resolve()!=relative(root,selected['compose'],exists=True):raise ValueError('native static manifest selects another compose source')
        hostnames=read(names,65536).decode()
    if not hostnames.strip():raise ValueError('static public delivery requires explicit validated hostnames')
    gate='ghcr.io/'+selected['repository']+'-gate@'+PLACEHOLDER
    stack=normalized(root,bind_compose(configs['compose']['text'],gate),environment)
    validate_stack(stack,selected['repository'],gate)
    public=manifest(root,selected,source)
    blueprint={'version':1,'repository':selected['repository'],'source_commit':source,'config':configs['config'],
               'compose':configs['compose'],'hostnames':hostnames,'production_stack':stack}
    destination=relative(root,CONFIG_ARTIFACT);destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(blueprint,sort_keys=True)+'\n')
    relative(root,MANIFEST_ARTIFACT).write_text(json.dumps(public,sort_keys=True)+'\n')
    verify(root,config,environment)


def verify_blueprint(root,config,environment):
    selected=declaration(root,config)
    if selected is None:return
    blueprint=json.loads(read(relative(root,CONFIG_ARTIFACT,exists=True),1048576))
    if set(blueprint)!={'version','repository','source_commit','config','compose','hostnames','production_stack'} or blueprint['version']!=1:
        raise ValueError('unexpected static configuration blueprint fields')
    if blueprint['repository']!=selected['repository'] or blueprint['source_commit']!=environment['SOURCE_COMMIT']:
        raise ValueError('static blueprint belongs to another source or image family')
    for key in ['config','compose']:
        source=read(relative(root,selected[key],exists=True))
        if blueprint[key]!={'path':selected[key],'sha256':sha256(source),'text':source.decode()}:
            raise ValueError('static blueprint differs from its reviewed native source')
    if not isinstance(blueprint['hostnames'],str) or not 1<=len(blueprint['hostnames'])<=65536:
        raise ValueError('static blueprints require bounded validated hostnames')
    gate='ghcr.io/'+selected['repository']+'-gate@'+PLACEHOLDER
    validate_stack(blueprint['production_stack'],selected['repository'],gate)
    return blueprint


def verify(root,config,environment,execute=run):
    blueprint=verify_blueprint(root,config,environment)
    if blueprint is None:return
    selected=declaration(root,config)
    observed=json.loads(read(relative(root,MANIFEST_ARTIFACT,exists=True)))
    if observed!=manifest(root,selected,environment['SOURCE_COMMIT']):raise ValueError('static release identity metadata differs from its actual public export')
    return blueprint


def materialize(blueprint,gate_ref,directory,environment,root=None):
    """The sole publication substitution is the already-pushed, verified gate digest."""
    root=Path(root or Path.cwd());repository=blueprint['repository']
    directory=Path(directory)
    if directory.exists():raise ValueError('static publication packaging requires a fresh private destination')
    if not gate_ref.startswith('ghcr.io/'+repository+'-gate@'):
        raise ValueError('published static gate differs from the accepted repository family')
    text=bind_compose(blueprint['compose']['text'],gate_ref)
    stack=normalized(root,text,environment);validate_stack(stack,repository,gate_ref)
    expected=json.loads(json.dumps(blueprint['production_stack']))
    expected['services'][repository.rsplit('/',1)[-1]+'-gate']['image']=gate_ref
    if stack!=expected:raise ValueError('publication changed static host configuration beyond its verified gate digest')
    directory.mkdir(mode=0o700,parents=True)
    (directory/'compose.yml').write_text(text);(directory/'hostnames').write_text(blueprint['hostnames'])
    return {'compose.yml':sha256(text.encode()),'hostnames':sha256(blueprint['hostnames'].encode())}
