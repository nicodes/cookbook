"""Pure closed recipe data contract shared by native tools and trusted mirrors."""
from copy import deepcopy
from pathlib import Path
import re

import recipe_contract

if (not isinstance(getattr(recipe_contract, '__file__', None), str)
        or Path(recipe_contract.__file__).resolve() != Path(__file__).with_name('recipe_contract.py').resolve()):
    raise ValueError('recipe policy module origin differs from its selected SDK')

RECIPES = {'rust-cli', 'react-native', 'godot-game', 'godot-addon'}
SUITES = {'lint', 'unit', 'integration', 'e2e', 'security'}
RUNNERS = {'python', 'python-unittest', 'node', 'node-test', 'bun', 'bun-test',
           'bash', 'godot', 'godot-suite', 'godot-check', 'playwright', 'godot-browser'}
TEST_FIELDS = {'runner', 'path', 'args', 'timeout', 'sentinel', 'cwd', 'pattern',
               'policy', 'python_environment', 'name'}
TEST_COMMON_FIELDS = {'runner', 'cwd', 'timeout', 'policy', 'python_environment'}
MAX_TEST_CASES = 256
RUST_FIELDS = {'name', 'manifest', 'binaries', 'packages', 'features', 'all_features',
    'no_default_features', 'workspace', 'all_targets', 'targets', 'target',
    'target_directory', 'profile', 'test', 'build', 'lint', 'environment',
    'toolchain_environment', 'build_std', 'cwd', 'timeout'}
RUST_SELECTORS = {'binaries', 'packages', 'features', 'all_features', 'no_default_features',
    'workspace', 'all_targets', 'release', 'profile', 'lib', 'tests', 'enabled',
    'test_targets', 'test_filter', 'test_arguments', 'native_host'}
NODE_FIELDS = {'directory', 'manager', 'build', 'typecheck', 'lint', 'test', 'format',
    'format_check', 'ignore_scripts', 'expo', 'test_arguments', 'test_paths', 'format_paths',
    'glob_compatibility'}
MISSING = object()

COMMON_FIELDS = {'version', 'recipe', 'tests', 'lint', 'clean', 'node', 'security',
                 'artifacts', 'ci', 'components'}
FAMILY_FIELDS = {
    'godot-addon': {'package', 'format', 'publication'},
    'rust-cli': {'package', 'publication'},
    'react-native': {'dev', 'database', 'delivery', 'preview', 'operations'},
    'godot-game': {'dev', 'database', 'delivery', 'preview', 'operations'},
}
COMPONENT_FIELDS = {
    'godot-addon': {'addon_dependencies', 'browser', 'fixture_exports', 'python'},
    'rust-cli': {'browser', 'build_checks', 'fixtures', 'native', 'native_tools',
                 'python', 'runtime', 'rust', 'rust_tests', 'ui'},
    'react-native': {'app', 'assets', 'image_security', 'workload_browser', 'development_database', 'fixture_images', 'empty_directories',
                     'images', 'image_bases', 'module_link', 'native', 'playwright', 'postgres_fixture',
                     'recovery_fixture', 'rust', 'rust_images', 'rust_tests'},
    'godot-game': {'asset_fixtures', 'assets', 'browser', 'fixture_images', 'go', 'godot',
                   'image_security', 'image_bases', 'images', 'playwright', 'rust'},
}
PACKAGE_FIELDS = {
    'godot-addon': {'archive', 'script_uids', 'embedded_manifest'},
    'rust-cli': {'binary', 'files', 'file_sources', 'version_arguments', 'version_template',
                 'version_fields', 'info_arguments', 'info_fields', 'info_exact',
                 'native_targets', 'extra_files', 'release_metadata'},
}
ARTIFACT_FIELDS = {
    'godot-addon': {'evidence_paths'},
    'rust-cli': {'evidence_paths'},
    'react-native': {'evidence_paths', 'fixture_paths', 'release_metadata'},
    'godot-game': {'paths', 'images', 'environment', 'revision_environment',
                   'release_metadata', 'evidence_paths', 'evidence_retention_days'},
}
LINT_FIELDS = {
    'godot-addon': {'required_paths'},
    'rust-cli': {'shell_paths'},
    'react-native': {'auth_policy', 'secret_policy', 'shell_paths'},
    'godot-game': set(),
}
SECURITY_FIELDS = {
    'godot-addon': {'fixtures'},
    'rust-cli': {'fixtures', 'deny_warnings', 'trivy_paths', 'rust_inventory_manifests'},
    'react-native': {'fixtures', 'go_images', 'spdx_images'},
    'godot-game': {'fixtures', 'auth_policy', 'rust_inventory_manifests', 'rust_inventory_sbom'},
}
APP_FIELDS = {'environment', 'preview_environment', 'preview_variables', 'fixture_exports',
              'native_exports', 'forbidden_environment', 'forbidden_markers', 'minimum_html',
              'maximum_html', 'public_target', 'replace_public', 'release_file',
              'image_source', 'installed_root'}
GODOT_FIELDS = {'exports', 'test_jobs', 'import_timeout', 'import_arguments', 'addon_taint_markers',
                'forbidden_diagnostics', 'art_sources', 'prepare', 'postprocess', 'packs',
                'web_releases', 'templates', 'rust_web', 'extensions', 'addon_bindings'}
NATIVE_FIELDS = {'runner', 'path', 'cwd', 'args', 'timeout', 'environment', 'phase', 'stages'}
IMAGE_FIELDS = {'name', 'dockerfile', 'context', 'target', 'cache_key', 'pull', 'build_arguments',
                'native'}
TABLE_FIELDS = {
    'components.playwright': {'packages', 'browsers'},
    'components.development_database': {'image', 'database', 'owner_role', 'password', 'port_environment', 'default_port', 'data_directory', 'command', 'memory_bytes', 'startup_timeout_seconds'},
    'components.recovery_fixture': {'timeout'},
    'components.postgres_fixture': {'image', 'database', 'owner_role', 'runtime_role', 'sql_paths',
        'url_environment', 'role_environment', 'password_environment', 'suites', 'startup_timeout'},
    'components.workload_browser': {'hostname'},
    'components.image_security': {'reports'},
    'components.fixture_images': {'directory'},
    'components.addon_dependencies': {'destination', 'mise_tool', 'sha256'},
    'components.runtime': {'manifest'},
    'components.godot.rust_web': set(),
    'ci.godot': {'unit_shards', 'unit_workers', 'build_timeout_minutes', 'unit_timeout_minutes',
        'host_timeout_minutes', 'unit_artifacts', 'gates', 'host_unit_name'},
    'lint.secret_policy': {'application', 'workflows', 'scoped_env_dir', 'host_only', 'secrets_env_services'},
}
RECORD_FIELDS = {
    'components.empty_directories': {'path', 'mode'},
    'components.go': {'directory', 'package', 'output', 'test_packages'},
    'components.fixture_exports': {'project', 'preset', 'output', 'install_archive', 'required_outputs',
        'timeout', 'import_timeout'},
    'components.app.fixture_exports': {'output', 'environment', 'timeout'},
    'components.app.native_exports': {'platforms', 'output', 'timeout'},
    'components.build_checks': {'runner', 'path', 'args', 'timeout', 'cwd'},
    'components.fixtures': {'binary', 'manifest', 'files', 'repository', 'source_line', 'native_targets',
        'version_arguments', 'version_first_lines'},
    'components.ui': {'required_files'},
    'components.browser.journeys': {'name', 'path', 'variant'},
    'components.godot.exports': {'preset', 'output', 'kind', 'platform', 'runtime_compatibility',
        'timeout', 'import_timeout', 'import_arguments', 'privileged_directories', 'source_files'},
    'components.godot.extensions': {'source', 'destination', 'format', 'profile', 'record', 'target'},
    'package.native_targets': {'platform', 'arch', 'runner', 'target', 'extension', 'checks',
        'timeout_minutes', 'second_wave_timeout_minutes'},
}


def closed(value, fields, label):
    if not isinstance(value, dict) or set(value) - fields:
        raise ValueError(label + ' requires a closed literal table')
    return value


def nested(data, name):
    value = data
    for part in name.split('.'):
        if not isinstance(value, dict) or part not in value:
            return MISSING
        value = value[part]
    return value


def records(value, fields, label):
    if not isinstance(value, list):
        raise ValueError(label + ' requires literal records')
    for entry in value:
        closed(entry, fields, label)


def validate_ci_data(data):
    values = closed(data.get('ci', {}), {'runner', 'timeout_minutes', 'godot',
        'preview_environment', 'dependency_security'}, 'CI settings')
    if 'godot' in values and (data.get('recipe') != 'godot-game' or not isinstance(values['godot'], dict)):
        raise ValueError('Godot CI metadata belongs to its application recipe')
    preview = values.get('preview_environment', '')
    if not isinstance(preview, str) or (preview and (data.get('recipe') != 'react-native' or preview != 'Preview')):
        raise ValueError('only the reviewed React Native Preview environment may bind candidate builds')
    result = {'runner': 'ubuntu-24.04', 'timeout_minutes': 30,
        **{key: value for key, value in values.items()
           if key not in {'godot', 'preview_environment', 'dependency_security'}}}
    if preview:
        result['preview_environment'] = preview
    if (not isinstance(result['runner'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', result['runner'])
            or type(result['timeout_minutes']) is not int or not 1 <= result['timeout_minutes'] <= 180):
        raise ValueError('CI runner and job budget must be bounded source data')
    if 'dependency_security' in values:
        policy = closed(values['dependency_security'], {'runner', 'timeout_minutes', 'setup_timeout_minutes'},
                        'dependency security')
        if (set(policy) != {'runner', 'timeout_minutes', 'setup_timeout_minutes'}
                or not isinstance(policy['runner'], str) or policy['runner'] not in {'ubuntu-22.04', 'ubuntu-24.04',
                    'blacksmith-4vcpu-ubuntu-2204', 'blacksmith-4vcpu-ubuntu-2404'}
                or type(policy['timeout_minutes']) is not int or not 1 <= policy['timeout_minutes'] <= 180
                or type(policy['setup_timeout_minutes']) is not int
                or not 1 <= policy['setup_timeout_minutes'] <= policy['timeout_minutes']):
            raise ValueError('dependency security requires its exact native runner and bounded setup/job budgets')
    return result


def node_package(package):
    closed(package, NODE_FIELDS, 'Node package')
    recipe_contract._path(package.get('directory'), 'Node package directory')
    if not isinstance(package.get('manager', 'npm'), str) or package.get('manager', 'npm') not in {'npm', 'bun'}:
        raise ValueError('Node packages require the declared npm or Bun manager')
    for name in ['build', 'typecheck', 'lint', 'test', 'format', 'format_check']:
        if name in package and (not isinstance(package[name], str)
                or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.:-]*', package[name])):
            raise ValueError('Node operations must select native package script names')
    for name in ['expo', 'ignore_scripts', 'glob_compatibility']:
        if name in package and type(package[name]) is not bool:
            raise ValueError('Node capability switches must be booleans')
    for name in ['test_arguments', 'test_paths', 'format_paths']:
        values = package.get(name, [])
        if not isinstance(values, list) or any(not isinstance(value, str) or '\0' in value for value in values):
            raise ValueError('Node native arguments and paths must be literal lists')
        if name != 'test_arguments':
            for value in values:
                recipe_contract._path(value, 'Node native source path')
    if package.get('test_paths') and (package.get('manager', 'npm') != 'bun' or package.get('test')):
        raise ValueError('direct native test paths require Bun without a competing test script')
    return package


def validate_data(data):
    """Validate literal recipe semantics without reading or executing source files."""
    if (not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1
            or not isinstance(data.get('recipe'), str) or data['recipe'] not in RECIPES):
        raise ValueError('unsupported cookbook configuration')
    family = data['recipe']
    closed(data, COMMON_FIELDS | FAMILY_FIELDS[family], 'recipe')
    data = deepcopy(data)
    for name, fields in [('components', COMPONENT_FIELDS[family]), ('artifacts', ARTIFACT_FIELDS[family]),
            ('lint', LINT_FIELDS[family]), ('security', SECURITY_FIELDS[family]),
            ('clean', {'paths'}), ('format', {'paths'}), ('node', {'packages'}),
            ('database', {'status', 'migrate', 'seed', 'reset'}), ('delivery', {'definition'}),
            ('preview', {'definition'}), ('operations', {'definition'})]:
        if name in data:
            closed(data[name], fields, name)
    if 'package' in data:
        closed(data['package'], PACKAGE_FIELDS[family], 'package')
    if 'dev' in data:
        fields = ({'kind', 'identity', 'project_prefix', 'directory', 'port', 'port_attempts',
                   'program', 'cleanup'} if family == 'react-native' else
                  {'kind', 'identity', 'project_prefix', 'program', 'cleanup',
                   'capture_output', 'mode', 'state', 'log'})
        closed(data['dev'], fields, 'development')
    if 'publication' in data:
        fields = ({'mode', 'tag_prefix', 'initial', 'resume', 'retry', 'assets', 'environment',
                   'digests', 'registry_auth', 'provenance', 'evidence_assets', 'validation_timeout',
                   'publish_timeout', 'recovery_timeout'} if family == 'godot-addon' else
                  {'tag_prefix', 'initial', 'initial_version', 'version_identity', 'cargo_manifest',
                   'acceptance_timeout', 'validation_timeout', 'acceptance', 'attestation_platforms'})
        closed(data['publication'], fields, 'publication')
    components = data.get('components', {})
    if 'app' in components:
        closed(components['app'], APP_FIELDS, 'application component')
    if 'godot' in components:
        closed(components['godot'], GODOT_FIELDS, 'Godot component')
    if 'asset_fixtures' in components:
        records(components['asset_fixtures'], {'name', 'source', 'prepare'}, 'static fixture variants')
        for entry in components['asset_fixtures']:
            if 'prepare' in entry:
                records(entry['prepare'], NATIVE_FIELDS, 'native static fixture preparation')
    if 'assets' in components:
        records(components['assets'], {'name', 'source', 'max_bytes', 'max_file_bytes', 'max_files', 'serving_metadata'}, 'static assets')
    if 'image_bases' in components:
        records(components['image_bases'], {'component', 'reference'}, 'unchanged image bases')
    browser = {'godot-addon': {'package', 'cli_version'}, 'rust-cli': {'package'},
        'godot-game': {'package', 'runner', 'startup_runner', 'cache_runner', 'tls_proxy',
            'hostname', 'reports', 'cli_tool', 'browsers', 'journeys', 'forward_environment'}}
    if 'browser' in components:
        closed(components['browser'], browser[family], 'browser component')
    for name, fields in TABLE_FIELDS.items():
        value = nested(data, name)
        if value is not MISSING:
            closed(value, fields, name)
    for name, fields in RECORD_FIELDS.items():
        value = nested(data, name)
        if value is not MISSING:
            records(value, fields, name)
    if 'images' in components:
        records(components['images'], IMAGE_FIELDS, 'image components')
        for entry in components['images']:
            if 'native' in entry:
                records(entry['native'], NATIVE_FIELDS, 'native image preparation')
    if 'native' in components:
        fields = ({'name', 'role', 'component', 'binary', 'output', 'needs', 'environment', 'checks'}
                  if family == 'rust-cli' else NATIVE_FIELDS)
        records(components['native'], fields, 'native components')
    if 'python' in components:
        values = components['python']
        if not isinstance(values, list):
            raise ValueError('Python components require literal records')
        for entry in values:
            if not isinstance(entry, dict) or set(entry) not in ({'requirements', 'target'}, {'requirements', 'venv'}):
                raise ValueError('Python components require requirements and one local environment')
    if 'rust' in components:
        if not isinstance(components['rust'], list):
            raise ValueError('Cargo components require literal records')
        for entry in components['rust']:
            closed(entry, RUST_FIELDS | {'output_directory', 'output_names'}, 'Cargo component')
            for operation in ['build', 'lint', 'test']:
                if operation in entry:
                    fields = RUST_SELECTORS - {'release'} if operation == 'build' else RUST_SELECTORS
                    closed(entry[operation], fields, 'Cargo ' + operation + ' selectors')
    if 'rust_tests' in components:
        fields = ({'manifest', 'selectors', 'suite'} if family == 'react-native' else
            {'name', 'component', 'manifest', 'selectors', 'roles', 'platforms', 'suite',
             'dependency_context', 'reuse_native_build'})
        records(components['rust_tests'], fields, 'Cargo test selections')
        for entry in components['rust_tests']:
            if 'selectors' in entry:
                closed(entry['selectors'], RUST_SELECTORS, 'Cargo test selectors')
    if 'rust_images' in components:
        value = components['rust_images']
        if not isinstance(value, dict):
            raise ValueError('Rust image contracts require literal named records')
        for entry in value.values():
            closed(entry, {'user', 'lock_source', 'lock_installed', 'binaries', 'forbidden_markers'}, 'Rust image contract')
    if 'packages' in data.get('node', {}):
        values = data['node']['packages']
        if not isinstance(values, list):
            raise ValueError('Node packages require literal records')
        for value in values:
            node_package(value)
    validate_ci_data(data)
    if 'tests' in data:
        data['tests'] = expand_tests(data['tests'])
    for entries in data.get('tests', {}).values():
        for entry in entries:
            validate_test_data(entry, family)
    recipe_contract.validate_paths(data)
    return data


def validate_test_data(entry, family):
    closed(entry, TEST_FIELDS, 'native test entry')
    recipe_contract._path(entry.get('path'), 'native test source')
    for name in ['cwd', 'python_environment']:
        if name in entry:
            recipe_contract._path(entry[name], 'native test ' + name)
    if 'name' in entry and (not isinstance(entry['name'], str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', entry['name'])):
        raise ValueError('native test coverage names must be bounded literal labels')
    if not isinstance(entry.get('runner'), str) or entry['runner'] not in RUNNERS:
        raise ValueError('unsupported native test runner')
    policy = entry.get('policy', {})
    policy_keys = {'sentinel_regex', 'assertions_regex', 'allow_errors', 'editor', 'audio_driver',
                   'allow_error_version', 'deferred_autoloads'}
    if (not isinstance(policy, dict) or set(policy)-policy_keys
            or (policy and entry['runner'] not in {'godot', 'godot-check'})):
        raise ValueError('native assertion policy is unsupported for this runner')
    if 'deferred_autoloads' in policy and (family != 'godot-game'
            or entry['runner'] != 'godot-check' or type(policy['deferred_autoloads']) is not bool):
        raise ValueError('deferred autoloads require a native Godot game check and literal boolean')
    if entry['runner'] == 'godot-check' and set(policy) - {'deferred_autoloads'}:
        raise ValueError('native Godot checks retain their assertion policy')
    for name in ['sentinel_regex', 'assertions_regex', 'allow_errors']:
        expressions = policy.get(name, []) if name == 'allow_errors' else [policy.get(name)]
        if name == 'allow_errors' and (not isinstance(expressions, list) or len(expressions) > 16):
            raise ValueError('engine diagnostic allowances must be bounded literal expressions')
        for expression in expressions:
            if expression is None and name != 'allow_errors':
                continue
            if (not isinstance(expression, str) or len(expression) > 1024
                    or not expression.startswith('^') or not expression.endswith('$')):
                raise ValueError('assertion policies require bounded anchored expressions')
            try:
                compiled = re.compile(expression)
            except re.error as error:
                raise ValueError('invalid native assertion expression') from error
            if compiled.groups > 1 or (name == 'assertions_regex' and compiled.groups != 1):
                raise ValueError('assertion expressions may capture one positive assertion count')
    if 'editor' in policy and type(policy['editor']) is not bool:
        raise ValueError('native Godot editor policy must be boolean')
    if 'audio_driver' in policy and policy['audio_driver'] != 'Dummy':
        raise ValueError('native Godot assertion audio driver must retain Dummy')
    if 'allow_error_version' in policy and (not isinstance(policy['allow_error_version'], str)
            or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', policy['allow_error_version'])):
        raise ValueError('engine diagnostic allowances require an exact native version')
    if policy.get('allow_errors') and (policy.get('editor') is not True or not policy.get('allow_error_version')):
        raise ValueError('engine diagnostic allowances require their exact editor/version boundary')
    if entry['runner'] in {'godot', 'godot-suite'}:
        sentinel = entry.get('sentinel')
        regex_signal = entry['runner'] == 'godot' and isinstance(policy.get('sentinel_regex'), str)
        if not regex_signal and (not isinstance(sentinel, str) or not sentinel or '\n' in sentinel or '\r' in sentinel):
            raise ValueError('Godot tests require one exact assertion sentinel')
    arguments = entry.get('args', [])
    if not isinstance(arguments, list) or any(not isinstance(value, str) or '\0' in value for value in arguments):
        raise ValueError('native test arguments must be literal strings')
    if entry['runner'] == 'godot-suite' and arguments:
        raise ValueError('Godot suite discovery does not accept script arguments')
    if 'pattern' in entry:
        pattern = entry['pattern']
        if (entry['runner'] not in {'python-unittest', 'node-test'} or not isinstance(pattern, str)
                or not pattern or '/' in pattern or '\\' in pattern or '\0' in pattern):
            raise ValueError('native test discovery requires one filename pattern')
    if type(entry.get('timeout', 300)) is not int or not 1 <= entry.get('timeout', 300) <= 10800:
        raise ValueError('native tests require a bounded timeout')
    if 'python_environment' in entry and entry['runner'] not in {'python', 'python-unittest'}:
        raise ValueError('Python environments apply only to native Python test runners')


def expand_tests(tests):
    """Expand literal case groups without changing native entries or their order."""
    if not isinstance(tests, dict) or set(tests) - SUITES:
        raise ValueError('unknown test suite')
    result = {}
    for suite, entries in tests.items():
        if not isinstance(entries, list) or not entries:
            raise ValueError('declared test groups must contain real checks')
        expanded = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError('invalid native test entry')
            if 'cases' not in entry:
                expanded.append(entry)
                continue
            if set(entry) - TEST_COMMON_FIELDS - {'cases'}:
                raise ValueError('test case groups may share only native execution settings')
            cases = entry['cases']
            if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_TEST_CASES:
                raise ValueError('test case groups require between one and 256 literal cases')
            common = {key: value for key, value in entry.items() if key != 'cases'}
            for case in cases:
                if not isinstance(case, dict) or set(case) - TEST_FIELDS:
                    raise ValueError('test cases require ordinary native entry fields without nesting')
                if set(common) & set(case):
                    raise ValueError('test cases may not override shared execution settings')
                expanded.append({**deepcopy(common), **case})
        result[suite] = expanded
    return result
