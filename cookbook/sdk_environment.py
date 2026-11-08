"""Preserve SDK installation directories before isolating application data homes."""
import os
from pathlib import Path, PureWindowsPath
import platform
import sys

from sdk_source import python_entry as isolated_python_entry


def python_entry(entry, *arguments):
    return isolated_python_entry(sys.executable, entry, *arguments)


def install_environment(root, environment, selected=''):
    """Select the lower helper's context before explicit SDK installation only."""
    import dependencies
    context = dependencies.helper('rust_install_context')
    return context.environment(environment) if context.needs_rust(root, selected) else dict(environment)


def publish_install_environment(environment):
    """Carry the prepared context to later Actions steps after successful setup."""
    if environment.get('GITHUB_ENV') and 'CARGO_HOME' in environment:
        value = environment['CARGO_HOME']
        if not isinstance(value, str) or any(character in value for character in '\r\n\0'):
            raise ValueError('prepared Cargo home cannot be represented in the Actions environment')
        with open(environment['GITHUB_ENV'], 'a') as output:
            output.write('CARGO_HOME=' + value + '\n')


def command_environment(command, root, environment):
    """Guard only the SDK executable actually invoked at a native boundary."""
    import dependencies
    if command[:2] == ['mise', 'install']:
        # All recipe/worker installers use this existing native boundary. Retain
        # the pure context in their setup map for the following native commands.
        environment.update(install_environment(root, environment, ' '.join(command[2:])))
    tools = {'cargo': ('rust', 'cargo'), 'rustc': ('rust', 'rustc'),
             'go': ('go', 'go'),
             'python3': ('python', 'python3'), 'python': ('python', 'python'),
             'node': ('node', 'node'), 'bun': ('bun', 'bun'),
             'actionlint': ('actionlint', 'actionlint'), 'shellcheck': ('shellcheck', 'shellcheck'),
             'cargo-audit': ('cargo:cargo-audit', 'cargo-audit'),
             'govulncheck': ('go:golang.org/x/vuln/cmd/govulncheck', 'govulncheck')}
    selected = tools.get(command[0]) if command else None
    required = [('rust', 'cargo'), ('rust', 'rustc')] if command and command[0] == 'rustup' else ([selected] if selected else [])
    if command and command[0] in {'npm', 'npx'}:
        required = [('node', 'node'), ('node', command[0])]
    if not required:
        return {**environment, 'MISE_AUTO_INSTALL': 'false', 'MISE_EXEC_AUTO_INSTALL': 'false',
                'MISE_TASK_RUN_AUTO_INSTALL': 'false',
                'BUN_OPTIONS': '--no-env-file', 'EXPO_NO_DOTENV': '1'}
    caller = Path(environment.get('COOKBOOK_CALLER_ROOT', root))
    if not caller.is_absolute() or not Path(root).resolve().is_relative_to(caller.resolve()):
        raise ValueError('native SDK execution must retain its derived caller root')
    values = dependencies.helper('pinned-tools').activation(caller, required, environment)
    for key in ['COOKBOOK_ROOT', 'COOKBOOK_REF', 'TOOLS_ROOT', 'TOOLS_REF', 'COOKBOOK_CALLER_ROOT']:
        if key in environment and values.get(key) != environment[key]:
            raise ValueError('SDK activation must preserve both immutable source identities')
    return {**values, 'BUN_OPTIONS': '--no-env-file', 'EXPO_NO_DOTENV': '1'}


def preserve_mise(environment=None, *, system=None):
    values = dict(os.environ if environment is None else environment)
    if 'MISE_DATA_DIR' in values:
        return values
    windows = (system or platform.system()) == 'Windows'
    path = PureWindowsPath if windows else Path
    if values.get('XDG_DATA_HOME'):
        directory = path(values['XDG_DATA_HOME']) / 'mise'
    elif windows:
        directory = (path(values['LOCALAPPDATA']) if values.get('LOCALAPPDATA') else
                     path(values.get('USERPROFILE') or values.get('HOME') or str(Path.home())) / 'AppData/Local') / 'mise'
    else:
        directory = path(values.get('HOME') or str(Path.home())) / '.local/share/mise'
    values['MISE_DATA_DIR'] = str(directory)
    return values
