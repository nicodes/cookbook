"""Prepare native addon export fixtures during build, before tests consume them."""
import importlib.util
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import zipfile

from configuration import relative

import dependencies

COOKBOOK = Path(__file__).resolve().parents[1]
TOOLS = dependencies.tools_root()


def package_helper():
    return dependencies.helper('addon-package')


def records(root, config):
    entries = config.get('components', {}).get('fixture_exports', [])
    if not isinstance(entries, list):
        raise ValueError('addon fixture exports must be native export records')
    destinations = set()
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) - {'project', 'preset', 'output', 'timeout', 'required_outputs', 'install_archive', 'import_timeout', 'allowed_diagnostics'}
                or not {'project', 'preset', 'output', 'timeout', 'required_outputs'} <= set(entry)):
            raise ValueError('invalid addon fixture export')
        project = relative(root, entry['project'], exists=True)
        output = relative(root, entry['output'])
        if not project.is_dir() or not (project / 'project.godot').is_file():
            raise ValueError('addon export fixture must contain its native Godot project')
        if not output.is_relative_to(root / 'dist') or output.suffix != '.html' or output.parent in destinations:
            raise ValueError('addon fixture outputs require distinct dist directories')
        marker = relative(root, (output.parent / '.gdignore').relative_to(root).as_posix())
        if marker.exists() and not marker.is_file():
            raise ValueError('addon export import boundary must be a regular file')
        destinations.add(output.parent)
        if (not isinstance(entry['preset'], str) or not entry['preset'] or '\n' in entry['preset']
                or type(entry['timeout']) is not int or not 1 <= entry['timeout'] <= 3600):
            raise ValueError('addon native exports require a preset and bounded deadline')
        if 'import_timeout' in entry and (type(entry['import_timeout']) is not int or not 1 <= entry['import_timeout'] <= 3600):
            raise ValueError('addon native imports require a bounded deadline')
        diagnostics = entry.get('allowed_diagnostics', [])
        if (not isinstance(diagnostics, list) or any(not isinstance(line, str) or not line
                or '\n' in line or '\r' in line for line in diagnostics)):
            raise ValueError('addon diagnostic allowances require exact individual lines')
        required = entry['required_outputs']
        if not isinstance(required, list) or not required or len(set(required)) != len(required):
            raise ValueError('addon fixture must declare its required output files')
        for name in required:
            candidate = relative(output.parent, name)
            if candidate == output.parent:
                raise ValueError('addon export output must name a file')
        if 'install_archive' in entry:
            install = PurePosixPath(entry['install_archive'])
            if (len(install.parts) != 2 or install.parts[0] != 'addons'
                    or install.as_posix() != entry['install_archive'] or '\\' in entry['install_archive']
                    or ':' in entry['install_archive'] or install.parts[1] in {'.', '..'}):
                raise ValueError('installed fixture requires one addon directory')
        yield entry, project, output


def artifact_paths(root, config):
    return [output.parent.relative_to(root).as_posix() for _, _, output in records(root, config)]


def build_exports(root, config, environment, *, execute=subprocess.run):
    root = Path(root).resolve()
    plan = list(records(root, config))  # Validate the complete plan before any export.
    for entry, project, output in plan:
        with tempfile.TemporaryDirectory(prefix='addon-export-') as directory:
            installed = None
            if 'install_archive' in entry:
                # Only a fixture project is copied; never recursively copy the repository.
                if not project.is_relative_to(root / 'tests'):
                    raise ValueError('installed addon exports require a native tests fixture')
                if any(path.is_symlink() for path in project.rglob('*')):
                    raise ValueError('addon export fixture cannot contain symlinks')
                native = Path(directory) / 'project'
                shutil.copytree(project, native)
                package = config['package']
                archive = relative(root, package['archive'], exists=True)
                manifest = relative(root, package['manifest'], exists=True)
                helper = package_helper()
                helper.verify(archive, manifest, source=relative(root, package['source'], exists=True),
                              script_uids=package.get('script_uids', False))
                helper.verify_checksum(archive)
                installed = native / entry['install_archive']
                installed.mkdir(parents=True, exist_ok=False)
                with zipfile.ZipFile(archive) as package_zip:
                    package_zip.extractall(installed)
                helper.verify(archive, manifest, installed=installed, script_uids=package.get('script_uids', False))
                project = native
            output.parent.mkdir(parents=True, exist_ok=True)
            def native(arguments, timeout):
                result = execute([environment['GODOT_BIN'], '--headless', '--path', str(project), *arguments],
                                 cwd=root, env=environment, timeout=timeout,
                                 capture_output=True, text=True, check=False)
                log = (result.stdout or '') + (result.stderr or '')
                print(log, end='')
                filtered = '\n'.join(line for line in log.splitlines()
                                     if line not in entry.get('allowed_diagnostics', []))
                if result.returncode or re.search(r'(?m)(?:^|\s)(?:SCRIPT ERROR:|ERROR:|FAIL:)', filtered):
                    raise ValueError('addon fixture export failed')
            if 'import_timeout' in entry:
                native(['--editor', '--import', '--quit'], entry['import_timeout'])
            # Delete prior outputs so successful verification cannot use stale files.
            for name in entry['required_outputs']:
                target = relative(output.parent, name)
                if target.exists():
                    if not target.is_file():
                        raise ValueError('addon export output must be a regular file')
                    target.unlink()
            # A later editor assertion can scan the repository project root.
            # Exported PNGs are outputs, not new editor inputs: importing them
            # would add .import files and invalidate the closed build receipt.
            marker = relative(root, (output.parent / '.gdignore').relative_to(root).as_posix())
            if marker.exists():
                if not marker.is_file():
                    raise ValueError('addon export import boundary must be a regular file')
            else:
                marker.touch(exist_ok=False)
            native(['--export-release', entry['preset'], str(output)], entry['timeout'])
            for name in entry['required_outputs']:
                target = relative(output.parent, name, exists=True)
                if not target.is_file() or target.stat().st_size == 0:
                    raise ValueError('addon fixture output is empty: ' + name)
            if installed is not None:
                helper.verify(archive, manifest, installed=installed, script_uids=package.get('script_uids', False))
