"""Render the reviewed stdlib seed used before any Cookbook SDK imports."""
from pathlib import Path
import shlex
import hashlib
import seed_transport
import private_directory


def private_steps(indent=6, *, composite=False):
    """Prepare Windows' first parent with a direct trusted native interpreter."""
    if type(indent) is not int or not 0 <= indent <= 12 or type(composite) is not bool:
        raise ValueError('SDK preparation requires one bounded YAML indentation')
    command = private_directory.shell('"$PRIVATE_PYTHON"')
    raw = Path(__file__).with_name('bootstrap-python.ps1').read_bytes()
    if hashlib.sha256(raw).hexdigest() != '51f0189e08a2ec3890d2d490a82fb2a7fab3850377c3198c4882def06ce92a33':
        raise ValueError('Windows bootstrap host primitive differs from the exact reviewed Tools mirror')
    host = '''- name: Select an existing compatible Windows bootstrap host
  if: runner.os == 'Windows'
  id: sdk-private-host
  timeout-minutes: 1
  shell: pwsh
  env:
    MISE_AUTO_INSTALL: 'false'
    MISE_EXEC_AUTO_INSTALL: 'false'
    MISE_TASK_RUN_AUTO_INSTALL: 'false'
  run: |
''' + ''.join('    ' + line if line.strip() else line for line in raw.decode('utf-8').splitlines(keepends=True))
    text = '''- name: Bootstrap Python for protected SDK sources
  if: runner.os == 'Windows' && steps.sdk-private-host.outputs.found != 'true'
  id: sdk-private-python
  timeout-minutes: 5
  uses: actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405
  env:
    MISE_AUTO_INSTALL: 'false'
    MISE_EXEC_AUTO_INSTALL: 'false'
    MISE_TASK_RUN_AUTO_INSTALL: 'false'
  with:
    python-version: '3.13.11'
    check-latest: false
- name: Prepare the private artifact parent with a verified direct Python
  if: runner.os == 'Windows'
  timeout-minutes: 1
  shell: bash
  env:
    PRIVATE_PYTHON: ${{ steps.sdk-private-host.outputs.python-path || steps.sdk-private-python.outputs.python-path }}
    MISE_AUTO_INSTALL: 'false'
    MISE_EXEC_AUTO_INSTALL: 'false'
    MISE_TASK_RUN_AUTO_INSTALL: 'false'
  run: |
    set -euo pipefail
    COMMAND
    printf 'COOKBOOK_BOOTSTRAP_PYTHON=%s\\n' "$PRIVATE_PYTHON" >> "$GITHUB_ENV"
    printf '%s\\n' 'MISE_AUTO_INSTALL=false' 'MISE_EXEC_AUTO_INSTALL=false' 'MISE_TASK_RUN_AUTO_INSTALL=false' >> "$GITHUB_ENV"
'''.replace('COMMAND', command)
    if composite:
        # Composite metadata has no step timeout key; its existing caller cap
        # continues to bound the whole preparation action.
        text = text.replace('  timeout-minutes: 5\n', '').replace('  timeout-minutes: 1\n', '')
        host = host.replace('  timeout-minutes: 1\n', '')
    return ''.join(' ' * indent + line if line.strip() else line for line in (host + text).splitlines(keepends=True))

SEED = Path(__file__).with_name('sdk_source.py').read_text() + '''
import sys
root = Path(os.environ["GITHUB_ACTION_PATH"]).resolve().parent
revision = sys.argv[1]
verify(root, revision, repository="nicodes/cookbook")
provider = identity(read_source(root))
outer = no_links(Path.cwd() / ".artifacts/recipe-sdks")
if root.parent != outer:
    raise SystemExit("workflow SDK sources require their one generated cache parent")
marker = no_links(outer / ".gdignore")
if marker.exists():
    if read_regular(marker) != b"":
        raise SystemExit("engine import marker must be the existing empty generated file")
else:
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(descriptor)
tools = no_links(outer / "_tools")
if tools.exists():
    verify(tools, provider["revision"], repository="nicodes/tools")
with open(os.environ["GITHUB_OUTPUT"], "a") as output:
    output.write("tools-ref=" + provider["revision"] + "\\n")
'''


# Fixed reviewed payload of the readable seed above; rendering never compresses.
SEED_LENGTH = 6586
SEED_SHA256 = 'bbc05deb1315b3376916517b7cc99f789cbd640b5a191297a1d11edb587273dd'
ENCODED_SEED = (
    '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'
)


def action():
    source = seed_transport.seed_transport(SEED, encoded=ENCODED_SEED, length=SEED_LENGTH, sha256=SEED_SHA256)
    command = '"${COOKBOOK_BOOTSTRAP_PYTHON:-python3}" -I -B -c ' + shlex.quote(source) + ' "$COOKBOOK_REF"'
    indented = '\n'.join('          ' + line for line in command.splitlines())
    return '''name: Prepare Cookbook source dependencies
description: Resolve and check separate immutable Cookbook and Tools Git sources
inputs:
  cookbook-ref:
    description: Exact selected Cookbook source revision
    required: true
runs:
  using: composite
  steps:
    - name: Inspect the host parser without loading SDK code
      id: host
      shell: bash
      env:
        MISE_AUTO_INSTALL: 'false'
        MISE_EXEC_AUTO_INSTALL: 'false'
        MISE_TASK_RUN_AUTO_INSTALL: 'false'
      run: '"${COOKBOOK_BOOTSTRAP_PYTHON:-python3}" -I -B -c ''import sys; print("bootstrap-python=" + str(sys.version_info < (3, 11)).lower())'' >> "$GITHUB_OUTPUT"'
    - name: Bootstrap the supported configuration parser
      id: parser
      if: steps.host.outputs.bootstrap-python == 'true'
      uses: actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405
      env:
        MISE_AUTO_INSTALL: 'false'
        MISE_EXEC_AUTO_INSTALL: 'false'
        MISE_TASK_RUN_AUTO_INSTALL: 'false'
      with:
        python-version: '3.13.11'
        check-latest: false
    - name: Keep the directly prepared parser before SDK imports
      if: steps.host.outputs.bootstrap-python == 'true'
      shell: bash
      env:
        DIRECT_PYTHON: ${{ steps.parser.outputs.python-path }}
        MISE_AUTO_INSTALL: 'false'
        MISE_EXEC_AUTO_INSTALL: 'false'
        MISE_TASK_RUN_AUTO_INSTALL: 'false'
      run: printf 'COOKBOOK_BOOTSTRAP_PYTHON=%s\\n' "$DIRECT_PYTHON" >> "$GITHUB_ENV"
    - name: Verify Cookbook source before reading its Tools dependency
      id: dependency
      shell: bash
      env:
        COOKBOOK_REF: ${{ inputs.cookbook-ref }}
        MISE_AUTO_INSTALL: 'false'
        MISE_EXEC_AUTO_INSTALL: 'false'
        MISE_TASK_RUN_AUTO_INSTALL: 'false'
      run: |
''' + indented + '''
    - name: Require the dependency to be accepted on Tools main
      shell: bash
      env:
        TOOLS_REF: ${{ steps.dependency.outputs.tools-ref }}
        GH_TOKEN: ${{ github.token }}
        MISE_AUTO_INSTALL: 'false'
        MISE_EXEC_AUTO_INSTALL: 'false'
        MISE_TASK_RUN_AUTO_INSTALL: 'false'
      run: |
        set -euo pipefail
        relation="$(gh api "repos/nicodes/tools/compare/$TOOLS_REF...main" --jq .status)"
        [[ "$relation" == ahead || "$relation" == identical ]]
    - name: Check out the exact generic Tools dependency
      uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
      with:
        repository: nicodes/tools
        ref: ${{ steps.dependency.outputs.tools-ref }}
        path: .artifacts/recipe-sdks/_tools
        persist-credentials: false
    - name: Bind separate checked source roots
      shell: bash
      env:
        COOKBOOK_REF: ${{ inputs.cookbook-ref }}
        TOOLS_REF: ${{ steps.dependency.outputs.tools-ref }}
        MISE_AUTO_INSTALL: 'false'
        MISE_EXEC_AUTO_INSTALL: 'false'
        MISE_TASK_RUN_AUTO_INSTALL: 'false'
      run: |
        "${COOKBOOK_BOOTSTRAP_PYTHON:-python3}" -I -B -c 'import os, pathlib, runpy, sys; root=pathlib.Path(os.environ["GITHUB_ACTION_PATH"]).resolve().parent; sys.path.insert(0,str(root/"cookbook")); runpy.run_path(str(root/"cookbook/provider_dependency.py"),run_name="__main__")'
'''


if __name__ == '__main__':
    print(action(), end='')
