"""Render the fixed Cookbook/Tools source bootstrap for generated Mise tasks."""
from pathlib import Path
import re
import shlex
import seed_transport

# Versioned mirror of the standalone Tools identity primitive. No downloaded
# source is imported before its repository/ref/own-root/clean checks pass.
SEED = Path(__file__).with_name('sdk_source.py').read_text() + '''
import sys
cookbook, mode, *arguments = sys.argv[1:]
if not re.fullmatch("[a-f0-9]{40}", cookbook) or mode not in {"setup", "exec"}:
    raise SystemExit("Cookbook bootstrap requires its immutable source and explicit operation")
root = Path.cwd().resolve()
raw = read_regular(root / "mise.toml")
caller = tomllib.loads(raw.decode().replace("\\r\\n", "\\n").replace("\\r", "\\n"))
variables = caller.get("vars", {})
if type(variables.get("cookbook_contract")) is not int or variables["cookbook_contract"] != 1 or variables.get("cookbook_ref") != cookbook:
    raise SystemExit("Cookbook bootstrap differs from its caller-declared source")
version = caller.get("tools", {}).get("python")
version = version.get("version") if isinstance(version, dict) else version
if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\\.[0-9]+\\.[0-9]+", version):
    raise SystemExit("bootstrap Python requires the exact caller-pinned version")
environment = dict(os.environ, MISE_AUTO_INSTALL="false", MISE_EXEC_AUTO_INSTALL="false", MISE_TASK_RUN_AUTO_INSTALL="false")
install = subprocess.check_output(["mise", "where", "python@" + version], cwd=root,
    env=environment, text=True, timeout=30).strip()
directory = Path(install)
if not directory.is_absolute() or not directory.is_dir():
    raise SystemExit("exact bootstrap Python is absent; run explicit setup")
candidates = [directory / "bin/python3", directory / "python.exe"]
interpreter = next((path.resolve() for path in candidates if path.is_file()
    and path.resolve().is_relative_to(directory.resolve())), None)
if interpreter is None:
    raise SystemExit("exact bootstrap Python executable is absent; run explicit setup")
actual = subprocess.check_output([str(interpreter), "-I", "-B", "-c",
    "import sys; print('.'.join(map(str,sys.version_info[:3])))"], cwd=root,
    env=environment, text=True, timeout=30).strip()
if actual != version:
    raise SystemExit("bootstrap Python differs from the exact caller pin")
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
if not arguments:
    raise SystemExit("select an implemented Cookbook operation")
name, *argv = arguments
if not re.fullmatch(r"(?:cookbook|helpers)/[a-z][a-z0-9_-]*\\.py", name):
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
SEED_LENGTH = 9629
SEED_SHA256 = 'e7649155f5992e1ece30f3246a5d57ec145486d5e8fb603bf20b813521415fa3'
ENCODED_SEED = (
    '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'
    'oJu1tw1LHsqv92/XWHRp'
)


def command(revision):
    if not isinstance(revision, str) or not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('Cookbook bootstrap requires one immutable source pin')
    source = seed_transport.seed_transport(SEED, encoded=ENCODED_SEED, length=SEED_LENGTH, sha256=SEED_SHA256)
    return 'exec python3 -I -B -c ' + shlex.quote(source) + ' ' + revision
