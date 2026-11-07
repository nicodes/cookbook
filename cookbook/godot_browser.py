"""Serve accepted static images over local HTTPS for owned browser assertions."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import tomllib

from configuration import relative
import images

import dependencies

COOKBOOK = Path(__file__).resolve().parents[1]
TOOLS = dependencies.tools_root()


def declaration(root, config):
    value = config.get('components', {}).get('browser')
    fields = {'package','hostname','reports','runner','cache_runner','startup_runner','tls_proxy','cli_tool','browsers'}
    if not isinstance(value, dict) or set(value)-{'journeys','forward_environment'} != fields:
        raise ValueError('browser fixture requires exact native source and host data')
    if not re.fullmatch(r'[a-z0-9.-]+', value['hostname']):
        raise ValueError('browser fixture requires its literal application hostname')
    for key in ['runner','cache_runner','startup_runner','tls_proxy']:
        path = relative(root, value[key], exists=True)
        if (not path.is_file() or path.suffix != '.mjs' or not any(part in {'test','tests'} for part in path.relative_to(root).parts)):
            raise ValueError('browser assertions and fixture programs belong to owned tests')
    forwarded=value.get('forward_environment',[])
    if (not isinstance(forwarded,list) or len(forwarded)>32 or len(set(forwarded))!=len(forwarded)
            or any(not isinstance(name,str) or not re.fullmatch('[A-Z][A-Z0-9_]*',name)
                   or name in {'GH_TOKEN','GITHUB_TOKEN','TOOLS_VERIFICATION_SESSION'}
                   or name.startswith(('GITHUB_','MISE_','COOKBOOK_','REQUEST_')) for name in forwarded)):
        raise ValueError('browser fixture forwarding requires bounded literal native parameter names')
    journeys=value.get('journeys',[])
    if not isinstance(journeys,list) or len(journeys)>8:
        raise ValueError('browser image journeys require bounded native declarations')
    names=set();paths=set()
    shipping=set(images.components(images.definition(root,config))) if journeys else set()
    if journeys:
        import image_fixtures
        fixtures=image_fixtures.declaration(root,config)
        shipping.update(fixtures['names'] if fixtures else [])
    for journey in journeys:
        if (not isinstance(journey,dict) or set(journey)!={'name','path','image'}
                or not re.fullmatch('[a-z][a-z0-9-]{0,63}',journey.get('name',''))
                or journey['name'] in names or journey['path'] in paths or journey['image'] not in shipping):
            raise ValueError('browser journeys require distinct native programs and declared shipping or accepted fixture image components')
        path=relative(root,journey['path'],exists=True)
        if (not path.is_file() or path.suffix!='.mjs' or not any(part in {'tests','test'} for part in path.relative_to(root).parts)):
            raise ValueError('browser image journey must identify its real owned assertion program')
        names.add(journey['name']);paths.add(journey['path'])
    package = relative(root, value['package'], exists=True)
    if not (package/'package.json').is_file() or not any(record['directory']==value['package'] for record in config.get('node',{}).get('packages',[])):
        raise ValueError('browser fixture requires one declared locked native package')
    relative(root, value['reports'])
    if value['browsers'] != ['chromium','firefox','webkit']:
        raise ValueError('canonical image browser coverage must retain its three native browsers')
    mise = tomllib.loads((root/'mise.toml').read_text())
    pin = mise.get('tools',{}).get(value['cli_tool'])
    if (not re.fullmatch(r'github:[A-Za-z0-9.-]+/gd-playwright-cli',value['cli_tool'])
            or not isinstance(pin,dict) or set(pin)!={'version','asset_pattern','bin'}
            or not re.fullmatch(r'cli-v[0-9]+\.[0-9]+\.[0-9]+',pin.get('version',''))
            or pin.get('asset_pattern')!='gd-playwright-cli_Linux_x86_64.tar.gz' or pin.get('bin')!='gd-playwright-cli'):
        raise ValueError('browser CLI requires its exact native Mise release pin')
    pins = mise.get('env',{})
    checksum, runtime = pins.get('GD_PLAYWRIGHT_ASSET_SHA256',''), pins.get('GODOT_BROWSER_RUNTIME_IMAGE','')
    if (not re.fullmatch('[a-f0-9]{64}',checksum)
            or not re.fullmatch(r'mcr\.microsoft\.com/playwright@sha256:[a-f0-9]{64}',runtime)):
        raise ValueError('browser CLI archive and runtime image pins belong to Mise')
    return value,pin,checksum,runtime


def setup(root, config, environment, *, capability=False, public_browsers=None):
    value,pin,checksum,runtime = declaration(root,config)
    destination=relative(root,'.artifacts/browser-sdk/gd-playwright-cli');receipt=relative(root,'.artifacts/browser-sdk/gd-playwright-cli.json')
    source={'repository':value['cli_tool'][7:],'version':pin['version'],'asset':pin['asset_pattern'],'sha256':checksum}
    if destination.is_symlink() or receipt.is_symlink():raise ValueError('browser SDK cache may not contain links')
    valid=False
    if destination.is_file() and receipt.is_file():
        accepted=json.loads(receipt.read_text())
        valid=accepted.get('source')==source and accepted.get('binary_sha256')==hashlib.sha256(destination.read_bytes()).hexdigest()
    if not valid:
        destination.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='browser-sdk-',dir=destination.parent) as directory:
            archive=Path(directory)/pin['asset_pattern']
            url='https://github.com/'+source['repository']+'/releases/download/'+pin['version']+'/'+pin['asset_pattern']
            subprocess.run(['curl','-fsSL','--connect-timeout','20','--max-time','120','--retry','2',url,'-o',str(archive)],check=True,timeout=380)
            if archive.stat().st_size>128<<20 or hashlib.sha256(archive.read_bytes()).hexdigest()!=checksum:
                raise ValueError('browser CLI archive differs from its reviewed checksum')
            with tarfile.open(archive,'r:gz') as package:
                matches=[member for member in package.getmembers() if member.name==pin['bin']]
                if len(matches)!=1 or not matches[0].isfile() or not 0<matches[0].size<=128<<20:
                    raise ValueError('browser SDK archive lacks its exact regular executable')
                binary=package.extractfile(matches[0]).read()
            destination.write_bytes(binary);destination.chmod(0o755)
            receipt.write_text(json.dumps({'source':source,'binary_sha256':hashlib.sha256(binary).hexdigest()},sort_keys=True)+'\n')
    package=root/value['package'];playwright=package/'node_modules/.bin/playwright'
    if not playwright.is_file():raise ValueError('browser setup requires its installed locked native package')
    cache=relative(root,'.artifacts/playwright');cache.mkdir(parents=True,exist_ok=True)
    values={'PLAYWRIGHT_BROWSERS_PATH':str(cache),'GDPW_BIN':str(destination),'BROWSER_CACHE':str(cache)}
    if capability:
        selected=public_browsers or ['chromium']
        if selected not in [['chromium'],['chromium','firefox','webkit']]:
            raise ValueError('public browser SDK setup must retain its literal declared engines')
        values['BROWSER_RUNTIME_IMAGE']=''
        subprocess.run([str(playwright),'install','--with-deps',*selected],cwd=package,env={**environment,**values},check=True,timeout=600)
    else:
        values['BROWSER_RUNTIME_IMAGE']=runtime
        subprocess.run(['docker','pull',runtime],cwd=root,env=environment,check=True,timeout=300)
        subprocess.run([str(playwright),'install',*value['browsers']],cwd=package,env={**environment,**values},check=True,timeout=300)
    environment.update(values);os.environ.update(values)


def journeys(root, config, environment, entry=None):
    value,_pin,_checksum,runtime = declaration(root,config)
    executable=relative(root,'.artifacts/browser-sdk/gd-playwright-cli',exists=True)
    receipt_path=relative(root,'.artifacts/browser-sdk/gd-playwright-cli.json',exists=True)
    if receipt_path.stat().st_size>1<<20:raise ValueError('browser SDK receipt exceeds its bound')
    receipt=json.loads(receipt_path.read_text())
    expected={'repository':value['cli_tool'][7:],'version':_pin['version'],'asset':_pin['asset_pattern'],'sha256':_checksum}
    if (receipt.get('source')!=expected or not executable.is_file() or executable.is_symlink()
            or receipt.get('binary_sha256')!=hashlib.sha256(executable.read_bytes()).hexdigest()):
        raise ValueError('reviewed browser SDK is absent or changed; run mise run setup first')
    if environment.get('BROWSER_RUNTIME_IMAGE',runtime)!=runtime:
        raise ValueError('browser runtime differs from the caller SDK pin')
    selected=entry['path'] if entry else value['runner']
    approved={value['runner'],value['cache_runner'],value['startup_runner'],*[item['path'] for item in value.get('journeys',[])]}
    if selected not in approved:
        raise ValueError('browser worker must run one declared owned assertion program')
    values={**environment,'GODOT_BROWSER_HOSTNAME':value['hostname'],'GODOT_BROWSER_REPORTS':value['reports'],
        'GODOT_BROWSER_RUNNER':selected,'GODOT_BROWSER_CACHE_RUNNER':value['cache_runner'],
        'GODOT_BROWSER_STARTUP_RUNNER':value['startup_runner'],'GODOT_BROWSER_TLS_PROXY':value['tls_proxy'],
        'BROWSER_RUNTIME_IMAGE':runtime,'BROWSER_CACHE':str(root/'.artifacts/playwright'),'GDPW_BIN':str(executable),
        'GODOT_BROWSER_FORWARD_ENVIRONMENT':' '.join(value.get('forward_environment',[]))}
    if entry is not None:
        journeys=value.get('journeys') or [{'name':'canonical','path':value['runner'],'image':'gate'}]
        matched=[journey for journey in journeys if journey['path']==selected]
        if len(matched)!=1:
            raise ValueError('canonical browser coverage must execute one declared image journey')
        if value.get('journeys'):
            definition=images.definition(root,config)
            source=environment.get('SOURCE_COMMIT','')
            if not re.fullmatch('[a-f0-9]{40}',source):raise ValueError('browser image journey requires its exact built source')
            values['GATE_IMAGE']=definition['image_base']+'-'+matched[0]['image']+':'+source
        values.pop('BROWSER_EXECUTABLE',None)
        values.update(BROWSER=' '.join(value['browsers']),BROWSER_TEST_MODE='',BROWSER_ROUTE_CYCLES='0',BROWSER_SOAK_SECONDS='0',BROWSER_STARTUP='0')
    subprocess.run(['bash',str(TOOLS/'helpers/godot-browser.sh')],cwd=root,env=values,check=True,
                   timeout=entry.get('timeout',1800) if entry else 1800)
