"""Restore native addon dependencies with a reviewed caller-pinned registry CLI."""
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.request

from configuration import read_source_data, relative, runtime_configuration


def declaration(root, config):
    values = config.get('components', {}).get('addon_dependencies')
    if values is None:
        return None
    if not isinstance(values, dict) or set(values) != {'mise_tool', 'sha256', 'destination'}:
        raise ValueError('addon dependency setup requires a reviewed native SDK hash')
    name = values['mise_tool']
    if not isinstance(name, str) or not re.fullmatch(r'github:[A-Za-z0-9.-]+/gdam', name):
        raise ValueError('addon dependency installer must name its native registry CLI')
    tools = tomllib.loads((root / 'mise.toml').read_text()).get('tools', {})
    pin = tools.get(name)
    if (not isinstance(pin, dict) or set(pin) != {'version', 'asset_pattern', 'bin'}
            or not re.fullmatch(r'v\d+\.\d+\.\d+', pin.get('version', ''))
            or pin.get('bin') != 'gdam' or not re.fullmatch(r'[A-Za-z0-9_.-]+\.tar\.gz', pin.get('asset_pattern', ''))):
        raise ValueError('registry SDK requires one exact Mise version, archive and binary')
    if not isinstance(values['sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', values['sha256']):
        raise ValueError('registry SDK archive requires a reviewed SHA256')
    destination = relative(root, values['destination'])
    if not destination.is_relative_to(root / '.artifacts') or destination.name != 'gdam':
        raise ValueError('registry SDK binary must be an owned generated artifact')
    return values, pin, name.removeprefix('github:'), destination


def download(url, target):
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'tools-addon-dependencies'})
            with urllib.request.urlopen(request, timeout=60) as incoming, target.open('wb') as output:
                size = 0
                for chunk in iter(lambda: incoming.read(1024 * 1024), b''):
                    size += len(chunk)
                    if size > 128 * 1024 * 1024:
                        raise ValueError('registry SDK archive exceeds its bound')
                    output.write(chunk)
            return
        except (urllib.error.URLError, TimeoutError):
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)


def setup(root, config, environment, *, fetch=download, execute=subprocess.run):
    root = Path(root).resolve()
    record = declaration(root, config)
    if record is None:
        return
    values, pin, repository, destination = record
    source = {'repository': repository, 'version': pin['version'], 'asset': pin['asset_pattern'], 'sha256': values['sha256']}
    receipt = destination.with_suffix('.json')
    if receipt.is_symlink():
        raise ValueError('registry SDK receipt may not be a symlink')
    valid = False
    if destination.is_file() and not destination.is_symlink() and receipt.is_file() and not receipt.is_symlink():
        try:
            accepted = json.loads(receipt.read_text())
            valid = accepted.get('source') == source and accepted.get('binary_sha256') == hashlib.sha256(destination.read_bytes()).hexdigest()
        except (OSError, ValueError):
            pass
    if not valid:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='registry-sdk-', dir=destination.parent) as directory:
            archive = Path(directory) / pin['asset_pattern']
            fetch('https://github.com/' + repository + '/releases/download/' + pin['version'] + '/' + pin['asset_pattern'], archive)
            if hashlib.sha256(archive.read_bytes()).hexdigest() != values['sha256']:
                raise ValueError('registry SDK archive differs from its reviewed checksum')
            with tarfile.open(archive, 'r:gz') as package:
                matches = [member for member in package.getmembers() if member.name == pin['bin']]
                if len(matches) != 1 or not matches[0].isfile() or not 0 < matches[0].size <= 128 * 1024 * 1024:
                    raise ValueError('registry SDK archive lacks one regular native binary')
                binary = package.extractfile(matches[0]).read()
                if not binary:
                    raise ValueError('registry SDK binary is empty')
            candidate = Path(directory) / 'verified-gdam'
            candidate.write_bytes(binary)
            candidate.chmod(0o755)
            candidate.replace(destination)
            receipt.write_text(json.dumps({'source': source, 'binary_sha256': hashlib.sha256(binary).hexdigest()}, sort_keys=True) + '\n')
            receipt.chmod(0o600)
    version = execute([str(destination), '--version'], cwd=root, env=environment,
                      timeout=30, check=True, capture_output=True, text=True)
    if not re.search(r'\bgdam ' + re.escape(pin['version'].removeprefix('v')) + r'(?:\s|$)', version.stdout or ''):
        raise ValueError('registry SDK binary version differs from the caller Mise pin')
    runtime = runtime_configuration()
    runtime.prepare(root, 'gdam.toml', 'gdam')
    if (root / 'gdam.link.toml').exists():
        runtime.prepare(root, 'gdam.link.toml', 'gdam-link')
    manifest = read_source_data(root / 'gdam.toml')
    runtime.verify(root, 'gdam.toml', 'gdam')
    package = nullcontext()
    if 'package' in manifest:
        source = relative(root, manifest['package'], exists=True)
        if source == root or not source.is_dir():
            raise ValueError('registry package must name an owned source directory')
        package = runtime.scoped(root, (source / 'gdam.toml').relative_to(root), 'gdam')
    with package:
        execute([str(destination), 'install', '--frozen-lockfile'], cwd=root, env=environment, timeout=180, check=True)
