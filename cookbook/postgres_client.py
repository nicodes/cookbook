"""Lease PostgreSQL capture clients inside the protected task's own cgroup."""
import contextlib
import importlib.util
import os
from pathlib import Path
import re
import secrets
import selectors
import subprocess
import tempfile
import time


NAME = re.compile(r'[a-z][a-z0-9_]{0,62}\Z')
SNAPSHOT = re.compile(r'[A-Fa-f0-9]+-[A-Fa-f0-9]+-[0-9]+\Z')
WITNESS = re.compile(r'[1-9][0-9]*:[0-9]+:[0-9]+:[0-9,]*:on:repeatable read\Z')
WITNESS_QUERY = ("SELECT pg_catalog.pg_backend_pid()::text || ':' || pg_catalog.pg_current_snapshot()::text"
    " || ':' || pg_catalog.current_setting('transaction_read_only')"
    " || ':' || pg_catalog.current_setting('transaction_isolation')")
MAX_QUERY = 16 << 20
PROC_ROOT = Path('/proc')


def runtime_module():
    source = Path(__file__).with_name('capture_runtime.py')
    spec = importlib.util.spec_from_file_location('_postgres_client_runtime', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def server_process(resources, record, expected_uid):
    raw = runtime_module().run(resources, ['container', 'inspect', record['container_id']])
    actual = resources.inspect(record, raw)
    pid = actual.get('State', {}).get('Pid')
    if type(pid) is not int or pid <= 0:
        raise ValueError('PostgreSQL omitted its actual native process identity')
    root = PROC_ROOT / str(pid)
    status = dict(line.split(':', 1) for line in (root / 'status').read_text().splitlines() if ':' in line)
    if status.get('Uid', '').split() != [str(expected_uid)] * 4:
        raise ValueError('PostgreSQL writer user differs from its approved image')
    rows = (root / 'cgroup').read_text().splitlines()
    if (len(rows) != 1 or not rows[0].startswith('0::/') or not any(
            part in {'libpod-' + record['container_id'], 'libpod-' + record['container_id'] + '.scope'}
            for part in rows[0][3:].split('/'))):
        raise ValueError('PostgreSQL process is outside its exact native container')
    fields = (root / 'stat').read_text().rsplit(')', 1)[1].split()
    if len(fields) < 20 or not fields[19].isdigit():
        raise ValueError('PostgreSQL process has no kernel start identity')
    return pid, fields[19]


class Client:
    def __init__(self, resources, identity, staging, database, role, marker, expires, record, server_uid, server_identity):
        self.resources = resources
        self.container_id = identity
        self.staging = staging
        self.database = database
        self.role = role
        self.application_name = marker
        self.expires = expires
        self.record = record
        self.server_uid = server_uid
        self.server_identity = server_identity

    def verify_server(self):
        if server_process(self.resources, self.record, self.server_uid) != self.server_identity:
            raise ValueError('PostgreSQL restarted during logical capture')

    def remaining(self, maximum=600):
        seconds = min(maximum, self.expires - time.monotonic())
        if seconds <= 0:
            raise TimeoutError('PostgreSQL capture client deadline expired')
        return seconds

    def arguments(self, program):
        if program not in {'psql', 'pg_dump'}:
            raise ValueError('PostgreSQL capture client requires its fixed native program')
        return ['/usr/bin/podman', '--remote=false', 'exec', '--user=65534:65534', self.container_id, program]

    def run(self, arguments, *, body=None, output=None, timeout=600):
        with tempfile.TemporaryFile() as errors, tempfile.TemporaryFile() as captured:
            stdin = {'stdin': subprocess.DEVNULL} if body is None else {'input': body}
            result = subprocess.run(arguments, **stdin, stdout=captured if output is None else output,
                stderr=errors, env=self.resources.environment(), timeout=self.remaining(timeout))
            if result.returncode:
                raise ValueError('bounded PostgreSQL capture client failed')
            if output is not None:
                return None
            captured.seek(0)
            raw = captured.read(MAX_QUERY + 1)
            if len(raw) > MAX_QUERY:
                raise ValueError('PostgreSQL capture client output exceeded its bound')
            return raw

    def version(self):
        return self.run(self.arguments('pg_dump') + ['--version'], timeout=15).decode().strip()

    def session(self):
        return Session(self)

    def dump(self, snapshot_id, output):
        if not isinstance(snapshot_id, str) or not SNAPSHOT.fullmatch(snapshot_id):
            raise ValueError('PostgreSQL dump requires its actual exported snapshot')
        output = Path(output).absolute()
        if (not output.is_relative_to(self.staging)
                or any(path.is_symlink() for path in [output, *output.parents])):
            raise ValueError('PostgreSQL dump escaped private capture staging')
        self.verify_server()
        with output.open('xb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            self.run(self.arguments('pg_dump') + ['--no-password', '--format=custom', '--snapshot=' + snapshot_id,
                '--username=' + self.role, '--dbname=' + self.database], output=stream)
        self.verify_server()


class Session:
    def __init__(self, client):
        self.client = client
        self.errors = tempfile.TemporaryFile()
        arguments = client.arguments('psql')
        arguments.insert(arguments.index(client.container_id), '--interactive')
        try:
            self.process = subprocess.Popen(arguments + ['--no-password', '-XAtq', '-v', 'ON_ERROR_STOP=1',
                '--username=' + client.role, '--dbname=' + client.database], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=self.errors, env=client.resources.environment())
        except BaseException:
            self.errors.close()
            raise
        self.pending = bytearray()
        self.witness = None
        self.snapshot_id = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def run(self, sql):
        if not isinstance(sql, str) or '\0' in sql or len(sql.encode()) > MAX_QUERY:
            raise ValueError('capture SQL must be finite stdin data')
        self.client.remaining()
        try:
            self.process.stdin.write((sql.rstrip(';') + ';\n').encode())
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise ValueError('PostgreSQL snapshot session ended') from None

    def query(self, sql):
        self.run(sql)
        with selectors.DefaultSelector() as selected:
            selected.register(self.process.stdout, selectors.EVENT_READ)
            while b'\n' not in self.pending:
                if not selected.select(self.client.remaining()):
                    raise TimeoutError('PostgreSQL snapshot query exceeded the capture deadline')
                raw = os.read(self.process.stdout.fileno(), 65536)
                if not raw:
                    raise ValueError('PostgreSQL snapshot session ended')
                self.pending.extend(raw)
                if len(self.pending) > MAX_QUERY:
                    raise ValueError('PostgreSQL snapshot row exceeded its bound')
        line, _, rest = self.pending.partition(b'\n')
        self.pending = bytearray(rest)
        return line.decode().strip()

    def begin(self):
        self.run('BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')

    def export_snapshot(self):
        self.witness = self.query(WITNESS_QUERY)
        if not WITNESS.fullmatch(self.witness):
            raise ValueError('capture does not hold its read-only repeatable snapshot')
        self.snapshot_id = self.query('SELECT pg_export_snapshot()')
        if not SNAPSHOT.fullmatch(self.snapshot_id):
            raise ValueError('PostgreSQL exported an invalid snapshot identity')
        return self.snapshot_id

    def verify_snapshot(self):
        if self.witness is None or self.query(WITNESS_QUERY) != self.witness:
            raise ValueError('PostgreSQL capture lost its original transaction fence')

    def commit(self):
        self.verify_snapshot()
        # The dump connection must have released its server-side transaction
        # before capture metadata can certify completion of this snapshot.
        deadline = time.monotonic() + min(5, self.client.remaining())
        sql = ("SELECT count(*) FROM pg_catalog.pg_stat_activity WHERE application_name='"
            + self.client.application_name + "' AND usename='" + self.client.role
            + "' AND pid<>pg_catalog.pg_backend_pid()")
        while self.query(sql) != '0':
            if time.monotonic() >= deadline:
                raise ValueError('PostgreSQL dump backend did not terminate')
            time.sleep(.025)
        self.run('COMMIT')
        if self.query('SELECT 1') != '1':
            raise ValueError('PostgreSQL snapshot commit was not acknowledged')
        self.client.verify_server()

    def close(self):
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process.stdout.close()
        self.errors.close()


@contextlib.contextmanager
def context(resources, record, staging, *, database, role, socket_dir='@/komizo-capture',
        timeout=600, control_cgroup=None, server_uid=999):
    if (not isinstance(database, str) or not NAME.fullmatch(database) or not isinstance(role, str)
            or not NAME.fullmatch(role) or type(timeout) is not int or not 1 <= timeout <= 900
            or not isinstance(socket_dir, str) or not re.fullmatch(r'@/[a-z][a-z0-9-]{0,63}', socket_dir)
            or type(server_uid) is not int or not 1 <= server_uid <= 65534):
        raise ValueError('PostgreSQL capture requires explicit bounded client scope')
    resources.validate(record, record['application'], 'postgres')
    service = record['deployment']['release']['workload']['services']['postgres']
    sockets = [item.partition('=')[2].split(',') for item in service.get('command', [])
        if item.startswith('unix_socket_directories=')]
    if len(sockets) != 1 or socket_dir not in sockets[0]:
        raise ValueError('capture socket is not declared by the installed PostgreSQL workload')
    runtime = runtime_module()
    staging = runtime.private_directory(Path(staging).absolute())
    server_process_identity = server_process(resources, record, server_uid)
    seconds = float(timeout)
    if os.environ.get('KOMIZO_DEADLINE_UNIX'):
        seconds = min(seconds, int(os.environ['KOMIZO_DEADLINE_UNIX']) - time.time())
    if seconds <= 0:
        raise TimeoutError('protected PostgreSQL capture deadline expired')
    expires = time.monotonic() + seconds
    marker = 'kmz-backup-' + (os.environ.get('KOMIZO_OPERATION_ID') or secrets.token_hex(16))
    millis = max(1, int(seconds * 1000))
    # pg_dump resets the four ordinary timeout GUCs in its pinned startup code.
    # Physical client containment and the surviving connection check are the
    # dump boundary; the ordinary GUCs additionally bound the held psql session.
    environment = {'PGHOST': socket_dir, 'PGPORT': '5432', 'PGDATABASE': database, 'PGUSER': role,
        'PGAPPNAME': marker, 'PGCONNECT_TIMEOUT': str(min(15, max(1, int(seconds)))),
        'PGPASSFILE': '/dev/null', 'PGSERVICEFILE': '/dev/null', 'PGSSLMODE': 'disable',
        'PGOPTIONS': '-c client_connection_check_interval=100ms -c statement_timeout=' + str(millis)
            + ' -c transaction_timeout=' + str(millis) + ' -c idle_in_transaction_session_timeout=' + str(millis)}
    with runtime.container(resources, record, staging, artifact=service['artifact'], user='65534:65534',
            mounts={}, entrypoint='/bin/sleep', command=[str(max(1, int(seconds)))], environment=environment,
            network='postgres', control_cgroup=control_cgroup, timeout=max(1, int(seconds))) as identity:
        yield Client(resources, identity, staging, database, role, marker, expires, record, server_uid,
            server_process_identity)
