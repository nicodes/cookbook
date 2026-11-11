"""CI-only real APT per-file failover; downloads packages without installing them."""
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading

spec = importlib.util.spec_from_file_location('ubuntu_ci_transport', Path(__file__).parents[1] / 'cookbook/ubuntu_ci.py')
ubuntu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ubuntu)


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.geteuid() != 0:
        raise ValueError('real APT transport qualification is restricted to sudo GitHub CI')
    # Every first-mirror request receives only 503. No repository data, key or
    # signature is supplied by the fixture; Ubuntu's real HTTPS archive is next.
    counts = {'index': 0, 'package': 0}
    lock = threading.Lock()

    class Unavailable(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            with lock:
                if self.path.startswith('/dists/'):
                    counts['index'] += 1
                elif self.path.startswith('/pool/'):
                    counts['package'] += 1
            self.send_response(503)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def log_message(self, *_):
            pass

    with tempfile.TemporaryDirectory(prefix='cookbook-apt-transport-') as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        log = root / 'phases.jsonl'
        wrapper = root / 'download-only-apt'
        wrapper.write_text('''#!/usr/bin/python3
import json,pathlib,subprocess,sys
arguments=sys.argv[1:];options={};position=0
while arguments[position]=='-o':
 key,value=arguments[position+1].split('=',1);options[key]=value;position+=2
phase=arguments[position]
assert phase in {'update','install'}
if phase=='install':arguments[position+1:position+1]=['--download-only','--reinstall']
result=subprocess.run(['/usr/bin/apt-get',*arguments],check=False)
count=len(list(pathlib.Path(options['Dir::Cache::archives']).glob('*.deb'))) if phase=='install' else 0
with open(''' + repr(str(log)) + ''','a') as output:
 output.write(json.dumps({'phase':phase,'exit_code':result.returncode,'downloaded_debs':count})+'\\n')
raise SystemExit(result.returncode)
''')
        wrapper.chmod(0o755)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Unavailable)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        original_mirrors = ubuntu.MIRRORS
        original_apt = ubuntu.APT
        status = Path('/var/lib/dpkg/status')
        before = hashlib.sha256(status.read_bytes()).hexdigest()
        try:
            ubuntu.MIRRORS = {key: ('http://127.0.0.1:' + str(server.server_port) + '/', mirrors[0])
                for key, mirrors in original_mirrors.items()}
            ubuntu.APT = str(wrapper)
            # Production helper, real signed indexes and real package downloads,
            # with its unchanged original 300-second preparation deadline.
            ubuntu.prepare('linker')
            phases = [json.loads(line) for line in log.read_text().splitlines()]
            if ([row['phase'] for row in phases] != ['update', 'install']
                    or any(row['exit_code'] != 0 for row in phases)
                    or phases[-1]['downloaded_debs'] < 1
                    or counts['index'] < 1 or counts['package'] < 1
                    or hashlib.sha256(status.read_bytes()).hexdigest() != before):
                raise ValueError('real index/package failover or download-only isolation was not observed')
            print(json.dumps({'kind': 'cookbook.ubuntu-mirror-transport', 'version': 1,
                'first_mirror_index_requests': counts['index'], 'first_mirror_package_requests': counts['package'],
                'downloaded_debs': phases[-1]['downloaded_debs'], 'dpkg_status_unchanged': True,
                'signed_ubuntu_update_and_package_download': True}, sort_keys=True))
        finally:
            ubuntu.MIRRORS = original_mirrors
            ubuntu.APT = original_apt
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)


if __name__ == '__main__':
    main()
