"""Validate native Vite output reproducibility before Rust embeds its files."""
import hashlib
from pathlib import Path
import stat
import tempfile

from configuration import relative
import node_component


def records(values):
    if not isinstance(values, list) or len(values) > 1:
        raise ValueError('Rust CLI supports one optional app UI')
    result = []
    for declaration in values:
        if not isinstance(declaration, dict) or set(declaration) - {'required_files'}:
            raise ValueError('UI source and output use the canonical app and .artifacts/ui paths')
        required = declaration.get('required_files', ['index.html'])
        if (not isinstance(required, list) or not required or any(not isinstance(name, str) for name in required)
                or len(set(required)) != len(required) or 'index.html' not in required):
            raise ValueError('UI validation requires index.html and distinct native output identities')
        result.append({'package': 'app', 'output': '.artifacts/ui', 'required_files': required})
    return result


def snapshot(directory, required):
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('build the UI before validating embedded assets')
    result = {}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise ValueError('UI output must not contain symlinks')
        kind = path.stat().st_mode
        if stat.S_ISDIR(kind):
            continue
        if not stat.S_ISREG(kind):
            raise ValueError('UI output must contain regular files only')
        result[path.relative_to(directory).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not result or any(name not in result for name in required):
        raise ValueError('UI build omitted required native assets')
    return result


def validate_build(root, declarations, packages, environment, run):
    for declaration in records(declarations):
        package_path = relative(root, declaration.get('package'), exists=True)
        matches = [node_component.validate(value, root) for value in packages]
        matches = [(package, directory) for package, directory in matches if directory == package_path]
        if len(matches) != 1 or not matches[0][0].get('build'):
            raise ValueError('UI reproducibility requires exactly one declared native package build')
        package, directory = matches[0]
        required = declaration.get('required_files', ['index.html'])
        if not isinstance(required, list) or not required:
            raise ValueError('UI validation requires real output identities')
        for name in required:
            relative(root, name)
        output = relative(root, declaration.get('output'))
        expected = snapshot(output, required)
        with tempfile.TemporaryDirectory(prefix='ui-rebuild-') as scratch:
            alternate = Path(scratch) / 'ui'
            # All current optional Solid UIs use Vite's native build outDir.
            node_component.run_script(package, directory, package['build'], environment, run,
                                      ['--outDir', str(alternate)])
            if snapshot(alternate, required) != expected:
                raise ValueError('locked UI builds produced different output bytes')
        print('UI output is complete and byte-reproducible: ' + declaration['package'])
