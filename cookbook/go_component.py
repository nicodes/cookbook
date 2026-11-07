"""Compile and test owned locked Go modules without installing SDKs in builds/tests."""
import os
from pathlib import Path
import re
import subprocess
import tomllib
from configuration import relative

PACKAGE=re.compile(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+(?:/\.\.\.)?\Z')


def declaration(root,config):
    records=config.get('components',{}).get('go',[])
    if not isinstance(records,list) or len(records)>16:raise ValueError('Go modules require a bounded native component inventory')
    outputs=set()
    for record in records:
        if (not isinstance(record,dict) or set(record)-{'directory','package','output','test_packages','environment'}
                or not {'directory','package','output'}<=set(record)):
            raise ValueError('Go components require native module, package and executable data')
        directory=relative(root,record['directory'],exists=True)
        if not directory.is_dir() or not (directory/'go.mod').is_file() or not (directory/'go.sum').is_file():
            raise ValueError('Go components require an owned locked native module')
        if not isinstance(record['package'],str) or not PACKAGE.fullmatch(record['package']) or record['package'].endswith('/...'):
            raise ValueError('Go executables require one literal native package')
        output=relative(root,record['output'])
        if output in outputs or output==directory or output.is_relative_to(directory/'third_party'):
            raise ValueError('Go executable outputs must be distinct from owned source')
        outputs.add(output)
        tests=record.get('test_packages',[])
        if not isinstance(tests,list) or len(tests)>32 or len(set(tests))!=len(tests) or any(not isinstance(value,str) or not PACKAGE.fullmatch(value) for value in tests):
            raise ValueError('Go unit coverage requires bounded literal native packages')
        values=record.get('environment',{})
        if (not isinstance(values,dict) or set(values)-{'CGO_ENABLED','GOOS','GOARCH'}
                or values.get('CGO_ENABLED','0')!='0' or values.get('GOOS','linux')!='linux'
                or values.get('GOARCH','amd64') not in {'amd64','arm64'}):
            raise ValueError('native Go runtime compilation requires an explicit static Linux target')
    return records


def artifact_paths(root,config):return [value['output'] for value in declaration(root,config)]


def environment(root,record,values,*,offline):
    pins=tomllib.loads((root/'mise.toml').read_text()).get('tools',{})
    pin=pins.get('go');pin=pin.get('version') if isinstance(pin,dict) else pin
    if not isinstance(pin,str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',pin):raise ValueError('native Go requires an exact caller Mise SDK')
    result=values|{'GOTOOLCHAIN':'local','GOFLAGS':'-mod=readonly','CGO_ENABLED':'0','GOOS':'linux','GOARCH':'amd64'}|record.get('environment',{})
    if offline:result.update(GOPROXY='off',GOSUMDB='off')
    actual=subprocess.check_output(['go','env','GOVERSION'],cwd=root,env=result,text=True,timeout=30).strip()
    if actual!='go'+pin:raise ValueError('use the exact caller-pinned Go SDK; run mise run setup first')
    return result


def setup(root,config,values,run):
    for record in declaration(root,config):
        directory=relative(root,record['directory'],exists=True)
        prepared=environment(root,record,values,offline=False)
        run(['go','mod','download'],directory,prepared)
        if record.get('test_packages'):
            # External packages can have test-only imports outside mod download's
            # lazy closure. Resolve their metadata during setup, without compiling.
            run(['go','list','-mod=readonly','-deps','-test',*record['test_packages']],directory,prepared)


def build(root,config,values,run):
    for record in declaration(root,config):
        output=relative(root,record['output']);output.parent.mkdir(parents=True,exist_ok=True)
        run(['go','build','-mod=readonly','-trimpath','-buildvcs=false','-o',str(output),record['package']],
            relative(root,record['directory'],exists=True),environment(root,record,values,offline=True))
        if output.is_symlink() or not output.is_file() or output.read_bytes()[:4]!=b'\x7fELF' or not os.access(output,os.X_OK):
            raise ValueError('native Go build did not produce its declared static Linux executable')


def test(root,config,values,run):
    for record in declaration(root,config):
        if record.get('test_packages'):
            run(['go','test','-mod=readonly',*record['test_packages']],relative(root,record['directory'],exists=True),
                environment(root,record,values,offline=True))
