"""Carry exact configured browser reports from native workers into their host.

The aggregate host layout is <configured reports>/<entry>/<worker-relative
path>. Each worker's bytes and relative tree stay intact; existing artifact and
diagnostic workflows continue to own retention.
"""
import copy
import hashlib
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess

from configuration import relative

MAX_FILES = 20000
MAX_FILE = 2 << 30
MAX_TOTAL = 16 << 30
AUTHORITY = ('source_commit', 'source_tree', 'cookbook_commit', 'configuration_sha256', 'execution_context')


def name(value):
    if (not isinstance(value, str) or not value or '\\' in value or ':' in value
            or any(ord(char) < 32 for char in value)):
        raise ValueError('browser report paths must be owned relative paths')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or '.git' in path.parts or str(path) == '.':
        raise ValueError('browser report paths must stay inside their configured directory')
    return path.as_posix()


def report_root(root, config):
    browser = config.get('components', {}).get('browser')
    if not browser:
        return None
    if not isinstance(browser, dict):
        raise ValueError('browser report evidence requires its configured component')
    path = relative(root, name(browser.get('reports')))
    if path == root:
        raise ValueError('browser reports require one owned generated directory')
    return path.relative_to(root).as_posix()


def required(config, entry):
    """The configured default native driver owns the canonical report producer."""
    import godot_ci
    browser = config.get('components', {}).get('browser')
    if not browser or entry.get('kind') != 'test':
        return False
    suite = entry['suite']
    return any(value.get('runner') == 'godot-browser' and value.get('path') == browser.get('runner')
               and godot_ci.case_id(suite, value) in entry['cases'] for value in config.get('tests', {}).get(suite, []))


def generated(root, name):
    if subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z', '--', name], timeout=30):
        raise ValueError('browser report transport may not contain tracked source')


def inventory(directory):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir() or any(path.is_symlink() for path in directory.parents):
        raise ValueError('browser reports require an unlinked directory')
    files, total = {}, 0
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise ValueError('browser reports may not contain links')
        info = path.stat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 <= info.st_size <= MAX_FILE:
            raise ValueError('browser reports require bounded unlinked regular files')
        name = path.relative_to(directory).as_posix()
        relative(directory, name, exists=True)
        with path.open('rb') as stream:
            files[name] = {'sha256': hashlib.file_digest(stream, 'sha256').hexdigest(), 'size': info.st_size}
        total += info.st_size
        if len(files) > MAX_FILES or total > MAX_TOTAL:
            raise ValueError('browser reports exceed their existing packet file or byte bounds')
    return files


def validate(config, proof):
    entry = proof['entry']
    browser = config.get('components', {}).get('browser')
    record = proof.get('browser_evidence')
    if record is None:
        if required(config, entry):
            raise ValueError('configured browser report producer omitted its evidence')
        return None
    if not browser or entry['kind'] == 'build':
        raise ValueError('browser evidence belongs only to a configured native worker')
    expected_root = name(browser.get('reports'))
    if (not isinstance(record, dict) or set(record) != {'version', 'root', 'entry', 'files', *AUTHORITY}
            or type(record['version']) is not int or record['version'] != 1 or record['root'] != expected_root or record['root'] == '.'
            or record['entry'] != entry['id'] or any(record[key] != proof[key] for key in AUTHORITY)
            or not isinstance(record['entry'], str) or not re.fullmatch('[a-z][a-z0-9-]{0,127}', record['entry'])
            or not isinstance(record['files'], dict) or not 1 <= len(record['files']) <= MAX_FILES):
        raise ValueError('browser evidence differs from its exact source, run, worker or configured root')
    total = 0
    for filename, value in record['files'].items():
        if name(filename) != filename:
            raise ValueError('browser report paths must be canonical owned relative files')
        if (not isinstance(value, dict) or set(value) != {'sha256', 'size'}
                or not isinstance(value['sha256'], str) or not re.fullmatch('[a-f0-9]{64}', value['sha256'])
                or type(value['size']) is not int or not 0 <= value['size'] <= MAX_FILE):
            raise ValueError('browser report evidence requires exact hashes and bounded byte sizes')
        total += value['size']
    if not 0 < total <= MAX_TOTAL:
        raise ValueError('browser report evidence cannot be empty or exceed its existing packet bound')
    if required(config, entry) and not any(filename.endswith('.json') and value['size'] > 0
                                          for filename, value in record['files'].items()):
        raise ValueError('configured browser report producer requires nonempty JSON evidence')
    return record


def capture(root, config, proof):
    name = report_root(root, config)
    if name is None or proof['entry']['kind'] == 'build':
        return
    generated(root, name)
    path = relative(root, name)
    files = inventory(path) if path.exists() else {}
    if not files or not any(row['size'] for row in files.values()):
        if required(config, proof['entry']):
            raise ValueError('configured browser report producer did not write nonempty reports')
        return
    if required(config, proof['entry']) and not any(filename.endswith('.json') and row['size'] > 0
                                                   for filename, row in files.items()):
        raise ValueError('configured browser report producer requires nonempty JSON evidence')
    proof['browser_evidence'] = {'version': 1, 'root': name, 'entry': proof['entry']['id'], 'files': files,
                                 **{key: copy.deepcopy(proof[key]) for key in AUTHORITY}}
    validate(config, proof)


def stage(root, destination, config, proof):
    record = validate(config, proof)
    if record is None:
        return
    source = relative(root, record['root'], exists=True)
    generated(root, record['root'])
    if inventory(source) != record['files']:
        raise ValueError('browser report bytes changed before worker packet export')
    target = Path(destination) / 'browser-evidence'
    for name in record['files']:
        output = relative(target, name)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(relative(source, name, exists=True), output)
    if inventory(target) != record['files']:
        raise ValueError('browser report bytes changed during worker packet export')


def packet(config, proof, directory):
    record = validate(config, proof)
    if record is None:
        return set()
    if inventory(Path(directory) / 'browser-evidence') != record['files']:
        raise ValueError('browser worker packet changed or omitted its closed report bytes')
    return {'browser-evidence/' + name for name in record['files']}


def join_plan(root, config, proofs, directory, artifact_name):
    """Aggregate original relative worker bytes as reports/<entry>/<relative>."""
    root_name = report_root(root, config)
    selected = [proof for proof in proofs if validate(config, proof) is not None]
    if not selected:
        return []
    generated(root, root_name)
    reports = relative(root, root_name)
    if reports.exists() and (not reports.is_dir() or any(reports.iterdir())):
        raise ValueError('browser host report join collides with existing report data')
    operations, destinations = [], set()
    for proof in selected:
        record = proof['browser_evidence']
        incoming = Path(directory) / artifact_name(proof) / 'browser-evidence'
        if inventory(incoming) != record['files']:
            raise ValueError('browser report packet changed before the host join')
        for name in record['files']:
            target = relative(reports, proof['entry']['id'] + '/' + name)
            if target in destinations or target.exists():
                raise ValueError('browser host report namespaces or destination files collide')
            destinations.add(target)
            operations.append((relative(incoming, name, exists=True), target))
    return operations


def join(operations):
    for source, target in operations:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as output, source.open('rb') as incoming:
            shutil.copyfileobj(incoming, output)


def verify_host(root, config, proofs):
    for proof in proofs:
        record = validate(config, proof)
        if record is not None:
            generated(root, record['root'])
            directory = relative(root, record['root'] + '/' + proof['entry']['id'], exists=True)
            if inventory(directory) != record['files']:
                raise ValueError('joined browser reports differ from their exact worker evidence')


def verify_final(host, proofs):
    """Bind final aggregate JSON hashes to the accepted imported namespaces."""
    records = [proof['browser_evidence'] for proof in proofs if proof.get('browser_evidence') is not None]
    if not records:
        return
    actual = host.get('evidence')
    if not isinstance(actual, dict):
        raise ValueError('final host evidence omitted its imported browser reports')
    for record in records:
        prefix = record['root'] + '/' + record['entry'] + '/'
        expected = {prefix + filename: value['sha256'] for filename, value in record['files'].items()
                    if filename.endswith('.json')}
        selected = {filename: value for filename, value in actual.items()
                    if isinstance(filename, str) and filename.startswith(prefix) and filename.endswith('.json')}
        if selected != expected:
            raise ValueError('final host browser evidence differs from its accepted worker reports')
