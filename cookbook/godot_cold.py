"""Provide an exact-source checkout and isolated home for native setup assertions."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import dependencies


@contextmanager
def checkout(root, tools, environment):
    root=Path(root).resolve();tools=Path(tools).resolve()
    source=subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True,timeout=30).strip()
    mise=shutil.which('mise',path=environment.get('PATH'))
    if not mise:raise ValueError('cold setup requires the already-reviewed Mise executable')
    with tempfile.TemporaryDirectory(prefix='native cold setup ',dir=environment.get('RUNNER_TEMP')) as directory:
        directory=Path(directory);client=directory/'project with spaces';home=directory/'isolated development home'
        subprocess.run(['git','clone','--quiet','--no-local','--no-hardlinks',str(root),str(client)],check=True,timeout=120)
        subprocess.run(['git','-C',str(client),'checkout','--quiet','--detach',source],check=True,timeout=30)
        home.mkdir(mode=0o700)
        allowed={name:value for name,value in environment.items() if name.startswith(('GITHUB_','RUNNER_','LC_'))
                 or name in {'CI','GH_TOKEN','MISE_GITHUB_TOKEN','LANG','GITHUB_TOKEN','SOURCE_COMMIT'}}
        child={**allowed,'HOME':str(home),'PATH':'/usr/bin:/bin','TOOLS_ROOT':str(dependencies.tools_root()),'COOKBOOK_ROOT':str(tools),
               'MISE_TRUSTED_CONFIG_PATHS':str(client),'MISE_YES':'1','MISE_DATA_DIR':str(home/'mise data'),
               'MISE_CACHE_DIR':str(home/'mise cache'),'MISE_CONFIG_DIR':str(home/'mise config'),
               'MISE_STATE_DIR':str(home/'mise state'),'COOKBOOK_GODOT_ENTRY':'build','COOKBOOK_STAGE':'full'}
        yield client,child,mise
        if subprocess.check_output(['git','-C',str(client),'rev-parse','HEAD'],text=True,timeout=30).strip()!=source:
            raise ValueError('cold setup changed its exact source revision')
        subprocess.run(['git','-C',str(client),'diff','--exit-code','HEAD','--'],check=True,timeout=30)
