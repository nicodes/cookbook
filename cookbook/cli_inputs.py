"""Stage finite release data without overlaying source or provider checkouts."""
import argparse
import os
from pathlib import Path
import re
import shutil
import stat

from configuration import load
import cli_release
import native_extras
import rust_cli

PROOF = '.artifacts/cookbook-validation.json'
EVIDENCE = '.artifacts/cli-publication-evidence'
MAX_FILE = 512 << 20


def ordinary(path):
    path = Path(path).absolute()
    for parent in [path, *path.parents]:
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError('release data must not traverse links')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('release inputs must be ordinary single-link files')
    return info


def inventory(directory):
    for parent in [directory, *directory.parents]:
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError('release transport requires ordinary directory ancestors')
    result = set()
    for path in directory.rglob('*'):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        ordinary(path)
        result.add(path.relative_to(directory).as_posix())
        if len(result) > 4096:
            raise ValueError('release transport inventory exceeds its bound')
    return result


def native_names(root, config):
    package = rust_cli.package_data(root, config)
    native = rust_cli.helper('native_cli')
    names = [native.archive_name(package, target) for target in package['native_targets']]
    names += [*native_extras.names(config), 'checksums.txt']
    if package.get('release_metadata'):
        names.append('release.json')
    if len(names) != len(set(names)) or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name) for name in names):
        raise ValueError('release staging requires distinct flat archive names')
    return names


def copy(source, destination, maximum):
    before = ordinary(source)
    if not 0 < before.st_size <= maximum:
        raise ValueError('release transport member exceeds its bound')
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size):
            raise ValueError('release transport changed before staging')
        with os.fdopen(descriptor, 'rb', closefd=False) as incoming, destination.open('xb') as output:
            os.fchmod(output.fileno(), 0o644)
            remaining = maximum
            while block := incoming.read(min(65536, remaining + 1)):
                remaining -= len(block)
                if remaining < 0:
                    raise ValueError('release transport grew beyond its bound')
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        after = os.fstat(descriptor)
        if after.st_nlink != 1 or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns):
            raise ValueError('release transport changed while staging')
    finally:
        os.close(descriptor)


def stage(root, role, *, product=False):
    root = Path(root).resolve()
    if role not in {'full', 'archives', 'publication'} or product and role != 'archives':
        raise ValueError('release staging requires one fixed transport role')
    config = load(root)
    policy = cli_release.policy(config).get('acceptance')
    with_evidence = product or role == 'publication' and bool(policy)
    directory = root / '.artifacts/cli-input' / role
    source_files = inventory(directory)
    names = native_names(root, config)
    dist_names = set(names)
    if role == 'publication' and policy:
        dist_names.update(policy['files'])
    if inventory(directory / 'dist') != dist_names:
        raise ValueError('original native distribution differs from its closed release names')
    selected = {PROOF: (directory / PROOF, 4 << 20)}
    selected.update({'dist/' + name: (directory / 'dist' / name, MAX_FILE) for name in names})
    if not set(selected) <= source_files:
        raise ValueError('release transport is missing its original archives or proof')
    if with_evidence:
        if not policy:
            raise ValueError('product handoff requires declared source acceptance')
        evidence = ['receipt.json', *policy['files']]
        incoming = root / '.artifacts/cli-input/product' if product else directory / EVIDENCE
        if inventory(incoming) != set(evidence):
            raise ValueError('product handoff differs from its closed evidence inventory')
        selected.update({EVIDENCE + '/' + name: (incoming / name, 1 << 20) for name in evidence})
        if role == 'publication':
            selected.update({'dist/' + name: (directory / 'dist' / name, 1 << 20) for name in policy['files']})
    if role != 'full' and source_files != {name for name in selected if name in source_files}:
        raise ValueError('accepted release handoff contains unexpected members')
    outputs = [root / 'dist']
    if with_evidence:
        outputs.append(root / EVIDENCE)
    if any(path.exists() or path.is_symlink() for path in [*outputs, root / PROOF]):
        raise ValueError('release staging requires fresh destinations')
    for path in [root / '.artifacts', root / '.artifacts/cli-publication-evidence']:
        for parent in [path, *path.parents]:
            if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
                raise ValueError('release destinations require ordinary directory ancestors')
    for path in outputs:
        path.mkdir(mode=0o700, parents=True)
    try:
        for name, (source, maximum) in selected.items():
            copy(source, root / name, maximum)
    except BaseException:
        for path in outputs:
            shutil.rmtree(path)
        (root / PROOF).unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', choices=['full', 'archives', 'publication'], required=True)
    parser.add_argument('--product', action='store_true')
    args = parser.parse_args()
    stage(Path.cwd(), args.input, product=args.product)


if __name__ == '__main__':
    main()
