"""Create closed, deterministic application artifact packets without an engine."""
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import tempfile


MAX_MEMBERS = 65536
MAX_INVENTORY = 8 << 20
MEMBER_TYPES = {'image', 'static', 'metadata', 'release'}
HEX = re.compile(r'[a-f0-9]{64}\Z')


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('artifact metadata contains a duplicate field')
        value[key] = item
    return value


def read_json(path, maximum=MAX_INVENTORY):
    raw = read_regular(path, maximum)
    return json.loads(raw, object_pairs_hook=unique_object,
                      parse_constant=lambda _value: (_ for _ in ()).throw(ValueError('non-finite artifact metadata')))


def identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def regular(path, maximum=None):
    path = Path(path)
    if any(parent.is_symlink() for parent in [path, *path.parents]):
        raise ValueError('artifact paths may not traverse links')
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or maximum is not None and info.st_size > maximum):
        raise ValueError('artifact must be a bounded regular file with one name')
    return info


def read_regular(path, maximum):
    before = regular(path, maximum)
    with open(path, 'rb') as stream:
        if identity(os.fstat(stream.fileno())) != identity(before):
            raise ValueError('artifact changed while opening')
        body = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    if (len(body) > maximum or identity(before) != identity(after)
            or identity(before) != identity(regular(path, maximum))):
        raise ValueError('artifact changed while reading')
    return body


def digest_file(path):
    before = regular(path)
    with open(path, 'rb') as stream:
        if identity(os.fstat(stream.fileno())) != identity(before):
            raise ValueError('artifact changed while opening')
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        after = os.fstat(stream.fileno())
    if identity(before) != identity(after) or identity(before) != identity(regular(path)):
        raise ValueError('artifact changed while hashing')
    return digest, before.st_size


def relative_name(value):
    if (not isinstance(value, str) or not value or len(value.encode()) > 1024
            or '\\' in value or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError('artifact member requires a bounded relative path')
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {'', '.', '..'} for part in value.split('/')):
        raise ValueError('artifact member requires a bounded relative path')
    return value


def static_sources(source, max_bytes, max_file_bytes, max_files):
    source = Path(source)
    if (source.is_symlink() or not source.is_dir()
            or any(parent.is_symlink() for parent in source.parents)):
        raise ValueError('static source must be a regular directory without links')
    if (type(max_bytes) is not int or type(max_file_bytes) is not int or type(max_files) is not int
            or not 0 < max_file_bytes <= max_bytes <= 8 << 30 or not 0 < max_files <= MAX_MEMBERS):
        raise ValueError('static transport requires finite file, tree and count limits')
    files, total = {}, 0
    for path in sorted(source.rglob('*')):
        if path.is_symlink():
            raise ValueError('static source may not contain links')
        if path.is_dir():
            continue
        name = relative_name(path.relative_to(source).as_posix())
        info = regular(path, max_file_bytes)
        if info.st_mode & 0o7000:
            raise ValueError('static source may not carry privileged file modes')
        total += info.st_size
        if total > max_bytes or len(files) >= max_files:
            raise ValueError('static source exceeds its declared transport limits')
        files[name] = info
    if not files:
        raise ValueError('static source must contain files')
    return files


def pack_static(source, destination, *, max_bytes, max_file_bytes, max_files=MAX_MEMBERS):
    """Package the exact source files; archive metadata never carries host ownership."""
    source, destination = Path(source), Path(destination)
    files = static_sources(source, max_bytes, max_file_bytes, max_files)
    if destination.resolve().is_relative_to(source.resolve()):
        raise ValueError('static archive must be outside its input tree')
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(parent.is_symlink() for parent in [destination, *destination.parents]):
        raise ValueError('static archive destination may not traverse links')
    descriptor, temporary = tempfile.mkstemp(prefix='.static-', dir=destination.parent)
    hashes = {}
    try:
        with os.fdopen(descriptor, 'wb') as target:
            with gzip.GzipFile(filename='', fileobj=target, mode='wb', compresslevel=1, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode='w|', format=tarfile.PAX_FORMAT) as archive:
                    for name, before in files.items():
                        path = source / name
                        if identity(regular(path, max_file_bytes)) != identity(before):
                            raise ValueError('static source changed before packaging')
                        body = read_regular(path, max_file_bytes)
                        if len(body) != before.st_size or identity(regular(path)) != identity(before):
                            raise ValueError('static source changed while packaging')
                        entry = tarfile.TarInfo(name)
                        entry.size = len(body)
                        entry.mode = 0o644
                        entry.mtime = 0
                        entry.uid = entry.gid = 0
                        entry.uname = entry.gname = ''
                        archive.addfile(entry, io.BytesIO(body))
                        hashes[name] = hashlib.sha256(body).hexdigest()
            target.flush()
            os.fsync(target.fileno())
        current = static_sources(source, max_bytes, max_file_bytes, max_files)
        if set(current) != set(files) or any(identity(current[name]) != identity(info) for name, info in files.items()):
            raise ValueError('static source membership or bytes changed while packaging')
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    digest, size = digest_file(destination)
    return {'sha256': digest, 'size': size, 'files': hashes,
            'unpacked_bytes': sum(info.st_size for info in files.values())}


def packet_files(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or any(parent.is_symlink() for parent in root.parents):
        raise ValueError('artifact packet must be a directory without links')
    result = set()
    for path in root.rglob('*'):
        if path.is_symlink():
            raise ValueError('artifact packet may not contain links')
        if path.is_dir():
            continue
        regular(path)
        name = relative_name(path.relative_to(root).as_posix())
        if len(result) >= MAX_MEMBERS:
            raise ValueError('artifact packet contains too many files')
        result.add(name)
    return result


def write_inventory(root, members):
    """Bind all packet payloads; the inventory itself is bound by build evidence."""
    root = Path(root)
    if not isinstance(members, dict) or not members or len(members) > MAX_MEMBERS:
        raise ValueError('artifact packet requires an explicit bounded member inventory')
    if 'inventory.json' in members or members.get('release.json') != 'release':
        raise ValueError('artifact packet requires one release descriptor and a separate inventory')
    selected = set()
    files = {}
    for name, kind in sorted(members.items()):
        relative_name(name)
        if kind not in MEMBER_TYPES:
            raise ValueError('artifact packet member type is not supported')
        digest, size = digest_file(root / name)
        selected.add(name)
        files[name] = {'sha256': digest, 'size': size, 'type': kind}
    if packet_files(root) - {'inventory.json'} != selected:
        raise ValueError('artifact packet contains undeclared or missing members')
    body = json.dumps({'version': 1, 'files': files}, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n'
    if len(body) > MAX_INVENTORY:
        raise ValueError('artifact packet inventory exceeds its bound')
    destination = root / 'inventory.json'
    if destination.exists():
        regular(destination, MAX_INVENTORY)
    destination.write_bytes(body)
    return verify_inventory(root)


def verify_inventory(root):
    root = Path(root)
    value = read_json(root / 'inventory.json')
    if (not isinstance(value, dict) or set(value) != {'version', 'files'} or value['version'] != 1
            or not isinstance(value['files'], dict) or not value['files']
            or len(value['files']) > MAX_MEMBERS or value['files'].get('release.json', {}).get('type') != 'release'
            or 'inventory.json' in value['files']):
        raise ValueError('artifact packet inventory has an invalid shape')
    if packet_files(root) != set(value['files']) | {'inventory.json'}:
        raise ValueError('artifact packet contains undeclared or missing members')
    for name, entry in value['files'].items():
        relative_name(name)
        if (not isinstance(entry, dict) or set(entry) != {'sha256', 'size', 'type'}
                or not isinstance(entry['sha256'], str) or not HEX.fullmatch(entry['sha256'])
                or type(entry['size']) is not int or entry['size'] < 0 or entry['type'] not in MEMBER_TYPES):
            raise ValueError('artifact packet member has an invalid shape')
        digest, size = digest_file(root / name)
        if digest != entry['sha256'] or size != entry['size']:
            raise ValueError('artifact packet differs from its accepted member bytes')
    return value


def unpack_static(path, destination, *, max_bytes=512 << 20, max_file_bytes=128 << 20, max_files=MAX_MEMBERS):
    """Extract an accepted static archive into a new owned fixture directory."""
    path, destination = Path(path), Path(destination)
    before = regular(path)
    if destination.exists() or destination.is_symlink() or any(parent.is_symlink() for parent in destination.parents):
        raise ValueError('static fixture destination must be a new directory without links')
    if (type(max_bytes) is not int or type(max_file_bytes) is not int or type(max_files) is not int
            or not 0 < max_file_bytes <= max_bytes <= 8 << 30 or not 0 < max_files <= MAX_MEMBERS):
        raise ValueError('static fixture extraction requires finite limits')
    destination.mkdir(mode=0o700, parents=True)
    total, names = 0, set()
    try:
        with path.open('rb') as incoming:
            if identity(os.fstat(incoming.fileno())) != identity(before):
                raise ValueError('static archive changed before fixture extraction')
            with gzip.GzipFile(fileobj=incoming) as compressed:
                class Bounded:
                    def __init__(self):
                        self.remaining = max_bytes + max_files * 2048 + (1 << 20)
                    def read(self, size=-1):
                        if size < 0:
                            size = self.remaining + 1
                        body = compressed.read(min(size, self.remaining + 1))
                        self.remaining -= len(body)
                        if self.remaining < 0:
                            raise ValueError('static archive metadata exceeds its extraction budget')
                        return body
                with tarfile.open(fileobj=Bounded(), mode='r|') as archive:
                    for member in archive:
                        name = relative_name(member.name)
                        if (not member.isfile() or member.issparse() or name in names or member.size < 0
                                or member.size > max_file_bytes or member.mode & 0o7000 or len(names) >= max_files):
                            raise ValueError('static fixture archive contains unsafe or duplicate members')
                        total += member.size
                        if total > max_bytes:
                            raise ValueError('static fixture archive exceeds its extraction budget')
                        names.add(name)
                        target = destination / name
                        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644)
                        with os.fdopen(descriptor, 'wb') as output:
                            os.fchmod(output.fileno(), 0o644)
                            source = archive.extractfile(member)
                            copied = 0
                            while body := source.read(1 << 20):
                                copied += len(body)
                                output.write(body)
                            if copied != member.size:
                                raise ValueError('static fixture archive contains truncated bytes')
                    # Read through the gzip trailer, including the tar stream's
                    # own read-ahead buffer. A valid prefix cannot hide appended
                    # data or a corrupt/truncated compression checksum.
                    while tail := archive.fileobj.read(1 << 20):
                        if tail.strip(b'\0'):
                            raise ValueError('static fixture archive contains data after its end marker')
            if identity(os.fstat(incoming.fileno())) != identity(before):
                raise ValueError('static archive changed during fixture extraction')
        if identity(regular(path)) != identity(before) or not names:
            raise ValueError('static fixture archive changed or contains no payload')
        return names
    except BaseException:
        import shutil
        shutil.rmtree(destination)
        raise
