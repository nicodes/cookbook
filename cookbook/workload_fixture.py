"""Serve exact accepted static artifacts with the native workload renderer."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import urllib.error
import urllib.request

import artifact_packet
import asset_fixtures
from configuration import relative
import dependencies
import deployment
import komizo_core

SDK = '.artifacts/sdk/workload-caddy'
CADDY_ASSET_RELEASE = 'https://github.com/nicodes/komizo/releases/download/v0.6.6/'
CADDY_ASSET_GO = '1.27.4'
# These checksums bind the attested release binaries to their pinned build inputs.
CADDY_ASSET_SOURCE = {
    'caddy-build.sh': '6c30de5fa4cdb860b39908451cac55c72f42d615e48b035246e095f59bc02d60',
    'caddy.go.mod': 'a9056233663f7ccd6ded5f1334393061cfa06bd088f67b0050758c3a2555ba3a',
    'caddy.go.sum': '05275136107a9863d5b1fb2501e19ee43c2e181a5d50eff470f04e58113a631c',
}
CADDY_ASSET_DIGESTS = {
    'x86_64': '1c389b8b94d15a84ae9808e01deb97c333af111accea3d1bc488877f9b42dc81',
    'arm64': '72883f5e11ded86c2888a3bbff6a60e4453471d124b8b26a808b0bc7a9997701',
}
CADDY_ASSET_LIMIT = 96 << 20


def go_version():
    return __import__('tomllib').loads((Path(__file__).resolve().parents[1]/'mise.toml').read_text())['tools']['go']


def compiler_environment(environment):
    blocked={'GOROOT','GOENV','GOTOOLDIR','GOFLAGS','GOTOOLCHAIN','CGO_ENABLED','GOCACHEPROG',
        'GOOS','GOARCH','GOAMD64','GOARM','GOMIPS','GOMIPS64','GORISCV64','GOWORK','GOEXPERIMENT',
        'GODEBUG','GO111MODULE','LD_PRELOAD','LD_LIBRARY_PATH','GOMODCACHE','GOCACHE','GOPATH',
        'GOPROXY','GOSUMDB','GOPRIVATE','GONOSUMDB','GONOPROXY','GOINSECURE'}
    values={key:value for key,value in environment.items() if key not in blocked}
    return values | {'CGO_ENABLED':'0','GOENV':'off','GOTOOLCHAIN':'local','GOFLAGS':'-buildvcs=false',
        'MISE_AUTO_INSTALL':'false','MISE_EXEC_AUTO_INSTALL':'false','MISE_TASK_RUN_AUTO_INSTALL':'false',
        'MISE_NO_CONFIG':'1','MISE_NO_ENV':'1','MISE_NO_HOOKS':'1','GOWORK':'off'}


def caddy_source():
    tools = dependencies.tools_root()
    return {name: artifact_packet.digest_file(tools / 'helpers' / name)[0]
            for name in CADDY_ASSET_SOURCE}


def caddy_expected(root, environment):
    provider = dependencies.tools_identity()
    files = caddy_source()
    required = go_version()
    actual = subprocess.check_output(['mise','exec','go@'+required,'--','go','env','GOVERSION'],
        cwd=root,env=compiler_environment(environment),text=True,timeout=30).strip()
    if actual != 'go' + required:
        raise ValueError('workload HTTP fixtures require the provider-reviewed Go SDK')
    return {'version': 1, 'tools': provider, 'source_sha256': files, 'go': required}


def published_caddy(environment):
    if environment.get('GITHUB_ACTIONS') != 'true' or sys.platform != 'linux':
        return None
    architecture = {'aarch64': 'arm64', 'arm64': 'arm64', 'x86_64': 'x86_64'}.get(platform.machine())
    if architecture and go_version() == CADDY_ASSET_GO and caddy_source() == CADDY_ASSET_SOURCE:
        return CADDY_ASSET_RELEASE + 'caddy_Linux_' + architecture, CADDY_ASSET_DIGESTS[architecture]
    return None


def download_caddy(directory, url, digest):
    with tempfile.TemporaryDirectory(dir=directory) as temporary:
        binary = Path(temporary) / 'caddy'
        with urllib.request.urlopen(url, timeout=60) as response, binary.open('wb') as output:
            remaining = CADDY_ASSET_LIMIT
            while chunk := response.read(min(1 << 20, remaining + 1)):
                remaining -= len(chunk)
                if remaining < 0:
                    raise ValueError('published Caddy fixture exceeds its size limit')
                output.write(chunk)
        if artifact_packet.digest_file(binary)[0] != digest:
            raise ValueError('published Caddy fixture checksum mismatch')
        binary.chmod(0o755)
        binary.replace(directory / 'caddy')


def caddy_binary(root, environment):
    override = environment.get('KOMIZO_CADDY')
    if override:
        if environment.get('GITHUB_ACTIONS') == 'true':
            raise ValueError('Actions HTTP fixtures require their immutable prepared Caddy SDK')
        path = Path(override)
        if not path.is_absolute():
            raise ValueError('staged Caddy must be an explicit absolute executable')
        artifact_packet.regular(path)
        return path
    binary = relative(root, SDK + '/caddy', exists=True)
    published = published_caddy(environment)
    if published:
        artifact_packet.regular(binary, CADDY_ASSET_LIMIT)
        if artifact_packet.digest_file(binary)[0] != published[1]:
            raise ValueError('published Caddy fixture checksum mismatch; run setup')
        return binary
    expected = caddy_expected(root, environment)
    receipt = artifact_packet.read_json(relative(root, SDK + '/build.json', exists=True), 32768)
    if receipt != expected | {'sha256': artifact_packet.digest_file(binary)[0]}:
        raise ValueError('native Caddy fixture SDK is missing or changed; run setup')
    return binary


def setup(root, config, environment, run):
    if not config.get('components', {}).get('assets') or sys.platform != 'linux':
        return
    prepare(root, environment, run)


def prepare(root, environment, run):
    with ThreadPoolExecutor(max_workers=1) as worker:
        renderer = worker.submit(komizo_core.prepare, root, dict(environment), run)
        prepare_caddy(root, dict(environment), run)
        renderer.result()


def prepare_caddy(root, environment, run):
    if environment.get('KOMIZO_CADDY'):
        caddy_binary(root, environment)
        return
    published = published_caddy(environment)
    if published:
        try:
            caddy_binary(root, environment)
            return
        except (ValueError, FileNotFoundError):
            directory = relative(root, SDK)
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            download_caddy(directory, *published)
        caddy_binary(root, environment)
        return
    komizo_core.install_tool('go',go_version(),compiler_environment(environment),run)
    expected = caddy_expected(root, environment)
    try:
        caddy_binary(root, environment)
        return
    except (ValueError, FileNotFoundError):
        pass
    directory = relative(root, SDK)
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    source = directory / 'source'
    source.mkdir(mode=0o700, exist_ok=True)
    tools = dependencies.tools_root()
    for original, name in [('caddy.go.mod', 'go.mod'), ('caddy.go.sum', 'go.sum'), ('caddy-build.sh', 'build.sh')]:
        (source / name).write_bytes(artifact_packet.read_regular(tools / 'helpers' / original, 1 << 20))
    values = compiler_environment(environment)
    values |= {'GOMODCACHE':str(source/'modules'),'GOCACHE':str(source/'cache'),'GOPATH':str(source/'go'),
        'GOPROXY':'https://proxy.golang.org','GOSUMDB':'sum.golang.org','GOPRIVATE':'','GONOSUMDB':'','GONOPROXY':'','GOINSECURE':''}
    run(['mise','exec','go@'+go_version(),'--','sh','build.sh'],source,values,timeout=600)
    run(['mise','exec','go@'+go_version(),'--','go','mod','verify'],source,values,timeout=300)
    destination = directory / 'caddy'
    shutil.copyfile(source / 'caddy', destination)
    destination.chmod(0o755)
    (directory / 'build.json').write_text(json.dumps(expected | {'sha256': artifact_packet.digest_file(destination)[0]}, sort_keys=True) + '\n')
    caddy_binary(root, environment)


@contextlib.contextmanager
def context(root, config, environment, *, upstreams=None, hostname=None, variant=None, packet=None, expected_source=None, on_started=None):
    """Expose loopback fixture sockets; the application supplies its own assertions."""
    root = Path(root)
    if on_started is not None and not callable(on_started):
        raise ValueError('workload startup observation requires a callable owned fixture observer')
    if packet is not None and variant is not None:
        raise ValueError('workload fixtures select one exact packet or declared variant')
    packet = Path(packet) if packet is not None else (relative(root, '.artifacts/release', exists=True) if variant is None else asset_fixtures.directory(root, config, variant))
    release = deployment.verify(root, environment, directory=packet, expected_source=expected_source)
    selected = [route for route in release['workload']['routes']
                if route.get('target', {}).get('kind') == 'asset' and (hostname is None or route['host'] == hostname)]
    if not selected:
        raise ValueError('workload browser fixture requires an admitted asset route')
    hostname = selected[0]['host']
    upstreams = ({route['target']['name']: '127.0.0.1:1' for route in release['workload']['routes']
                  if route.get('target', {}).get('kind') == 'service'} if upstreams is None else upstreams)
    if not isinstance(upstreams, dict) or any(not isinstance(key, str) or not isinstance(value, str)
            or not re.fullmatch(r'127\.0\.0\.1:[1-9][0-9]{0,4}', value) or int(value.rsplit(':', 1)[1]) > 65535
            for key, value in upstreams.items()):
        raise ValueError('workload fixture upstreams must be explicit owned loopback sockets')
    binary = komizo_core.binary(root, environment)
    caddy = caddy_binary(root, environment)
    output = relative(root, '.artifacts/e2e')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='workload-http-') as directory:
        directory = Path(directory)
        assets = directory / 'assets'
        assets.mkdir(mode=0o700)
        for name, value in release['workload']['assets'].items():
            record = next(item for item in config['components']['assets'] if item['name'] == value['artifact'])
            artifact_packet.unpack_static(packet / 'static' / (value['artifact'] + '.tar.gz'), assets / name,
                max_bytes=record['max_bytes'], max_file_bytes=record['max_file_bytes'],
                max_files=record.get('max_files', artifact_packet.MAX_MEMBERS))
        incoming = directory / 'upstreams.json'
        incoming.write_bytes(deployment.canonical(upstreams))
        rendered = subprocess.check_output([str(binary), 'release', 'render', str(packet / 'release.json'),
            '--assets-root', str(assets), '--upstreams', str(incoming), '--http'], cwd=root,
            env=environment, text=True, timeout=30)
        if not rendered.startswith('{\n'):
            raise ValueError('native renderer did not emit its fixed HTTP fixture configuration')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        source = directory / 'Caddyfile'
        source.write_text(rendered.replace('{\n', '{\n\thttp_port ' + str(port) + '\n\tdefault_bind 127.0.0.1\n', 1))
        subprocess.run([str(caddy), 'adapt', '--config', str(source), '--adapter', 'caddyfile', '--validate'],
                       cwd=directory, env=environment, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, timeout=30)
        log = output / 'workload-http.log'
        with log.open('wb') as errors:
            process = subprocess.Popen([str(caddy), 'run', '--config', str(source), '--adapter', 'caddyfile'],
                                       cwd=directory, env=environment, stdout=errors, stderr=errors)
            try:
                if on_started is not None:
                    on_started(process.pid)
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                base = 'http://127.0.0.1:' + str(port)
                deadline = time.monotonic() + 30
                while True:
                    if process.poll() is not None:
                        raise ValueError('owned workload HTTP fixture stopped before readiness')
                    try:
                        with opener.open(urllib.request.Request(base + '/', headers={'Host': 'unclaimed.invalid'}), timeout=2) as response:
                            if response.status == 404:
                                break
                    except urllib.error.HTTPError as error:
                        if error.code == 404:
                            break
                    except (OSError, urllib.error.URLError):
                        pass
                    if time.monotonic() >= deadline:
                        raise ValueError('owned workload HTTP fixture readiness deadline exceeded')
                    time.sleep(.05)
                origin = 'http://' + hostname + ':' + str(port)
                values = environment | {'E2E_URL': origin + '/', 'GATE_BASE_URL': base,
                    'GATE_HOSTNAME': hostname, 'GATE_PORT': str(port), 'WORKLOAD_FIXTURE_PID': str(process.pid),
                    'GATE_URL': base, 'GATE_DESCRIPTOR_FILE': str((packet / 'release.json').absolute()),
                    'EXPECTED_DESCRIPTOR_DIGEST': 'sha256:' + artifact_packet.digest_file(packet / 'release.json')[0]}
                yield SimpleNamespace(environment=values, port=port, pid=process.pid, base_url=base,
                                      origin=origin, hostname=hostname, assets=assets, release=release)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
