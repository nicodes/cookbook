"""Explicitly prepare browsers from caller-owned locked Node packages."""
from pathlib import Path
import node_component


def selection(root, config, packages, browsers):
    if (not isinstance(packages, list) or not 1 <= len(packages) <= 16
            or any(not isinstance(name, str) for name in packages) or len(set(packages)) != len(packages)):
        raise ValueError('browser SDK setup selects distinct locked native package directories')
    if (not isinstance(browsers, list) or not 1 <= len(browsers) <= 3
            or any(name not in {'chromium','firefox','webkit'} for name in browsers)
            or len(set(browsers)) != len(browsers)):
        raise ValueError('browser SDK setup selects distinct actual Playwright engines')
    known = {record['directory']: record for value in config.get('node', {}).get('packages', [])
             for record, _ in [node_component.validate(value, root)]}
    if not set(packages) <= known.keys():
        raise ValueError('browser SDK setup requires its real declared locked Node package')
    return [known[name] for name in packages]


def declaration(root, config):
    value = config.get('components', {}).get('playwright')
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'packages','browsers'}:
        raise ValueError('plain browser SDK setup requires package and engine data only')
    selection(root, config, value['packages'], value['browsers'])
    return value


def install(root, config, environment, run, packages, browsers):
    for package in selection(root, config, packages, browsers):
        directory = root/package['directory']
        module = directory/'node_modules/playwright/package.json'
        if not module.is_file() or not module.resolve().is_relative_to((directory/'node_modules').resolve()):
            raise ValueError('prepare the declared locked Playwright package before browser SDK setup')
        command = (['bun','x','--no-install','playwright'] if package.get('manager','npm')=='bun'
                   else ['npm','exec','--no','--','playwright'])
        # Hosted runners prepare their OS libraries in setup. Local setup keeps
        # the original no-elevation behavior and uses installed system libraries.
        flags = ['--with-deps'] if environment.get('GITHUB_ACTIONS')=='true' else []
        run([*command,'install',*flags,*browsers],directory,environment,timeout=600)


def setup(root, config, environment, run):
    value = declaration(root, config)
    if value is not None:
        install(root,config,environment,run,value['packages'],value['browsers'])
