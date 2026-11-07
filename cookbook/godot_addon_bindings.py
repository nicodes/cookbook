"""Explicit caller-pinned native addon generation; no dependency work in tests."""
import os
from pathlib import Path
import re
import subprocess
import tomllib


def declaration(root, config):
    selected = config.get('components', {}).get('godot', {}).get('addon_bindings')
    if selected is None:
        return None
    if not isinstance(selected, dict) or set(selected) != {'tool'} or selected['tool'] != 'github:aviorstudio/gdam':
        raise ValueError('addon bindings must explicitly select the public native GDAM SDK')
    pin = tomllib.loads((root / 'mise.toml').read_text()).get('tools', {}).get(selected['tool'])
    if (not isinstance(pin, dict) or set(pin) != {'version', 'asset_pattern', 'bin'} or pin['bin'] != 'gdam'
            or not isinstance(pin['version'], str) or not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', pin['version'])
            or not isinstance(pin['asset_pattern'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}\.tar\.gz', pin['asset_pattern'])):
        raise ValueError('native addon generation requires the exact caller release, archive and binary pin')
    return {'tool': selected['tool'], 'pin': pin}


def installed(root, config, environment):
    selected = declaration(root, config)
    if selected is None:
        return None
    # `where` is a read-only lookup. Installation belongs to mise run setup.
    directory = Path(subprocess.check_output(['mise', 'where', selected['tool']], cwd=root,
        env=environment, text=True, timeout=30).strip())
    if not directory.is_absolute() or not directory.is_dir():
        raise ValueError('native addon CLI must already be installed by setup')
    binaries = [directory / 'bin/gdam', directory / 'gdam']
    found = [path for path in binaries if path.is_file() and not path.is_symlink() and os.access(path, os.X_OK)]
    if len(found) != 1:
        raise ValueError('native addon SDK must resolve one regular installed CLI')
    return {**selected, 'binary': found[0]}
