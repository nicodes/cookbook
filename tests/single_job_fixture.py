#!/usr/bin/env python3
"""Write a complete, committed minimal Godot addon source for one-job validation rehearsals.

Usage: single_job_fixture.py DESTINATION COOKBOOK_REVISION
Needs TOOLS_ROOT to name the separate pinned Tools checkout. Prints the fixture commit.
"""
import hashlib
from pathlib import Path
import subprocess
import sys

COOKBOOK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(COOKBOOK / 'cookbook'))
import dependencies
import generate

# The exact official Godot 4.7.2 archives that the host setup always prepares.
GODOT = {
    'GODOT_VERSION': '4.7.2',
    'GODOT_EXPECTED_VERSION': '4.7.2.stable.official.ed1daf0bf',
    'GODOT_BINARY_CHECKSUM': 'sha256:cadd3204e728a35d3f13adb7fd0d7902636b79f6b95c40c265eb73b6c35329e4',
    'GODOT_TEMPLATES_CHECKSUM': 'sha512:ca4d71c4d7b81dfc15d1a98baa07534aa95b03fdda78a0075b06672e1648d2e5f40980c9adc28d23e1b92e732ee7bf3461997aa804af74ec2fcd7a93ccb84079',
}
README = '# Fixture addon\nNative single-job validation fixture.\n'


def write(root, revision):
    root = Path(root)
    (root / 'addon').mkdir(parents=True)
    (root / 'tests').mkdir()
    (root / '.github').mkdir()
    (root / 'README.md').write_text(README)
    dependencies.helper('source_config').write(root / '.github/readme-source.toml', {
        'version': 1, 'sha256': hashlib.sha256(README.encode()).hexdigest()})
    (root / 'addon/plugin.cfg').write_text('[plugin]\nversion="0.0.1"\n')
    (root / 'package.files').write_text('plugin.cfg\n')
    (root / 'tests/test_behavior.py').write_text('assert 2 + 2 == 4\n')
    (root / '.gitignore').write_text('/.artifacts/\n/dist/\n')
    (root / 'cookbook.toml').write_text('''version=1
recipe="godot-addon"
[package]
archive="dist/@owner_addon.zip"
[clean]
paths=["dist"]
[[tests.unit]]
runner="python"
path="tests/test_behavior.py"
[[tests.integration]]
runner="python"
path="tests/test_behavior.py"
''')
    (root / 'mise.toml').write_text('[tools]\npython="3.13.11"\nactionlint="1.7.12"\n[env]\n'
                                    + ''.join(key + '="' + value + '"\n' for key, value in GODOT.items()))
    files = {**generate.entrypoint_files(root, revision), **generate.workflow_files(root, revision)}
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body.encode('utf-8'))
    for argv in [['init', '-q', '-b', 'main'], ['add', '.'],
                 ['-c', 'user.name=Recipe Controls', '-c', 'user.email=recipe@example.invalid',
                  'commit', '-qm', 'single-job fixture']]:
        subprocess.run(['git', '-C', str(root), *argv], check=True)
    return subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()


if __name__ == '__main__':
    destination, revision = sys.argv[1:]
    print(write(destination, revision))
