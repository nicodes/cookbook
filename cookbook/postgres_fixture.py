"""Own a disposable native PostgreSQL test fixture, never a retained development DB."""
import contextlib
import json
from pathlib import Path
import re
import secrets
import signal
import subprocess
import tempfile
import threading
import time

from configuration import relative

IDENTIFIER = re.compile(r'[a-z][a-z0-9_]{0,62}\Z')
ENVIRONMENT = re.compile(r'[A-Z][A-Z0-9_]*\Z')
IMAGE = re.compile(r'postgres@sha256:[a-f0-9]{64}\Z')
CONTAINER = re.compile(r'[a-f0-9]{64}\Z')
LABEL = 'tools.postgres-fixture'


@contextlib.contextmanager
def termination_cleanup():
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)
    def terminate(signum, _frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def declaration(root, config):
    value = config.get('components', {}).get('postgres_fixture')
    if value is None:
        return None
    keys = {'image', 'database', 'owner_role', 'runtime_role', 'sql_paths', 'url_environment', 'role_environment', 'password_environment', 'suites', 'startup_timeout'}
    required = {'image', 'database', 'owner_role', 'runtime_role', 'sql_paths', 'url_environment', 'suites'}
    if not isinstance(value, dict) or set(value) - keys or not required <= set(value):
        raise ValueError('invalid native PostgreSQL fixture declaration')
    if not isinstance(value['image'], str) or not IMAGE.fullmatch(value['image']):
        raise ValueError('PostgreSQL fixtures require an exact reviewed public image digest')
    if any(not isinstance(value[name], str) or not IDENTIFIER.fullmatch(value[name]) for name in ['database', 'owner_role', 'runtime_role']):
        raise ValueError('PostgreSQL fixtures require explicit native role and database identifiers')
    if value['owner_role'] == value['runtime_role']:
        raise ValueError('test runtime and database owner identities must remain separate')
    paths = value['sql_paths']
    if not isinstance(paths, list) or not paths or len(paths) > 32:
        raise ValueError('PostgreSQL fixture role policy requires bounded native SQL sources')
    for name in paths:
        path = relative(root, name, exists=True)
        if not path.is_file() or path.suffix != '.sql' or path.stat().st_size > 1 << 20:
            raise ValueError('PostgreSQL fixture SQL must be bounded native source files')
        if not set(Path(name).parts) & {'tests', 'test', 'db'}:
            raise ValueError('PostgreSQL fixture SQL belongs with its native database or tests')
    selected = value['suites']
    if (not isinstance(selected, list) or not selected or any(not isinstance(name, str) for name in selected)
            or len(selected) != len(set(selected)) or not set(selected) <= {'unit', 'integration', 'e2e'}):
        raise ValueError('PostgreSQL fixtures must select actual behavioral test groups')
    urls = value['url_environment']
    if (not isinstance(urls, dict) or not urls or len(urls) > 16
            or any(not ENVIRONMENT.fullmatch(name) or role not in {'owner', 'runtime'} for name, role in urls.items())):
        raise ValueError('PostgreSQL fixture URLs require bounded owner/runtime environment selectors')
    roles = value.get('role_environment', [])
    if not isinstance(roles, list) or any(not isinstance(name, str) or not ENVIRONMENT.fullmatch(name) for name in roles):
        raise ValueError('PostgreSQL runtime role outputs require native environment identifiers')
    passwords = value.get('password_environment', {})
    if (not isinstance(passwords, dict) or len(passwords) > 16
            or any(not ENVIRONMENT.fullmatch(name) or role not in {'owner', 'runtime'} for name, role in passwords.items())):
        raise ValueError('PostgreSQL fixture credentials require native owner/runtime selectors')
    timeout = value.get('startup_timeout', 50)
    if type(timeout) is not int or not 1 <= timeout <= 180:
        raise ValueError('PostgreSQL fixture startup requires an explicit bounded deadline')
    return value


def docker(arguments, environment, *, body=None, timeout=30, execute=subprocess.run, required=True):
    try:
        result = execute(['docker', *arguments], env=environment, input=body, capture_output=True,
                         text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        if required:
            raise ValueError('owned PostgreSQL fixture command failed') from None
        return subprocess.CompletedProcess(['docker'], 1, stdout='', stderr='')
    if result.returncode and required:
        # No command line, captured SQL or environment is included in exceptions.
        raise ValueError('owned PostgreSQL fixture command failed')
    return result


def owned(container, nonce, environment, execute):
    if not CONTAINER.fullmatch(container):
        return False
    result = docker(['inspect', '--format', '{{json .Config.Labels}}', container], environment,
                    execute=execute, required=False)
    if result.returncode:
        return False
    try:
        return json.loads(result.stdout).get(LABEL) == nonce
    except (ValueError, AttributeError):
        return False


@contextlib.contextmanager
def context(root, config, environment, suite, *, execute=subprocess.run, now=time.monotonic, pause=time.sleep):
    root = Path(root).resolve()
    value = declaration(root, config)
    if value is None or suite not in value['suites']:
        yield dict(environment)
        return
    nonce = secrets.token_hex(16)
    owner_password, runtime_password = secrets.token_hex(32), secrets.token_hex(32)
    fixture_env = {**environment, 'POSTGRES_PASSWORD': owner_password, 'PGPASSWORD': owner_password}
    container = ''
    with tempfile.TemporaryDirectory(prefix='postgres-fixture-') as directory, termination_cleanup():
        cid_file = Path(directory) / 'container.id'
        try:
            result = docker(['run', '--detach', '--rm', '--pull=missing', '--cidfile', str(cid_file),
                '--label', LABEL + '=' + nonce, '--publish', '127.0.0.1::5432',
                '--security-opt', 'no-new-privileges:true', '--pids-limit', '256',
                '--tmpfs', '/var/lib/postgresql:rw,nosuid,nodev,size=512m',
                '--env', 'POSTGRES_PASSWORD', '--env', 'POSTGRES_USER=' + value['owner_role'],
                '--env', 'POSTGRES_DB=' + value['database'], value['image']],
                fixture_env, timeout=120, execute=execute)
            container = result.stdout.strip()
            if not CONTAINER.fullmatch(container) or not owned(container, nonce, fixture_env, execute):
                raise ValueError('PostgreSQL fixture did not return its exact owned container identity')
            deadline = now() + value.get('startup_timeout', 50)
            while True:
                ready = docker(['exec', container, 'pg_isready', '-h', '127.0.0.1', '-U', value['owner_role'], '-d', value['database']],
                               fixture_env, timeout=5, execute=execute, required=False)
                if ready.returncode == 0:
                    break
                if now() >= deadline:
                    raise ValueError('owned PostgreSQL fixture readiness deadline exceeded')
                pause(.25)
            result = docker(['port', container, '5432/tcp'], fixture_env, execute=execute)
            binding = re.fullmatch(r'127\.0\.0\.1:([0-9]+)\s*', result.stdout)
            if binding is None or not 1 <= int(binding[1]) <= 65535:
                raise ValueError('PostgreSQL fixture must bind one Docker-assigned loopback port')
            port = binding[1]
            prefix = '\\set runtime_password ' + runtime_password + '\n'
            for name in value['sql_paths']:
                sql = prefix + relative(root, name, exists=True).read_text()
                docker(['exec', '-i', '--env', 'PGPASSWORD', container, 'psql', '-X',
                        '--set', 'ON_ERROR_STOP=1', '--single-transaction', '--no-psqlrc',
                        '--username', value['owner_role'], '--dbname', value['database']],
                       fixture_env, body=sql, timeout=60, execute=execute)
            urls = {'owner': 'postgres://' + value['owner_role'] + ':' + owner_password,
                    'runtime': 'postgres://' + value['runtime_role'] + ':' + runtime_password}
            suffix = '@127.0.0.1:' + port + '/' + value['database'] + '?sslmode=disable'
            urls = {key: url + suffix for key, url in urls.items()}
            selected = {name: urls[role] for name, role in value['url_environment'].items()}
            selected.update({name: value['runtime_role'] for name in value.get('role_environment', [])})
            passwords = {'owner': owner_password, 'runtime': runtime_password}
            selected.update({name: passwords[role] for name, role in value.get('password_environment', {}).items()})
            yield dict(environment) | selected
        finally:
            # A failed `docker run` may create a container before returning an error.
            # Recover only IDs carrying this exact fresh nonce; never stop by port/name.
            candidates = {container} if container else set()
            if cid_file.is_file() and not cid_file.is_symlink():
                candidates.add(cid_file.read_text().strip())
            listing = docker(['ps', '--all', '--quiet', '--no-trunc', '--filter', 'label=' + LABEL + '=' + nonce],
                             fixture_env, execute=execute, required=False)
            if listing.returncode == 0:
                candidates.update(listing.stdout.splitlines())
            for candidate in candidates:
                if owned(candidate, nonce, fixture_env, execute):
                    docker(['rm', '--force', '--volumes', candidate], fixture_env, execute=execute, required=False)
