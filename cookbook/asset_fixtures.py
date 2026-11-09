"""Package nonshipping static test variants before build acceptance."""
import copy
from pathlib import Path
import shutil
import tempfile
import os
import hashlib

import artifact_packet
from configuration import relative
import deployment


def declaration(root, config):
    records = config.get('components', {}).get('asset_fixtures', [])
    if not isinstance(records, list) or len(records) > 16:
        raise ValueError('static fixtures require bounded native asset variants')
    result = {}
    for record in records:
        deployment.closed(record, {'name', 'source', 'prepare'}, 'static fixture', {'name', 'source'})
        name = record['name']
        if not isinstance(name, str) or not deployment.NAME.fullmatch(name) or name in result:
            raise ValueError('static fixtures require distinct native variant names')
        relative(root, record['source'])
        result[name] = record
    if result and len(config.get('components', {}).get('assets', [])) != 1:
        raise ValueError('static fixture variants require one explicit static asset')
    return result


def artifact_paths(root, config):
    return ['.artifacts/fixture-releases/' + name for name in declaration(root, config)]


def directory(root, config, variant):
    if variant not in declaration(root, config):
        raise ValueError('static fixture must select a declared accepted asset variant')
    return relative(root, '.artifacts/fixture-releases/' + variant, exists=True)


def package(root, config, environment, native=None):
    variants = declaration(root, config)
    if not variants:
        return
    canonical = relative(root, '.artifacts/release', exists=True)
    release = deployment.verify(root, environment)
    original = config['components']['assets'][0]
    name = original['name']
    destination = relative(root, '.artifacts/fixture-releases')
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    for variant, record in variants.items():
        if record.get('prepare'):
            if native is None:
                raise ValueError('static fixture preparation requires its owning native recipe runner')
            native(root, record['prepare'], environment)
        with tempfile.TemporaryDirectory(prefix='.fixture-', dir=destination) as scratch:
            target = Path(scratch)
            clone = copy.deepcopy(release)
            members = {'release.json': 'release'}
            for path in artifact_packet.packet_files(canonical) - {'inventory.json', 'release.json', 'static/' + name + '.tar.gz'}:
                source = canonical / path
                output = target / path
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, output)
                members[path] = 'image' if path.startswith('images/') else 'metadata'
            path = 'static/' + name + '.tar.gz'
            metadata = artifact_packet.pack_static(relative(root, record['source'], exists=True), target / path,
                max_bytes=original['max_bytes'], max_file_bytes=original['max_file_bytes'],
                max_files=original.get('max_files', artifact_packet.MAX_MEMBERS))
            digest = 'sha256:' + metadata['sha256']
            manifest = deployment.static_manifest(digest, metadata['size'])
            artifact = clone['artifacts'][name]
            artifact.update(digest=digest, size_bytes=metadata['size'],
                reference=artifact['reference'].split('@')[0] + '@sha256:' + hashlib.sha256(manifest).hexdigest())
            members[path] = 'static'
            (target / 'release.json').write_bytes(deployment.canonical(clone))
            deployment.native_validate(root, target / 'release.json', environment)
            artifact_packet.write_inventory(target, members)
            selected = destination / variant
            if selected.is_symlink():
                raise ValueError('static fixture destinations may not be linked')
            if selected.exists():
                shutil.rmtree(selected)
            os.replace(target, selected)
