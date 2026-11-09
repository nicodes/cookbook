"""Transfer exact native test variants separately from shipping artifacts."""
import json
import re
from configuration import relative
import artifact_packet
import dependencies
import images
import native_runtime


def declaration(root, config):
    value = config.get('components', {}).get('fixture_images')
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'directory'}:
        raise ValueError('fixture images require one generated artifact directory')
    directory = relative(root, value['directory'])
    if (not directory.is_relative_to(root / '.artifacts') or directory == root / '.artifacts'
            or directory.is_relative_to(root / '.artifacts/release')):
        raise ValueError('fixtures belong outside the closed shipping packet')
    definition = images.definition(root, config)
    shipping = images.components(definition)
    names = [record.get('name') for record in config.get('components', {}).get('images', [])]
    if (any(not isinstance(name, str) or not re.fullmatch('[a-z][a-z0-9-]{0,63}', name) for name in names)
            or len(set(names)) != len(names)):
        raise ValueError('fixture images require distinct native components')
    extra = sorted(set(names) - set(shipping))
    if not 1 <= len(extra) <= 16 or not set(shipping) <= set(names):
        raise ValueError('fixture declaration must cover actual nonshipping variants')
    return {'directory': directory, 'names': extra, 'image_base': definition['image_base']}


def artifact_paths(root, config):
    value = declaration(root, config)
    return [] if value is None else [(value['directory'] / name).relative_to(root).as_posix()
        for name in ['images.json', *[name + '.oci.tar' for name in value['names']]]]


def references(root, config, revision='{revision}'):
    value = declaration(root, config)
    if value is None:
        return []
    if revision != '{revision}' and not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('fixtures require their exact source revision')
    return [value['image_base'] + '-' + name + ':' + revision for name in value['names']]


def inspect(refs, root, environment):
    return {reference: value['Id'] for reference, value in
            zip(refs, native_runtime.inspect(refs, environment), strict=True)}


def package(root, config, environment, run):
    value = declaration(root, config)
    if value is None:
        return
    revision = environment['SOURCE_COMMIT']
    refs = references(root, config, revision)
    directory = value['directory']
    directory.mkdir(parents=True, exist_ok=True)
    records = {}
    for name, reference in zip(value['names'], refs, strict=True):
        metadata = dependencies.helper('oci_archive').save_image(reference, directory / (name + '.oci.tar'), environment=environment)
        records[reference] = {'config_digest': metadata['config_digest'],
            'manifest_digest': metadata['manifest_digest'], 'archive_sha256': metadata['archive_sha256']}
    (directory / 'images.json').write_text(json.dumps({'version': 1, 'source_commit': revision,
        'images': records}, sort_keys=True) + '\n')


def verify(root, config, environment):
    value = declaration(root, config)
    if value is None:
        return {}
    directory = value['directory']
    data = artifact_packet.read_json(directory / 'images.json', 65536)
    refs = references(root, config, environment['SOURCE_COMMIT'])
    expected_files = {'images.json', *[name + '.oci.tar' for name in value['names']]}
    if (artifact_packet.packet_files(directory) != expected_files or set(data) != {'version', 'source_commit', 'images'}
            or data['version'] != 1 or data['source_commit'] != environment['SOURCE_COMMIT']
            or not isinstance(data['images'], dict) or set(data['images']) != set(refs)):
        raise ValueError('fixture transport differs from exact source and component inventory')
    identities = {}
    for name, reference in zip(value['names'], refs, strict=True):
        metadata = dependencies.helper('oci_archive').inspect_archive(directory / (name + '.oci.tar'))
        expected = {key: metadata[key] for key in ['config_digest', 'manifest_digest', 'archive_sha256']}
        if data['images'][reference] != expected:
            raise ValueError('fixture bytes differ from their exact accepted OCI identities')
        identities[reference] = metadata['config_digest']
    return identities


def load(root, config, environment, accepted, run):
    value = declaration(root, config)
    if value is None:
        return
    identities = verify(root, config, environment)
    if any(accepted.get('image:' + reference) != identity for reference, identity in identities.items()):
        raise ValueError('fixture image differs from the accepted build proof')
    for name, reference in zip(value['names'], identities, strict=True):
        dependencies.helper('oci_archive').load_image(value['directory'] / (name + '.oci.tar'), environment=environment)
    selected = list(identities.values())
    if inspect(selected, root, environment) != {identity: identity for identity in selected}:
        raise ValueError('loaded fixture differs from accepted immutable bytes')
