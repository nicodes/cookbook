"""Prepare closed Ubuntu CI dependency profiles without changing host APT configuration."""
import argparse
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import time

PREPARATION_SECONDS = 300
KEYRING = Path('/usr/share/keyrings/ubuntu-archive-keyring.gpg')
OS_RELEASE = Path('/etc/os-release')
APT = '/usr/bin/apt-get'
DPKG = '/usr/bin/dpkg'
TIMEOUT = '/usr/bin/timeout'
PROFILES = {
    'linker': ('musl-tools',),
    'native-host': ('musl-tools', 'qemu-system-x86', 'qemu-utils', 'cloud-image-utils', 'acl'),
    'owner-rehearsal': ('musl-tools', 'qemu-system-x86', 'qemu-utils', 'cloud-image-utils',
        'acl', 'gpg', 'gcc', 'libc6-dev', 'openssl'),
}
MIRRORS = {
    ('jammy', 'amd64'): ('https://archive.ubuntu.com/ubuntu/',
        'https://mirrors.edge.kernel.org/ubuntu/'),
    ('noble', 'amd64'): ('https://archive.ubuntu.com/ubuntu/',
        'https://mirrors.edge.kernel.org/ubuntu/'),
    ('noble', 'arm64'): ('https://ports.ubuntu.com/ubuntu-ports/',
        'https://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports/'),
}


def run(command, deadline, environment, *, capture=False):
    # GNU timeout is an independent finite supervisor even if this Python
    # caller exits. Both package phases consume the same original deadline.
    remaining = deadline - time.monotonic() - 3
    if remaining < 0.01:
        raise TimeoutError('Ubuntu CI preparation deadline exhausted')
    return subprocess.run([TIMEOUT, '--signal=TERM', '--kill-after=1s',
        f'{remaining:.3f}s', *command], check=True, stdin=subprocess.DEVNULL,
        env=environment, capture_output=capture, timeout=remaining + 2)


def host(deadline, environment):
    if os.geteuid() != 0:
        raise ValueError('Ubuntu CI dependency preparation requires sudo')
    release = {}
    for line in OS_RELEASE.read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        key, separator, raw = line.partition('=')
        if not separator or key in release:
            raise ValueError('ambiguous operating system identity')
        value = shlex.split(raw)
        if len(value) != 1:
            raise ValueError('invalid operating system identity')
        release[key] = value[0]
    codename = release.get('VERSION_CODENAME')
    if release.get('ID') != 'ubuntu' or (release.get('VERSION_ID'), codename) not in {
            ('22.04', 'jammy'), ('24.04', 'noble')}:
        raise ValueError('select a supported Ubuntu CI runner')
    architecture = run([DPKG, '--print-architecture'], deadline, environment,
        capture=True).stdout.decode().strip()
    if (codename, architecture) not in MIRRORS:
        raise ValueError('unsupported Ubuntu CI release or native architecture')
    metadata = KEYRING.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size == 0:
        raise ValueError('the existing Ubuntu archive keyring is required')
    return codename, architecture


def prepare(profile):
    if profile not in PROFILES:
        raise ValueError('select a closed Ubuntu CI dependency profile')
    deadline = time.monotonic() + PREPARATION_SECONDS
    environment = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
        'LANG': 'C.UTF-8', 'DEBIAN_FRONTEND': 'noninteractive'}
    codename, architecture = host(deadline, environment)
    with tempfile.TemporaryDirectory(prefix='cookbook-ubuntu-ci-') as temporary:
        root = Path(temporary)
        root.chmod(0o755)  # _apt must traverse the public, job-owned source/state tree.
        for name in ['sourceparts', 'confparts', 'state', 'state/lists',
                'cache', 'cache/archives', 'log']:
            (root / name).mkdir(mode=0o755)
            (root / name).chmod(0o755)
        # APT's mirror transport retries each index or package on the next
        # fixed mirror. This local list adds no remote mirror-selection input.
        mirrors = root / 'mirrors.txt'
        mirrors.write_text(''.join(uri + '\tpriority:' + str(priority) + '\n'
            for priority, uri in enumerate(MIRRORS[codename, architecture], 1)))
        mirrors.chmod(0o644)
        sources = root / 'ubuntu.sources'
        sources.write_text('Types: deb\nURIs: mirror+' + mirrors.as_uri()
            + '\nSuites: ' + ' '.join(codename + suffix for suffix in
                ['', '-updates', '-security', '-backports'])
            + '\nComponents: main restricted universe multiverse\nArchitectures: '
            + architecture + '\nSigned-By: ' + str(KEYRING) + '\n')
        sources.chmod(0o644)
        settings = {
            'Dir::Etc::main': '/dev/null', 'Dir::Etc::parts': str(root / 'confparts'),
            'Dir::Etc::sourcelist': str(sources), 'Dir::Etc::sourceparts': str(root / 'sourceparts'),
            'Dir::State': str(root / 'state'), 'Dir::State::lists': str(root / 'state/lists'),
            'Dir::State::status': '/var/lib/dpkg/status',
            'Dir::Cache': str(root / 'cache'), 'Dir::Cache::archives': str(root / 'cache/archives'),
            'Dir::Log': str(root / 'log'), 'APT::Architecture': architecture,
            'Acquire::Retries': '1', 'Acquire::http::Timeout': '20', 'Acquire::https::Timeout': '20',
            'Acquire::Check-Date': 'true', 'Acquire::Check-Valid-Until': 'true',
            'Acquire::AllowInsecureRepositories': 'false',
            'Acquire::AllowDowngradeToInsecureRepositories': 'false',
            'APT::Get::AllowUnauthenticated': 'false', 'DPkg::Lock::Timeout': '20',
            'Dpkg::Use-Pty': '0',
        }
        config = root / 'apt.conf'
        config.write_text(''.join(key + ' "' + value + '";\n' for key, value in settings.items()))
        config.chmod(0o644)
        # APT_CONFIG is read before the global main/parts files; redirecting
        # those here prevents inherited mirror/proxy/hooks from entering this call.
        environment['APT_CONFIG'] = str(config)
        options = [item for key, value in settings.items() for item in ['-o', key + '=' + value]]
        options += ['-o', 'Dpkg::Options::=--force-confdef', '-o', 'Dpkg::Options::=--force-confold']
        run([APT, *options, 'update', '--error-on=any'], deadline, environment)
        run([APT, *options, 'install', '--yes', '--no-install-recommends', *PROFILES[profile]],
            deadline, environment)
    print('Prepared Ubuntu CI dependency profile: ' + profile)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile', choices=sorted(PROFILES))
    prepare(parser.parse_args().profile)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('Ubuntu CI preparation failed: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
