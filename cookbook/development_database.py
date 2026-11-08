"""Own one checkout's retained, loopback-only PostgreSQL development database."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
import tomllib

import artifact_packet
import native_runtime

LABEL = 'cookbook.development.owner'
PURPOSE = 'cookbook.development.resource'
IDENTIFIER = re.compile(r'[a-z][a-z0-9_]{0,62}\Z')
IMAGE = re.compile(r'docker.io/library/postgres@sha256:[a-f0-9]{64}\Z')


class Database:
    def __init__(self, root, project, environment):
        self.root = Path(root).resolve()
        self.environment = dict(environment)
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,95}', project):
            raise ValueError('development database requires a checkout-scoped project identifier')
        self.owner = hashlib.sha256(str(self.root).encode()).hexdigest()
        self.name, self.volume = project + '-postgres', project + '-postgres-data'
        config = tomllib.loads(artifact_packet.read_regular(self.root / 'cookbook.toml', 1 << 20).decode())
        value = config.get('components', {}).get('development_database')
        allowed = {'image', 'database', 'owner_role', 'password', 'port_environment', 'default_port',
                   'data_directory', 'command', 'memory_bytes', 'startup_timeout_seconds'}
        required = {'image', 'database', 'owner_role', 'password', 'port_environment', 'default_port', 'data_directory'}
        if not isinstance(value, dict) or set(value) - allowed or not required <= set(value):
            raise ValueError('development database requires one closed native PostgreSQL declaration')
        if (not IMAGE.fullmatch(value['image']) or not IDENTIFIER.fullmatch(value['database'])
                or not IDENTIFIER.fullmatch(value['owner_role']) or not isinstance(value['password'], str)
                or not 1 <= len(value['password']) <= 128 or '\0' in value['password']
                or not re.fullmatch('[A-Z][A-Z0-9_]*', value['port_environment'])
                or value['data_directory'] not in {'/var/lib/postgresql', '/var/lib/postgresql/data'}):
            raise ValueError('development database has invalid native identity or synthetic credential data')
        raw_port = self.environment.get(value['port_environment'], str(value['default_port']))
        if not re.fullmatch('[0-9]{1,5}', raw_port) or not 1 <= int(raw_port) <= 65535:
            raise ValueError('development PostgreSQL requires a finite loopback port')
        self.port = int(raw_port)
        memory = value.get('memory_bytes', 256 << 20)
        command = value.get('command', [])
        if type(memory) is not int or not 16 << 20 <= memory <= 2 << 30:
            raise ValueError('development memory must remain finite')
        if not isinstance(command, list) or any(not isinstance(item, str) or '\0' in item for item in command):
            raise ValueError('development database command must be a literal native argument list')
        self.value = value
        self.memory = memory
        self.timeout = value.get('startup_timeout_seconds', 60)
        if type(self.timeout) is not int or not 1 <= self.timeout <= 180:
            raise ValueError('development PostgreSQL requires a bounded readiness deadline')

    def run(self, arguments, **kwargs):
        return native_runtime.run(arguments, environment=self.environment, timeout=kwargs.pop('timeout', 30), **kwargs)

    def exists(self, kind, name):
        result = self.run([kind, 'exists', name], check=False, capture_output=True)
        if result.returncode not in {0, 1}:
            raise ValueError('cannot determine exact development resource ownership')
        return result.returncode == 0

    def inspected(self, kind, name):
        value = json.loads(self.run([kind, 'inspect', name], check=True, capture_output=True, text=True).stdout)
        if not isinstance(value, list) or len(value) != 1:
            raise ValueError('development resource requires one exact native identity')
        return value[0]

    def owned(self, kind, name):
        if not self.exists(kind, name):
            return None
        value = self.inspected(kind, name)
        labels = value.get('Config', {}).get('Labels', {}) if kind == 'container' else value.get('Labels', {})
        if not isinstance(labels, dict) or labels.get(LABEL) != self.owner or labels.get(PURPOSE) != 'postgres':
            raise ValueError('development resource belongs to another checkout or purpose')
        return value

    def running(self):
        value = self.owned('container', self.name)
        return value is not None and value.get('State', {}).get('Running') is True

    def status(self):
        value = self.owned('container', self.name)
        print(json.dumps({'name': self.name, 'running': bool(value and value.get('State', {}).get('Running')),
                          'port': self.port, 'volume': self.volume}, sort_keys=True))

    def stop(self):
        if self.owned('container', self.name) is not None:
            self.run(['rm', '--force', self.name], check=True, capture_output=True)

    def reset(self):
        if self.running():
            raise ValueError('stop the development database before resetting its retained data')
        self.stop()
        if self.owned('volume', self.volume) is not None:
            self.run(['volume', 'rm', self.volume], check=True, capture_output=True)

    def execute(self, arguments, **kwargs):
        if self.owned('container', self.name) is None:
            raise ValueError('development database is not running')
        return self.run(['exec', '-i', self.name, *arguments], **kwargs)

    def up(self):
        value = self.owned('container', self.name)
        if value is not None:
            if value.get('ImageName', value.get('Config', {}).get('Image')) != self.value['image']:
                raise ValueError('stop the development database before changing its pinned image')
            bindings = value.get('HostConfig', {}).get('PortBindings', {}).get('5432/tcp')
            if bindings != [{'HostIp': '127.0.0.1', 'HostPort': str(self.port)}]:
                raise ValueError('development database differs from its declared loopback port')
            if not value.get('State', {}).get('Running'):
                self.run(['start', self.name], check=True, capture_output=True)
        else:
            labels = ['--label', LABEL + '=' + self.owner, '--label', PURPOSE + '=postgres']
            if self.owned('volume', self.volume) is None:
                self.run(['volume', 'create', *labels, self.volume], check=True, capture_output=True)
            values = self.environment | {'POSTGRES_PASSWORD': self.value['password']}
            argv = ['run', '--detach', '--name', self.name, *labels, '--pull=missing',
                '--security-opt=no-new-privileges', '--pids-limit=256',
                '--publish', '127.0.0.1:' + str(self.port) + ':5432',
                '--env', 'POSTGRES_PASSWORD', '--env', 'POSTGRES_USER=' + self.value['owner_role'],
                '--env', 'POSTGRES_DB=' + self.value['database'],
                '--volume', self.volume + ':' + self.value['data_directory']]
            argv += ['--memory', str(self.memory), '--memory-swap', str(self.memory)]
            command = self.value.get('command', [])
            if not isinstance(command, list) or any(not isinstance(value, str) or '\0' in value for value in command):
                raise ValueError('development database command must be a literal native argument list')
            native_runtime.run([*argv, self.value['image'], *command], environment=values,
                               check=True, capture_output=True, timeout=120)
        deadline = time.monotonic() + self.timeout
        while True:
            result = self.execute(['pg_isready', '-h', '127.0.0.1', '-U', self.value['owner_role'],
                                   '-d', self.value['database'], '-q'], check=False, capture_output=True, timeout=5)
            if result.returncode == 0:
                return
            if time.monotonic() >= deadline:
                raise ValueError('development PostgreSQL readiness deadline exceeded')
            time.sleep(.25)

    def foreground(self):
        self.up()
        try:
            argv, environment = native_runtime.command(['attach', '--sig-proxy=false', self.name], self.environment)
            subprocess.run(argv, env=environment, check=True)
        finally:
            self.stop()
