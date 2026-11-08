"""Bounded data-only identity and canonical generated Mise recipe tasks."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import tomllib

START = '# BEGIN GENERATED COOKBOOK TASKS v1\n'
END = '# END GENERATED COOKBOOK TASKS v1\n'
MAX_SOURCE = 262144
# SHA256 of the fixed public bootstrap command with its final SDK argument
# replaced by forty zeroes. Inspectors compare data; they never load the seed.
BOOTSTRAP_SHA256 = '74db42277cfb96fd4d3519e9c77b5ec14d474e6ad7a49c469808dfc1cbfb166f'
RECIPES = {'rust-cli', 'godot-addon', 'godot-game', 'react-native'}
SUITES = ['lint', 'unit', 'security', 'integration', 'e2e']
DATABASE = ['status', 'migrate', 'seed', 'reset']
DESCRIPTIONS = {
    'help': 'List implemented commands', 'setup': 'Prepare pinned tools and dependencies',
    'fmt': 'Apply formatting', 'build': 'Compile, package and validate outputs',
    'test': 'Run implemented test groups without building',
    'test:lint': 'Check formatting, code and configuration', 'test:unit': 'Run isolated behavior tests',
    'test:security': 'Run applicable security checks', 'test:integration': 'Test real component boundaries',
    'test:e2e': 'Run complete product journeys; requires current build',
    'clean': 'Remove declared outputs; preserve retained data',
    'dev': 'Run the implemented local stack', 'dev:stop': 'Stop this checkout development session',
    'dev:logs': 'Show this checkout development logs',
    'db:status': 'Show migration status', 'db:migrate': 'Apply development database migrations',
    'db:seed': 'Load development seed data', 'db:reset': 'Explicitly reset the disposable database',
}


def source_text(source):
    if isinstance(source, bytes):
        source = source.decode('utf-8')
    if not isinstance(source, str) or len(source.encode()) > MAX_SOURCE or '\0' in source:
        raise ValueError('Mise entrypoint requires bounded UTF-8 source')
    return source


def split(source, *, required=False):
    source = source_text(source)
    lines = re.split(r'(?<=\n)|(?<=\r)(?!\n)', source)
    starts = [i for i, line in enumerate(lines) if universal_text(line) == START]
    ends = [i for i, line in enumerate(lines) if universal_text(line) == END]
    if not starts and not ends and not required:
        return source, None
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ValueError('Mise source requires one complete generated cookbook block')
    prefix = ''.join(lines[:starts[0]])
    suffix = ''.join(lines[starts[0]:])
    if ''.join(lines[ends[0] + 1:]) or not prefix.endswith(('\n', '\r')):
        raise ValueError('generated cookbook block must be the complete source suffix')
    # Remove only the separator inserted by render; preserve all original SDK
    # prefix bytes and its native newline convention.
    return prefix[:-2] if prefix.endswith('\r\n') else prefix[:-1], suffix


def universal_text(source):
    return source.replace('\r\n', '\n').replace('\r', '\n')


def prefix_data(prefix):
    data = tomllib.loads(universal_text(prefix))
    if not isinstance(data.get('tools'), dict) or set(data) - {'tools', 'env'}:
        raise ValueError('recipe Mise source owns only SDKs, environment and canonical generated tasks')
    if set(data.get('env', {})) & {'TOOLS_ROOT', 'COOKBOOK_ROOT', 'TOOLS_REF', 'COOKBOOK_REF', 'COOKBOOK_CALLER_ROOT', 'COOKBOOK_BOOTSTRAP_PYTHON'}:
        raise ValueError('SDK locations and identities belong to the verified bootstrap environment')
    if any(name.startswith('MISE_') for name in data.get('env', {})):
        raise ValueError('recipe SDK environment cannot override the canonical Mise execution engine')
    directives = data.get('env', {}).get('_', {})
    if isinstance(directives, dict) and 'file' in directives:
        raise ValueError('Mise dotenv file loading is not supported; declare the process environment explicitly')
    if isinstance(directives, dict) and 'source' in directives:
        import repository_hygiene
        repository_hygiene.check_configuration({'source': directives['source']})
    return data


def names(suites, capabilities):
    if (not isinstance(suites, list) or len(suites) != len(set(suites))
            or not {'lint', 'unit', 'security'} <= set(suites) or set(suites) - set(SUITES)):
        raise ValueError('recipe tasks require every applicable distinct native test suite')
    allowed = {'dev', *('db:' + value for value in DATABASE)}
    if len(capabilities) != len(set(capabilities)) or set(capabilities) - allowed:
        raise ValueError('recipe tasks require implemented distinct capabilities')
    result = ['help', 'setup', 'fmt', 'build', 'test',
              *['test:' + suite for suite in SUITES if suite in suites], 'clean']
    if 'dev' in capabilities:
        result += ['dev', 'dev:stop', 'dev:logs']
    result += ['db:' + value for value in DATABASE if 'db:' + value in capabilities]
    return result


def bootstrap_template(run, revision):
    if not isinstance(run, str) or not run.endswith(' ' + revision):
        raise ValueError('Mise task requires its literal pinned public SDK bootstrap')
    template = run[:-40] + '0' * 40
    if hashlib.sha256(template.encode()).hexdigest() != BOOTSTRAP_SHA256:
        raise ValueError('Mise task bootstrap differs from the accepted generic source')
    return run


def block(revision, recipe, digest, suites, capabilities, *, bootstrap_run=None):
    if (not isinstance(revision, str) or not re.fullmatch('[a-f0-9]{40}', revision)
            or not isinstance(recipe, str) or recipe not in RECIPES
            or not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest)):
        raise ValueError('Mise task identity requires exact SDK, recipe and raw configuration hash')
    commands = names(suites, capabilities)
    if bootstrap_run is None:
        spec = importlib.util.spec_from_file_location('cookbook_task_bootstrap',
            Path(__file__).with_name('bootstrap.py'))
        bootstrap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bootstrap)
        bootstrap_run = bootstrap.command(revision)
    bootstrap_run = bootstrap_template(bootstrap_run, revision)
    lines = [START.rstrip('\n'), '[settings]', 'task.run_auto_install = false', '', '[vars]',
             'cookbook_contract = 1', 'cookbook_ref = ' + json.dumps(revision),
             'cookbook_recipe = ' + json.dumps(recipe), 'cookbook_sha256 = ' + json.dumps(digest), '',
             '[tasks._sdk]', 'hide = true', 'shell = "bash -euc"', 'cache = { enabled = false }',
             'run = ' + json.dumps(bootstrap_run)]
    help_text = '\n'.join(f'mise run {name:<18}{DESCRIPTIONS[name]}' for name in commands)
    for name in commands:
        lines += ['', '[tasks.' + json.dumps(name) + ']', 'description = ' + json.dumps(DESCRIPTIONS[name]),
                  'cache = { enabled = false }', 'shell = "bash -euc"']
        if name == 'help':
            import shlex
            lines += ['run = ' + json.dumps("printf '%s\\n' " + shlex.quote(help_text))]
        else:
            mode = 'setup' if name == 'setup' else 'exec'
            step = '{ task = "_sdk", args = [' + ', '.join(json.dumps(value)
                    for value in [mode, 'cookbook/dispatch.py', name]) + '] }'
            lines += ['run = [' + ('"mise install python", ' if name == 'setup' else '') + step + ']']
    return '\n'.join([*lines, END.rstrip('\n'), ''])


def decode(source):
    prefix, suffix = split(source, required=True)
    prefix_data(prefix)
    data = tomllib.loads(universal_text(source_text(source)))
    identity = data.get('vars')
    fields = {'cookbook_contract', 'cookbook_ref', 'cookbook_recipe', 'cookbook_sha256'}
    if not isinstance(identity, dict) or set(identity) != fields or type(identity.get('cookbook_contract')) is not int or identity['cookbook_contract'] != 1:
        raise ValueError('Mise source requires one closed versioned cookbook identity')
    tasks = data.get('tasks')
    if not isinstance(tasks, dict) or set(tasks) - (set(DESCRIPTIONS) | {'_sdk'}):
        raise ValueError('Mise recipe contains undeclared task implementations')
    suites = [suite for suite in SUITES if 'test:' + suite in tasks]
    capabilities = (['dev'] if 'dev' in tasks else []) + ['db:' + value for value in DATABASE if 'db:' + value in tasks]
    dispatcher = tasks.get('_sdk')
    if not isinstance(dispatcher, dict) or not isinstance(dispatcher.get('run'), str):
        raise ValueError('Mise recipe requires its canonical generic SDK dispatcher')
    expected = block(identity['cookbook_ref'], identity['cookbook_recipe'], identity['cookbook_sha256'],
                     suites, capabilities, bootstrap_run=dispatcher.get('run'))
    if universal_text(suffix) != expected:
        raise ValueError('Mise tasks differ from the canonical pinned recipe contract')
    return {'version': 1, 'repository': 'nicodes/cookbook', 'revision': identity['cookbook_ref'], 'recipe': identity['cookbook_recipe'],
            'configuration_sha256': identity['cookbook_sha256'], 'test_groups': sorted(suites),
            'capabilities': capabilities}


def declared(source):
    """Recognize partial task authority so a bridge cannot fall back to Make."""
    data = tomllib.loads(universal_text(source_text(source)))
    _, suffix = split(source)
    return suffix is not None or bool(set(data) - {'tools', 'env'})


def render(source, revision, recipe, configuration, suites, capabilities):
    prefix, _ = split(source)
    prefix_data(prefix)
    if not prefix.endswith(('\n', '\r')):
        raise ValueError('Mise SDK source must end with its existing newline')
    digest = hashlib.sha256(configuration).hexdigest()
    separator = '\r\n' if prefix.endswith('\r\n') else '\r' if prefix.endswith('\r') else '\n'
    return prefix + separator + block(revision, recipe, digest, suites, capabilities)


def matches(record, declaration, source, *, test_groups):
    fields = {'version', 'repository', 'revision', 'recipe', 'configuration_sha256', 'test_groups', 'capabilities'}
    if (not isinstance(record, dict) or set(record) != fields or type(record.get('version')) is not int
            or record['version'] != 1 or record.get('repository') != 'nicodes/cookbook'
            or not isinstance(record.get('revision'), str) or not re.fullmatch('[a-f0-9]{40}', record['revision'])):
        raise ValueError('Mise identity must retain its one closed Cookbook source contract')
    capabilities = (['dev'] if declaration.get('dev') else []) + [
        'db:' + name for name in DATABASE if name in declaration.get('database', {})]
    if (record['recipe'] != declaration['recipe'] or record['test_groups'] != sorted(test_groups)
            or record['configuration_sha256'] != hashlib.sha256(source).hexdigest()
            or record['capabilities'] != capabilities):
        raise ValueError('Mise identity differs from the exact native recipe declaration')
    return record


def discovery():
    import sdk_identity
    return sdk_identity


def read(root):
    root = Path(root)
    if (root / 'Makefile').exists() or (root / 'Makefile').is_symlink():
        raise ValueError('canonical Mise recipe must have one task authority')
    module = discovery()
    source = module.read_regular(root / 'cookbook.toml')
    from configuration import validate_data
    import orchestration
    declaration = validate_data(tomllib.loads(source.decode('utf-8')))
    model = orchestration.contract(declaration)
    return matches(decode(module.read_source(root)), declaration, source,
                   test_groups=model['test_groups'])
