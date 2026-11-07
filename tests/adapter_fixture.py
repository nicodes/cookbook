"""Unit-only activation seam for existing native argv/output fixtures.

The source checks and native subprocesses remain real. Exact installed-tool
activation is covered separately by native Mise/SDK controls, so a fixture's
intentional compiler stub can continue proving the original adapter commands.
"""
import os
from pathlib import Path
import runpy
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cookbook'))
import sdk_identity
import sdk_environment

sdk_identity.verify(ROOT, os.environ['COOKBOOK_TEST_REF'])
real_run = subprocess.run


def nested(command, *arguments, **options):
    if isinstance(command, (list, tuple)):
        for index, value in enumerate(command):
            if not isinstance(value, str):
                continue
            path = Path(value)
            if path.is_absolute() and path.parent == ROOT / 'cookbook' and path.suffix == '.py':
                command = [sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                           str(path), *command[index + 1:]]
                break
    return real_run(command, *arguments, **options)


entry, *arguments = sys.argv[1:]
sys.argv = [entry, *arguments]
with patch.object(sdk_environment, 'command_environment', side_effect=lambda command, root, environment: dict(environment)), \
        patch.object(subprocess, 'run', side_effect=nested):
    runpy.run_path(entry, run_name='__main__')
