"""Compile and immediately preserve each application's declared native variant."""
from pathlib import Path
import re
import shutil

from configuration import relative
import rust_component


def output_names(component):
    options = component | component.get('build', {})
    binaries = options.get('binaries', [])
    names = component.get('output_names', {})
    if (not isinstance(names, dict) or set(names) - set(binaries)
            or any(not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name)
                   for name in [*binaries, *names.values()])):
        raise ValueError('native output names must map declared binaries to single safe filenames')
    return {name: names.get(name, name) for name in binaries}


def artifact_paths(declared):
    result = []
    for component in declared:
        if component.get('build', {}).get('enabled', True) is False:
            continue
        directory = Path(component.get('output_directory', '.artifacts'))
        result += [(directory / name).as_posix() for name in output_names(component).values()]
    if len(set(result)) != len(result):
        raise ValueError('native variants must preserve distinct declared output files')
    return result


def compile_and_stage(root, declared, records, environment, run):
    # Building all variants before copying can silently copy the final feature
    # variant into every fixture when Cargo shares its dependency cache.
    artifact_paths(declared)
    for original, record in zip(declared, records, strict=True):
        if record.get('build', {}).get('enabled', True) is False:
            continue
        rust_component.build(root, [record], environment, run)
        options = record | record.get('build', {})
        target = Path(environment.get('CARGO_TARGET_DIR', str(root / Path(record['manifest']).parent / 'target')))
        if record.get('target_directory'):
            target = relative(root, record['target_directory'])
        if record.get('target'):
            target /= record['target']
        target /= 'release' if options.get('profile', 'release') == 'release' else 'debug'
        destination = relative(root, original.get('output_directory', '.artifacts'))
        destination.mkdir(parents=True, exist_ok=True)
        for name in options.get('binaries', []):
            source = target / name
            if source.is_symlink() or not source.is_file():
                raise ValueError('native compiler output must be a regular declared executable')
            installed = relative(root, (destination / output_names(original)[name]).relative_to(root).as_posix())
            shutil.copy2(source, installed)
