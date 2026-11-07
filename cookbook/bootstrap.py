"""Render the fixed Cookbook/Tools source bootstrap for generated Mise tasks."""
from pathlib import Path
import re
import shlex
import seed_transport
import private_directory

# Versioned mirror of the standalone Tools identity primitive. No downloaded
# source is imported before its repository/ref/own-root/clean checks pass.
SEED = Path(__file__).with_name('sdk_source.py').read_text() + r'''
import sys
cookbook, mode, *arguments = sys.argv[1:]
if not re.fullmatch("[a-f0-9]{40}", cookbook) or mode not in {"setup", "exec"}:
    raise SystemExit("Cookbook bootstrap requires its immutable source and explicit operation")
root = Path.cwd().resolve()
raw = read_regular(root / "mise.toml")
caller = tomllib.loads(raw.decode().replace("\r\n", "\n").replace("\r", "\n"))
variables = caller.get("vars", {})
if type(variables.get("cookbook_contract")) is not int or variables["cookbook_contract"] != 1 or variables.get("cookbook_ref") != cookbook:
    raise SystemExit("Cookbook bootstrap differs from its caller-declared source")
''' + private_directory.SELECTION + r'''if os.name == "nt" and tuple(map(int, actual.split(".")[:2])) < (3, 13):
    raise SystemExit("Windows private SDK directories require the selected Python 3.13 or newer")
if mode == "setup":
    subprocess.run([str(interpreter), "-I", "-B", "-c", 'import os\nfrom pathlib import Path\nimport stat\nimport sys\nif os.name == "nt" and sys.version_info < (3, 13):\n    raise SystemExit("Windows private SDK directories require the selected Python 3.13 or newer")\nparent = Path.cwd() / ".artifacts"\nfor candidate in [parent, *parent.parents]:\n    try:\n        info = candidate.lstat()\n    except FileNotFoundError:\n        continue\n    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:\n        raise SystemExit("SDK artifact parent must not traverse links or reparse points")\nif parent.exists():\n    if not parent.is_dir():\n        raise SystemExit("SDK artifact parent must be a directory")\nelse:\n    parent.mkdir(mode=0o700)\n'], cwd=root, env=environment, check=True)
def checkout(repository, revision, name, supplied):
    directory = no_links(supplied or root / ".artifacts" / name / revision)
    if not directory.exists():
        if mode != "setup" or supplied:
            raise SystemExit("exact source is absent; run explicit setup")
        directory.mkdir(parents=True)
        url = "https://github.com/" + repository + ".git"
        git(directory, "init", "-q")
        git(directory, "remote", "add", "origin", url)
        git(directory, "fetch", "--quiet", "--depth=1", url, revision)
        git(directory, "checkout", "--quiet", "--detach", revision)
    return verify(directory, revision, repository=repository)
cookbook_root = checkout("nicodes/cookbook", cookbook, "cookbook", os.environ.get("COOKBOOK_ROOT"))
provider = identity(read_regular(cookbook_root / "mise.toml"))
tools = provider["revision"]
tools_root = checkout("nicodes/tools", tools, "tools", os.environ.get("TOOLS_ROOT"))
if tools_root.resolve() == cookbook_root.resolve():
    raise SystemExit("Cookbook and Tools roots must retain distinct source identities")
os.environ.update(COOKBOOK_ROOT=str(cookbook_root), COOKBOOK_REF=cookbook,
    TOOLS_ROOT=str(tools_root), TOOLS_REF=tools, COOKBOOK_CALLER_ROOT=str(root), MISE_AUTO_INSTALL="false",
    MISE_EXEC_AUTO_INSTALL="false", MISE_TASK_RUN_AUTO_INSTALL="false", PYTHONDONTWRITEBYTECODE="1")
if (root / ".artifacts").exists() or (root / ".artifacts").is_symlink():
    subprocess.run(python_entry(interpreter, cookbook_root / "cookbook/provider_dependency.py", "protect"),
        cwd=root, env=os.environ, check=True)
if not arguments:
    raise SystemExit("select an implemented Cookbook operation")
name, *argv = arguments
if not re.fullmatch(r"(?:cookbook|helpers)/[a-z][a-z0-9_-]*\.py", name):
    raise SystemExit("select an explicit Cookbook entrypoint")
entry = cookbook_root / name
if entry.is_symlink() or not entry.is_file():
    raise SystemExit("Cookbook entrypoint must be a tracked regular file")
git(cookbook_root, "ls-files", "--error-unmatch", "--", name)
if os.name == "nt":
    raise SystemExit(subprocess.run(python_entry(interpreter, entry, *argv)).returncode)
os.execv(str(interpreter), python_entry(interpreter, entry, *argv))
'''


# Fixed reviewed payload of the readable seed above; rendering never compresses.


# Fixed authoring-time payload; runtime rendering never compresses.
SEED_LENGTH = 10867
SEED_SHA256 = 'f41788a46dc04e965285e7945b6497c688537372738ab6bf425f6ef4aa0d3a3b'
ENCODED_SEED = (
    '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'
)


def command(revision):
    if not isinstance(revision, str) or not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('Cookbook bootstrap requires one immutable source pin')
    source = seed_transport.seed_transport(SEED, encoded=ENCODED_SEED, length=SEED_LENGTH, sha256=SEED_SHA256)
    return ('exec env MISE_AUTO_INSTALL=false MISE_EXEC_AUTO_INSTALL=false MISE_TASK_RUN_AUTO_INSTALL=false '
            '"${COOKBOOK_BOOTSTRAP_PYTHON:-python3}" -I -B -c ' + shlex.quote(source) + ' ' + revision)
