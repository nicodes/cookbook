"""Serve exact accepted static artifacts with the native workload renderer."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import json
import os
from pathlib import Path
import platform
import re
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
import deployment
import komizo_core
import native_proxy


def proxy_platform():
    machine = platform.machine()
    return {'x86_64': 'Linux/x86_64', 'aarch64': 'Linux/arm64', 'arm64': 'Linux/arm64'}.get(machine) if sys.platform == 'linux' else None


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
    target = proxy_platform()
    if not target:
        raise ValueError('HTTP fixtures require a reviewed Linux Caddy or an explicit staged KOMIZO_CADDY')
    native_proxy.verify(root, target)
    return native_proxy.output(root, target)


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
    target = proxy_platform()
    if target:
        native_proxy.fetch(root, target)
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
