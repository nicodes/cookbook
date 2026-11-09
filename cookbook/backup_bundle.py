"""Build and execute finite protected PostgreSQL backup bundles."""
import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import zipfile

MAX_BUNDLE = 32 << 20
MAX_FILE = 4 << 20
HELPERS = ('installed_resource.py', 'postgres-recovery.py', 'snapshot.py')
PROVIDER_HELPERS = ('capture_runtime.py',)
PROVIDERS = {'cookbook_revision', 'tools_revision', 'core_revision'}
PREFIX = b'#!/bin/sh\nexec /usr/bin/python3 -I -S "$0" "$@"\n'
SHA = re.compile(r'[a-f0-9]{64}\Z')
COMMIT = re.compile(r'[a-f0-9]{40}\Z')
NAME = re.compile(r'[a-z][a-z0-9-]{0,62}\Z')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate backup metadata field')
        value[key] = item
    return value


def decode(raw):
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid backup number')))


def regular(path, maximum):
    path = Path(path).absolute()
    if any(part.is_symlink() for part in [path, *path.parents]):
        raise ValueError('backup inputs cannot cross links')
    with path.open('rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
            raise ValueError('backup input must be a bounded unlinked regular file')
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        if len(raw) != info.st_size or any(getattr(info, key) != getattr(after, key) for key in
                ['st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns']):
            raise ValueError('backup input changed while reading')
        return raw


def path_name(value):
    return (isinstance(value, str) and bool(value) and len(value) < 256 and '\\' not in value
            and all(ord(char) >= 32 for char in value) and not PurePosixPath(value).is_absolute()
            and '..' not in PurePosixPath(value).parts and str(PurePosixPath(value)) == value)


def manifest_valid(record):
    if (not isinstance(record, dict) or set(record) != {'kind', 'version', 'application', 'source', 'providers', 'services', 'entrypoint', 'files'}
            or record['kind'] != 'komizo.backup-bundle' or type(record['version']) is not int or record['version'] != 1
            or not isinstance(record['application'], str) or not NAME.fullmatch(record['application'])
            or not isinstance(record['source'], dict) or set(record['source']) != {'repository', 'revision'}
            or not isinstance(record['source']['repository'], str)
            or not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', record['source']['repository'])
            or not isinstance(record['source']['revision'], str) or not COMMIT.fullmatch(record['source']['revision'])
            or not isinstance(record['providers'], dict) or set(record['providers']) != PROVIDERS
            or any(not isinstance(value, str) or not COMMIT.fullmatch(value) for value in record['providers'].values())
            or record['services'] != ['postgres']
            or not isinstance(record['files'], dict) or not 5 <= len(record['files']) <= 64
            or any(not path_name(name) or not isinstance(value, dict) or set(value) != {'sha256', 'size'}
                   or not isinstance(value['sha256'], str) or not SHA.fullmatch(value['sha256'])
                   or type(value['size']) is not int or not 0 < value['size'] <= MAX_FILE
                   for name, value in record['files'].items())
            or not isinstance(record['entrypoint'], str) or record['entrypoint'] not in record['files'] or not record['entrypoint'].startswith('product/')
            or not {'__main__.py', 'recipient.pem', *('helpers/' + name for name in [*HELPERS,*PROVIDER_HELPERS])} <= set(record['files'])):
        raise ValueError('backup bundle lacks a closed source/provider/file inventory')
    return record


def write_bundle(files, manifest, output):
    """Create the exact executable only after every declared byte is available."""
    manifest = dict(manifest, files={name: {'sha256': hashlib.sha256(body).hexdigest(), 'size': len(body)}
                                    for name, body in files.items()})
    manifest_valid(manifest)
    if set(files) != set(manifest['files']) or 'bundle.json' in files:
        raise ValueError('backup bundle inventory is ambiguous')
    buffer = io.BytesIO(PREFIX)
    buffer.seek(0, io.SEEK_END)
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, body in sorted({**files, 'bundle.json': canonical(manifest)}.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o400) << 16
            archive.writestr(info, body)
    raw = buffer.getvalue()
    if len(raw) > MAX_BUNDLE:
        raise ValueError('backup bundle exceeds its whole-program bound')
    output = Path(output).absolute()
    if output.exists() or output.is_symlink() or any(parent.is_symlink() for parent in output.parents):
        raise ValueError('backup bundle output must be new without linked ancestors')
    with output.open('xb') as stream:
        os.fchmod(stream.fileno(), 0o755)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return {'program': str(output), 'sha256': 'sha256:' + hashlib.sha256(raw).hexdigest(), 'size_bytes': len(raw)}


def tracked(root, revision, name):
    """Read a bounded immutable Git blob, never a concurrent checkout mutation."""
    import sdk_source
    if not COMMIT.fullmatch(revision) or not path_name(name):
        raise ValueError('backup source requires one exact revision and path')
    entry = sdk_source.git(root, 'ls-tree', '-z', revision, '--', name)
    fields = entry.removesuffix('\0').split('\t')
    if len(fields) != 2 or fields[1] != name:
        raise ValueError('backup source is not tracked at its declared revision')
    mode, kind, identity = fields[0].split(' ')
    if mode not in {'100644', '100755'} or kind != 'blob' or not COMMIT.fullmatch(identity):
        raise ValueError('backup source must be an immutable regular Git blob')
    size = int(sdk_source.git(root, 'cat-file', '-s', identity))
    if not 0 < size <= MAX_FILE:
        raise ValueError('backup source blob exceeds its bound')
    raw = subprocess.check_output(['git', '--no-replace-objects', '-c', 'core.hooksPath=' + os.devnull,
        '-c', 'core.fsmonitor=false', '-C', str(root), 'cat-file', 'blob', identity], timeout=30)
    actual = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw, usedforsecurity=False).hexdigest()
    if len(raw) != size or actual != identity:
        raise ValueError('backup source bytes differ from their immutable Git identity')
    return raw


def build(root, application, capture, recipient, output, *, files=()):
    """Use tracked source and the provider's locked Tools closure; no ambient SDK."""
    import dependencies
    import deployment
    import sdk_source
    root = Path(root).resolve(strict=True)
    if not isinstance(application, str) or not NAME.fullmatch(application):
        raise ValueError('backup bundle requires its application identity')
    revision = sdk_source.git(root, 'rev-parse', 'HEAD')
    repository = sdk_source.canonical_origin(sdk_source.git(root, 'remote', 'get-url', 'origin'))
    sdk_source.git(root, 'diff', '--quiet', 'HEAD', '--')
    provider = deployment.provider_provenance()
    tools = dependencies.tools_root()
    selected = [str(capture), *map(str, files)]
    if len(set(selected)) != len(selected):
        raise ValueError('product backup files must be distinct')
    provider_root = Path(__file__).resolve().parents[1]
    contents = {'__main__.py': tracked(provider_root, provider['cookbook_revision'], 'cookbook/backup_bundle.py'),
                **{'helpers/' + name: tracked(tools, provider['tools_revision'], 'helpers/' + name) for name in HELPERS},
                **{'helpers/' + name: tracked(provider_root, provider['cookbook_revision'], 'cookbook/' + name) for name in PROVIDER_HELPERS}}
    for name in selected:
        if not path_name(name) or not name.endswith('.py'):
            raise ValueError('product capture selects explicit tracked Python files')
        contents['product/' + name] = tracked(root, revision, name)
    certificate = regular(recipient, 65536)
    if not re.fullmatch(rb'-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----\r?\n?', certificate):
        raise ValueError('backup recipient must contain one public certificate only')
    subprocess.run(['/usr/bin/openssl', 'x509', '-noout'], input=certificate, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
    contents['recipient.pem'] = certificate
    manifest = {'kind': 'komizo.backup-bundle', 'version': 1, 'application': application,
                'source': {'repository': repository, 'revision': revision}, 'providers': provider,
                'services': ['postgres'], 'entrypoint': 'product/' + str(capture)}
    if sdk_source.git(root, 'rev-parse', 'HEAD') != revision:
        raise ValueError('backup source revision changed during bundle creation')
    sdk_source.git(root, 'diff', '--quiet', revision, '--')
    return write_bundle(contents, manifest, output)


def read_bundle(program):
    raw = regular(program, MAX_BUNDLE)
    if not raw.startswith(PREFIX):
        raise ValueError('backup bundle has an unsupported interpreter contract')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        if (not 6 <= len(members) <= 65 or len({member.filename for member in members}) != len(members)
                or any(not path_name(member.filename) or member.compress_type != zipfile.ZIP_STORED
                       or member.flag_bits & 1 or not 0 < member.file_size <= MAX_FILE
                       or not stat.S_ISREG(member.external_attr >> 16) for member in members)):
            raise ValueError('backup bundle has unsafe or repeated members')
        manifest = manifest_valid(decode(archive.read('bundle.json')))
        if archive.read('bundle.json') != canonical(manifest) or set(archive.namelist()) != set(manifest['files']) | {'bundle.json'}:
            raise ValueError('backup bundle does not match its exact inventory')
        contents = {}
        for name, expected in manifest['files'].items():
            raw = archive.read(name)
            if len(raw) != expected['size'] or hashlib.sha256(raw).hexdigest() != expected['sha256']:
                raise ValueError('backup bundle bytes differ from their inventory')
            contents[name] = raw
        return manifest, contents


def request(raw, manifest, environment, *, now=None):
    if len(raw) > 16384:
        raise ValueError('backup request exceeds its bound')
    value = decode(raw)
    fields = {'kind', 'version', 'application', 'instance', 'release_digest', 'operation_id', 'output_directory', 'deadline_unix'}
    now = time.time() if now is None else now
    if (not isinstance(value, dict) or set(value) != fields or raw != canonical(value)
            or value['kind'] != 'komizo.backup-request' or type(value['version']) is not int or value['version'] != 1
            or value['application'] != manifest['application'] or value['instance'] != 'production'
            or not isinstance(value['release_digest'], str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', value['release_digest'])
            or not isinstance(value['operation_id'], str) or not re.fullmatch('[a-f0-9]{32}', value['operation_id'])
            or type(value['deadline_unix']) is not int or not now < value['deadline_unix'] <= now + 1500
            or not isinstance(value['output_directory'], str) or not Path(value['output_directory']).is_absolute()
            or value['output_directory'] != str(Path(value['output_directory']).resolve())):
        raise ValueError('backup request is not a current exact production operation')
    for field in fields - {'kind', 'version'}:
        if environment.get('KOMIZO_' + field.upper()) != str(value[field]):
            raise ValueError('backup request differs from the protected process context')
    return value


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


JOURNAL_ROOT = Path('/var/lib/komizo/journal')
EXPORTS_ROOT = Path('/var/lib/komizo/exports')


def reconcile(program,raw,environment):
    """Clean only proven owned capture effects after Core quiesces the task."""
    manifest,contents=read_bundle(program)
    selected=decode(raw)
    fields={'kind','version','application','instance','operation_id','deadline_unix'}
    if (not isinstance(selected,dict) or set(selected)!=fields or raw!=canonical(selected)
            or selected.get('kind')!='komizo.capture-reconcile' or type(selected.get('version')) is not int
            or selected['version']!=1 or selected.get('application')!=manifest['application']
            or selected.get('instance')!='production' or not re.fullmatch('[a-f0-9]{32}',selected.get('operation_id','')) or type(selected.get('deadline_unix')) is not int
            or not time.time()<selected['deadline_unix']<=time.time()+900 or os.geteuid()!=0):
        raise ValueError('capture reconciliation requires exact root-owned operation scope')
    for key in ['application','instance','operation_id','deadline_unix']:
        if environment.get('KOMIZO_'+key.upper())!=str(selected[key]):
            raise ValueError('capture reconciliation differs from its protected task context')
    directory=JOURNAL_ROOT/selected['operation_id']/'capture-helpers'
    observations=[]
    with tempfile.TemporaryDirectory(prefix='capture-reconcile-') as working:
        working=Path(working)
        for name in ['installed_resource.py','capture_runtime.py']:
            path=working/name;path.write_bytes(contents['helpers/'+name]);path.chmod(0o400)
        resources=module(working/'installed_resource.py','cleanup_installed_resource')
        runtime=module(working/'capture_runtime.py','cleanup_capture_runtime')
        if directory.exists():
            paths=list(directory.iterdir())
            if len(paths)>64:raise ValueError('capture reconciliation exceeds its finite helper inventory')
            for path in paths:
                if time.time()>=selected['deadline_unix']:raise ValueError('capture reconciliation deadline expired')
                lease=runtime.validate_lease(decode(regular(path,1<<20)))
                if lease['application']!=selected['application'] or lease['operation_id']!=selected['operation_id']:
                    raise ValueError('capture reconciliation found foreign operation evidence')
            observations=runtime.cleanup_leases(resources,directory)
    output=EXPORTS_ROOT/selected['operation_id'];marker=output/'.capture-staging.json';removed=False
    if marker.exists():
        info=marker.lstat()
        if info.st_uid!=0 or info.st_mode&0o077:raise ValueError('capture staging marker is not root-private')
        record=decode(regular(marker,16384))
        expected={'kind','version','application','operation_id','path','device','inode'}
        if (not isinstance(record,dict) or set(record)!=expected or record.get('kind')!='komizo.capture-staging'
                or type(record.get('version')) is not int or record['version']!=1
                or type(record.get('device')) is not int or type(record.get('inode')) is not int
                or record.get('application')!=selected['application']
                or record.get('operation_id')!=selected['operation_id']):
            raise ValueError('capture plaintext staging has no exact operation authority')
        stage=Path(record['path'])
        if stage.parent!=output or not re.fullmatch(r'\.export-[a-zA-Z0-9_-]+',stage.name):
            raise ValueError('capture plaintext staging escaped its fixed operation directory')
        if stage.exists():
            info=stage.lstat()
            if (stage.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o077
                    or info.st_dev!=record['device'] or info.st_ino!=record['inode']):
                raise ValueError('capture plaintext staging ownership changed')
            shutil.rmtree(stage);removed=True
        marker.unlink()
    return {'kind':'komizo.capture-reconciled','version':1,'application':selected['application'],
        'instance':selected['instance'],'operation_id':selected['operation_id'],'helpers':len(observations),
        'helpers_absent':True,'orphan_plaintext_removed':removed}


def execute(program, raw, environment):
    manifest, contents = read_bundle(program)
    selected = request(raw, manifest, environment)
    if os.geteuid() != 0:
        raise ValueError('protected backup requires the root task boundary')
    output = Path(selected['output_directory'])
    if any(path.is_symlink() for path in [output, *output.parents]):
        raise ValueError('backup output cannot cross links')
    info = output.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077
            or output.name != selected['operation_id'] or any(output.iterdir())):
        raise ValueError('backup output must be this operation\'s fresh private directory')
    old_environment = dict(os.environ)
    fixed = {'KOMIZO_' + key.upper(): str(value) for key, value in selected.items() if key not in {'kind', 'version'}}
    fixed.update(PATH='/usr/local/bin:/usr/bin:/bin', HOME='/root', LANG='C.UTF-8')
    os.environ.clear(); os.environ.update(fixed)
    def interrupt(_number, _frame):
        raise InterruptedError('protected backup interrupted')
    handlers = {number: signal.signal(number, interrupt) for number in [signal.SIGINT, signal.SIGTERM]}
    installed_files = []
    try:
        with tempfile.TemporaryDirectory(prefix='.export-', dir=output) as temporary:
            stage = Path(temporary)
            info=stage.stat();marker=output/'.capture-staging.json'
            with marker.open('xb') as proof:
                os.fchmod(proof.fileno(),0o600)
                proof.write(canonical({'kind':'komizo.capture-staging','version':1,'application':selected['application'],
                    'operation_id':selected['operation_id'],'path':str(stage),'device':info.st_dev,'inode':info.st_ino}))
                proof.flush();os.fsync(proof.fileno())
            code = stage / 'bundle'; code.mkdir(mode=0o700)
            for name, body in contents.items():
                destination = code / name; destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with destination.open('xb') as stream:
                    os.fchmod(stream.fileno(), 0o400); stream.write(body)
            resources = module(code / 'helpers/installed_resource.py', 'bundle_installed_resource')
            records = [resources.query(selected['application'], 'postgres')]
            _revision, _images, deployment = resources.metadata(records)
            if (deployment['release_digest'] != selected['release_digest']
                    or deployment['release']['source']['repository'].lower() != manifest['source']['repository'].lower()):
                raise ValueError('backup request does not select the protected installed release')
            capture = module(code / manifest['entrypoint'], 'bundle_product_capture')
            if capture.PROJECT != selected['application'] or not callable(capture.capture):
                raise ValueError('bundled capture belongs to another application')
            capture.RECOVERY_ROOT = code / 'helpers'
            capture._installed = None
            target = Path(capture.capture(stage))
            metadata = decode(regular(stage / 'images.json', MAX_FILE))
            if target != stage / 'capture' or metadata.get('deployment') != deployment:
                raise ValueError('capture does not bind the protected installed release')
            resources.unchanged(records)
            postgres = module(code / 'helpers/postgres-recovery.py', 'bundle_postgres_recovery')
            snapshot = module(code / 'helpers/snapshot.py', 'bundle_snapshot')
            package = postgres.package_capture(target, stage / 'package', metadata)
            sealed = snapshot.seal(package, code / 'recipient.pem')
            resources.unchanged(records)
            if time.time() >= selected['deadline_unix']:
                raise ValueError('protected backup deadline expired')
            for name, maximum in [('snapshot.cms', 17 << 30), ('receipt.json', 1 << 20)]:
                source = sealed / name
                # Generic sealing already bounded and checksummed this regular
                # output. Stream it without placing plaintext in the export set.
                info = source.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= maximum:
                    raise ValueError('sealed output is not a bounded regular export')
                destination = output / name
                with source.open('rb') as incoming, destination.open('xb') as outgoing:
                    os.fchmod(outgoing.fileno(), 0o600)
                    installed_files.append(destination)
                    shutil.copyfileobj(incoming, outgoing, 1 << 20)
                    outgoing.flush(); os.fsync(outgoing.fileno())
        (output/'.capture-staging.json').unlink(missing_ok=True)
        return selected
    except BaseException:
        for path in installed_files:
            path.unlink(missing_ok=True)
        marker=output/'.capture-staging.json'
        if marker.exists():
            proof=decode(regular(marker,16384))
            if not Path(proof['path']).exists():marker.unlink()
        raise
    finally:
        for number, handler in handlers.items(): signal.signal(number, handler)
        os.environ.clear(); os.environ.update(old_environment)


def main():
    if zipfile.is_zipfile(sys.argv[0]):
        if len(sys.argv) != 1:
            raise ValueError('protected backup bundle accepts no arguments')
        raw=sys.stdin.buffer.read(16385)
        if decode(raw).get('kind')=='komizo.capture-reconcile':
            sys.stdout.buffer.write(canonical(reconcile(Path(sys.argv[0]),raw,dict(os.environ))))
        else:execute(Path(sys.argv[0]),raw,dict(os.environ))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--application', required=True)
    parser.add_argument('--capture', required=True)
    parser.add_argument('--file', action='append', default=[])
    parser.add_argument('--recipient', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.root, args.application, args.capture, args.recipient, args.output,
                           files=args.file), sort_keys=True))


if __name__ == '__main__':
    main()
