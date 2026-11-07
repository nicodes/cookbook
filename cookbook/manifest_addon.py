"""Build closed addon archives with optional embedded per-file provenance."""
import argparse
import configparser
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import zipfile

from configuration import load, relative, read_source_data
from godot_addon import package_data

import dependencies

COOKBOOK = Path(__file__).resolve().parents[1]
TOOLS = dependencies.tools_root()


def package_helper():
    return dependencies.helper('addon-package')


def inputs(root, config):
    values = package_data(config)
    source = relative(root, values['source'], exists=True)
    names = package_helper().manifest_files(relative(root, values['manifest'], exists=True))
    archive = relative(root, values['archive'])
    if not values.get('embedded_manifest'):
        actual = package_helper().source_files(source, relative(root, values['manifest'], exists=True),
                                                values.get('script_uids', False))
        if archive.resolve().is_relative_to(source.resolve()):
            raise ValueError('addon package output must be outside source')
        return actual, '', None, archive, None
    prefix = values['prefix']
    allowed = read_source_data(relative(root, values['allowlist'], exists=True))
    if (sorted(allowed.get('files', [])) != names or allowed.get('addon_files') != len(names)
            or allowed.get('zip_entries') != len(names) + 1):
        raise ValueError('addon allowlist counts differ from the closed file manifest')
    generated = values['generated_directories']
    actual = {}
    for path in source.rglob('*'):
        rel = path.relative_to(source)
        if path.is_symlink():
            raise ValueError('addon source contains a symlink')
        if any(rel == Path(name) or Path(name) in rel.parents for name in generated):
            if subprocess.check_output(['git', 'ls-files', '--', path.relative_to(root).as_posix()], cwd=root, text=True).strip():
                raise ValueError('generated addon exclusions may not hide tracked source')
            continue
        if path.is_file():
            actual[rel.as_posix()] = path
        elif not path.is_dir():
            raise ValueError('addon source contains a non-regular file')
    if 'gdam.json' in actual:
        raise ValueError('authored addon manifests must use gdam.toml; gdam.json is generated native I/O')
    if sorted(actual) != names:
        raise ValueError('addon source differs from the closed file manifest')
    version = json.loads(relative(root, values['version_source'], exists=True).read_text()).get('version')
    if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('addon requires a native semantic version')
    plugin = configparser.ConfigParser(interpolation=None)
    plugin.read(source / 'plugin.cfg')
    if plugin.get('plugin', 'version', fallback='') != '"' + version + '"':
        raise ValueError('addon native package and plugin versions differ')
    if values.get('script_uids', False):
        seen = set()
        for name in names:
            if name.endswith('.gd'):
                value = actual.get(name + '.uid')
                uid = value.read_text().strip() if value else ''
                if not re.fullmatch(r'uid://[a-z0-9]+', uid) or uid in seen:
                    raise ValueError('shipped GDScript requires distinct UID sidecars')
                seen.add(uid)
    registry = relative(root, values['registry_archive'])
    if archive == registry or archive.parent != registry.parent or archive.resolve().is_relative_to(source.resolve()):
        raise ValueError('addon archive outputs must be distinct siblings outside source')
    return actual, prefix, version, archive, registry


def artifact_paths(root, config):
    values = package_data(config)
    archive = relative(root, values['archive'])
    paths = [archive, archive.with_suffix(archive.suffix + '.sha256')]
    if values.get('embedded_manifest'):
        registry = relative(root, values['registry_archive'])
        paths += [registry, registry.with_suffix(registry.suffix + '.sha256'), archive.parent / 'PACKAGE_MANIFEST.json']
    return [path.relative_to(root).as_posix() for path in paths]


def write_zip(path, entries):
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)


def build(root, config):
    root = Path(root).resolve()
    files, prefix, version, archive, registry = inputs(root, config)
    records = package_helper().records(files, prefix)
    manifest = {'plugin_version': version, 'closed': True, 'zip_entries': len(records) + 1,
                'files': [{'path': name, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
                          for name, data, _ in records]}
    encoded = (json.dumps(manifest, indent=2, ensure_ascii=False) + '\n').encode()
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='manifest-addon-', dir=archive.parent) as directory:
        candidate = Path(directory) / archive.name
        write_zip(candidate, [*records, *([('PACKAGE_MANIFEST.json', encoded, 0o644)] if registry else [])])
        candidate.replace(archive)
        if registry:
            candidate = Path(directory) / registry.name
            write_zip(candidate, [(name[len(prefix):], data, mode) for name, data, mode in records])
            candidate.replace(registry)
    for path in [archive, *([registry] if registry else [])]:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        path.with_suffix(path.suffix + '.sha256').write_text(digest + '  ' + path.name + '\n')
    if registry:
        (archive.parent / 'PACKAGE_MANIFEST.json').write_bytes(encoded)
    return verify(root, config)


def verify(root, config):
    files, prefix, version, archive, registry = inputs(root, config)
    native = package_helper().records(files)
    expected_records = [{'path': prefix + name, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
                        for name, data, _ in native]
    expected_manifest = {'plugin_version': version, 'closed': True, 'zip_entries': len(files) + 1, 'files': expected_records}
    helper = package_helper()
    for path, prefixed in [(archive, bool(registry)), *([(registry, False)] if registry else [])]:
        helper.verify_checksum(path)
        with zipfile.ZipFile(path) as package:
            expected_names = [prefix + name if prefixed else name for name, _, _ in native]
            if prefixed:
                expected_names.append('PACKAGE_MANIFEST.json')
            if package.namelist() != expected_names:
                raise ValueError('addon archive entries differ from the exact closed manifest')
            for item in package.infolist():
                if not stat.S_ISREG(item.external_attr >> 16):
                    raise ValueError('addon archive entry must be a regular file')
            for name, data, _ in native:
                if package.read(prefix + name if prefixed else name) != data:
                    raise ValueError('addon archive payload differs from its source')
            if prefixed:
                embedded = package.read('PACKAGE_MANIFEST.json')
                if json.loads(embedded) != expected_manifest:
                    raise ValueError('embedded addon provenance differs from its source')
                if embedded != (archive.parent / 'PACKAGE_MANIFEST.json').read_bytes():
                    raise ValueError('external addon provenance differs from embedded bytes')
            if package.testzip() is not None:
                raise ValueError('addon archive integrity failed')
    return hashlib.sha256(archive.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['build', 'verify'])
    args = parser.parse_args()
    root = Path.cwd().resolve()
    try:
        print(globals()[args.operation](root, load(root)))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
