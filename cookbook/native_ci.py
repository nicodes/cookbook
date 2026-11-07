#!/usr/bin/env python3
"""Run declared native matrices and join partial proofs with complete host validation."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import ci
import sdk_environment

from configuration import load, relative
import native_components
import recipe
import rust_tests

import dependencies

COOKBOOK = Path(__file__).resolve().parents[1]
TOOLS = dependencies.tools_root()
MAX_FILES = 4096
MAX_FILE = 128 << 20
SHA256 = re.compile(r'[a-f0-9]{64}\Z')
HOSTED = {
    ('Linux', 'x86_64'): {'ubuntu-22.04', 'ubuntu-24.04'},
    ('Linux', 'arm64'): {'ubuntu-24.04-arm'},
    ('Darwin', 'x86_64'): {'macos-15-intel'},
    ('Darwin', 'arm64'): {'macos-15', 'macos-26'},
    ('Windows', 'x86_64'): {'windows-2025'},
    ('Windows', 'arm64'): {'windows-11-arm'},
}
TARGETS = {
    ('Linux', 'x86_64'): {'x86_64-unknown-linux-musl', 'x86_64-unknown-linux-gnu'},
    ('Linux', 'arm64'): {'aarch64-unknown-linux-musl', 'aarch64-unknown-linux-gnu'},
    ('Darwin', 'x86_64'): {'x86_64-apple-darwin'},
    ('Darwin', 'arm64'): {'aarch64-apple-darwin'},
    ('Windows', 'x86_64'): {'x86_64-pc-windows-msvc'},
    ('Windows', 'arm64'): {'aarch64-pc-windows-msvc'},
}

CONTEXT = ('GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_SHA',
           'GITHUB_WORKFLOW_REF', 'GITHUB_WORKFLOW_SHA', 'GITHUB_EVENT_NAME')


def helper(name):
    return dependencies.recipe_helper(name)


def slug(kind, name):
    return ('native-cargo-' if kind == 'cargo' else 'native-') + hashlib.sha256(name.encode()).hexdigest()[:24]


def phase_names(config):
    values = native_components.records(config) if config.get('components', {}).get('native') else []
    second = {value['name'] for value in values if any(item.get('platform', 'native') != 'native' for item in value.get('needs', []))}
    while True:
        previous = set(second)
        second.update(value['name'] for value in values if any(item['component'] in second for item in value.get('needs', [])))
        if second == previous:
            break
    return ([value['name'] for value in values if value['name'] not in second],
            [value['name'] for value in values if value['name'] in second])


def plan(root, config):
    targets = config.get('package', {}).get('native_targets', [])
    if not targets:
        return {'version': 1, 'requirements': [], 'wave1': {'include': []}, 'wave2': {'include': []}}
    if config['recipe'] != 'rust-cli' or not isinstance(targets, list) or len(targets) > 16:
        raise ValueError('this native matrix requires explicit Rust CLI target records')
    integration = {value['path'] for value in config.get('tests', {}).get('integration', [])}
    first, second = phase_names(config)
    graph = bool(config.get('components', {}).get('native'))
    requirements = []
    seen = set()
    for target in targets:
        system, arch = target.get('platform'), target.get('arch')
        if target.get('runner') not in HOSTED.get((system, arch), set()):
            raise ValueError('native matrices require reviewed hosted OS/architecture runners')
        if target.get('target') not in TARGETS.get((system, arch), set()):
            raise ValueError('native matrix requires a literal Rust target')
        if target.get('extension') != ('zip' if system == 'Windows' else 'tar.gz'):
            raise ValueError('native archive format must match the declared platform')
        if (system, arch) in seen:
            raise ValueError('native matrix repeats a platform/architecture')
        seen.add((system, arch))
        for wave, names in [(1, first), (2, second)]:
            if wave == 2 and not second:
                continue
            if graph and not names:
                raise ValueError('a native dependency graph needs a real first wave')
            environment = {'COOKBOOK_NATIVE_COMPONENTS': json.dumps(names)} if graph else {}
            selected = native_components.selection(config, environment) if graph else []
            checks = sorted({path for value in selected for path in value.get('checks', [])}) if graph else target.get('checks', [])
            if not isinstance(checks, list) or len(checks) != len(set(checks)) or not set(checks) <= integration:
                raise ValueError('native boundary selectors must name declared integration tests')
            cargo = [value['name'] for value in rust_tests.eligible(root, config, environment, system + '/' + arch)]
            builds = [value['name'] for value in rust_tests.eligible(root, config, environment, system + '/' + arch, 'build')]
            groups = [] if any(value.get('suite', 'unit') == 'unit' for value in config.get('components', {}).get('rust_tests', [])) else ['unit']
            budget = target.get('second_wave_timeout_minutes', target.get('timeout_minutes', 30)) if wave == 2 else target.get('timeout_minutes', 30)
            if type(budget) is not int or not 1 <= budget <= 120:
                raise ValueError('native matrix timeouts must be bounded source data')
            requirements.append({'id': f'{wave}-{system}-{arch}', 'wave': wave, 'runner': target['runner'],
                'platform': system, 'arch': arch, 'target': target['target'], 'extension': target['extension'], 'components': names,
                'groups': groups, 'cargo': cargo, 'checks': checks, 'build_checks': builds,
                'job': f'Native {wave} ({system}-{arch})', 'timeout_minutes': budget})
    result = {'version': 1, 'requirements': requirements, 'host_groups': recipe.test_suites(root, config),
            'wave1': {'include': [value for value in requirements if value['wave'] == 1]},
            'wave2': {'include': [value for value in requirements if value['wave'] == 2]}}
    if len(json.dumps(result)) > 200000:
        raise ValueError('native matrix exceeds bounded Actions output size')
    return result


def read_json(path, maximum=2 << 20):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError('native proof must be a bounded regular JSON file')
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError('native proof JSON must be an object')
    return value


def digest(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_FILE:
        raise ValueError('native evidence payload must be a nonempty bounded regular file')
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def configuration_hash(root):
    source = subprocess.check_output(['git', '-C', str(root), 'show', 'HEAD:cookbook.toml'], timeout=30)
    return hashlib.sha256(source).hexdigest()


def context_matches(value, expected, *, job=None):
    if not isinstance(value, dict) or any(not expected.get(key) or value.get(key) != expected[key] for key in CONTEXT):
        raise ValueError('native proof belongs to another Actions execution')
    if job is not None and value.get('GITHUB_JOB') != job:
        raise ValueError('native proof belongs to another matrix wave')


def expected_receipts(entry):
    return [*entry['groups'], *[slug('cargo', name) for name in entry['cargo']],
            *[slug('boundary', name) for name in entry['checks']]]


def build_payloads(root, config, entry):
    package = recipe.adapter(config).package_data(root, config)
    graph = config.get('components', {}).get('native')
    selected = [value for value in native_components.records(config) if value['name'] in entry['components']] if graph else []
    required = set()
    if graph:
        for value in selected:
            path = native_components.output(root, value, entry['platform'] + '/' + entry['arch'])
            required.update([path.relative_to(root).as_posix(), path.with_name(path.name + '.json').relative_to(root).as_posix()])
    if not graph or any(value['role'] == 'cli' for value in selected):
        native = helper('native_cli')
        archive = native.archive_name(package, entry)
        output = package['output'] + ('.exe' if entry['platform'] == 'Windows' and not package['output'].endswith('.exe') else '')
        required.update(['dist/' + archive, 'dist/checksums.txt', output,
                         '.artifacts/native-package/' + archive, '.artifacts/native-package/' + archive + '.json'])
        required.update('dist/' + name for name in package.get('extra_files', []))
        if package.get('release_metadata'):
            required.add('dist/release.json')
    scopes = recipe.adapter(config).base_artifact_paths(root, config) if not graph or any(value['role'] == 'cli' for value in selected) else []
    return required, [*scopes, *required]


def collect(root, config, source, tools, entry):
    tree = ci.identity(root, source, tools)
    verify = ci.verification()
    scope = recipe.adapter(config).verification_scope(root, config)
    build = read_json(root / '.artifacts/verification/build.json')
    execution = verify.execution_context()
    if (build.get('identity') != verify.identity(scope) or build.get('artifacts') != verify.artifacts(scope)):
        raise ValueError('native source or build outputs changed after verification')
    records = {name: read_json(root / '.artifacts/verification' / (name + '.json')) for name in expected_receipts(entry)}
    if any(value.get('tested_commit') != source for value in records.values()):
        raise ValueError('native test receipts must prove actually tested source')
    for value in [build.get('provenance', {}), *records.values()]:
        if (value.get('source_commit') != source or value.get('source_tree') != tree
                or value.get('tracked_source_clean') is not True or value.get('input_identity') != build['identity']
                or value.get('execution_context') != execution):
            raise ValueError('native checks did not accept the same source, outputs and job execution')
    return {'version': 1, 'coverage': 'native-partial', 'complete_host': False,
            'repository': os.environ.get('GITHUB_REPOSITORY'), 'source_commit': source, 'source_tree': tree,
            **dependencies.binding(tools), 'configuration_sha256': configuration_hash(root),
            'entry': entry, 'execution_context': execution, 'status': 'passed',
            'version_identity': os.environ.get('VERSION', 'dev'), 'date_identity': os.environ.get('DATE', 'unknown'),
            'build': build, 'tests': records}


def export(root, proof):
    stage = relative(root, '.artifacts/native-export')
    helper('clean').clean(root, ['.artifacts/native-export'])
    stage.mkdir(parents=True, mode=0o700)
    artifacts = proof['build']['artifacts']
    if not artifacts or len(artifacts) > MAX_FILES:
        raise ValueError('native proof requires bounded actual build outputs')
    if sum(relative(root, name, exists=True).stat().st_size for name in artifacts) > 2 << 30:
        raise ValueError('native transport exceeds its bounded output budget')
    for name, expected in artifacts.items():
        path = relative(root, name, exists=True)
        if digest(path) != expected:
            raise ValueError('native output changed before transport')
        destination = relative(stage / 'payload', name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    (stage / 'native-proof.json').write_text(json.dumps(proof, indent=2) + '\n')
    return stage


def packet(root, directory, config, source, tools, entry, environment):
    directory = Path(directory)
    proof = read_json(directory / 'native-proof.json')
    dependencies.validate_binding(proof, tools)
    if (proof.get('version') != 1 or proof.get('coverage') != 'native-partial'
            or proof.get('complete_host') is not False or proof.get('status') != 'passed'
            or proof.get('repository') != environment.get('GITHUB_REPOSITORY')
            or proof.get('source_commit') != source or proof.get('source_tree') != ci.git(root, 'rev-parse', 'HEAD^{tree}')
            or proof.get('cookbook_commit') != tools or proof.get('configuration_sha256') != configuration_hash(root)
            or proof.get('entry') != entry):
        raise ValueError('native packet differs from the exact declared source, tools or coverage')
    context_matches(proof.get('execution_context', {}), environment, job='native_one' if entry['wave'] == 1 else 'native_two')
    build = proof.get('build', {})
    if not isinstance(build, dict) or not isinstance(build.get('identity'), str) or not SHA256.fullmatch(build['identity']):
        raise ValueError('native packet requires its exact input identity')
    provenance = build.get('provenance', {})
    receipts = proof.get('tests', {})
    if not isinstance(build, dict) or not isinstance(provenance, dict) or not isinstance(receipts, dict):
        raise ValueError('native packet receipts must be explicit objects')
    if any(not isinstance(value, dict) or value.get('tested_commit') != source for value in receipts.values()):
        raise ValueError('native test receipts did not test this source commit')
    if set(receipts) != set(expected_receipts(entry)):
        raise ValueError('native packet omitted or invented declared test coverage')
    for value in [provenance, *receipts.values()]:
        if (not isinstance(value, dict) or value.get('source_commit') != source or value.get('source_tree') != proof['source_tree']
                or value.get('tracked_source_clean') is not True or value.get('input_identity') != build.get('identity')
                or value.get('execution_context') != proof['execution_context']):
            raise ValueError('native receipt belongs to another source or test execution')
    artifacts = build.get('artifacts', {})
    required, allowed = build_payloads(root, config, entry)
    if not isinstance(artifacts, dict) or not artifacts or len(artifacts) > MAX_FILES or not required <= set(artifacts):
        raise ValueError('native packet omitted required executable/archive/source receipts')
    for name, expected in artifacts.items():
        if (not isinstance(name, str) or not isinstance(expected, str) or not SHA256.fullmatch(expected)
                or not any(name == scope or name.startswith(scope.rstrip('/') + '/') for scope in allowed)):
            raise ValueError('native packet contains undeclared output paths or hashes')
        if digest(relative(directory / 'payload', name, exists=True)) != expected:
            raise ValueError('native packet payload differs from its accepted build hash')
    actual = set()
    for path in directory.rglob('*'):
        if path.is_symlink():
            raise ValueError('native packets may not contain symlinks')
        if path.is_file():
            actual.add(path.relative_to(directory).as_posix())
    if actual != {'native-proof.json', *['payload/' + name for name in artifacts]}:
        raise ValueError('native packet transport has unexpected or missing files')
    return proof


def github(repository, suffix):
    return json.loads(subprocess.check_output(['gh', 'api', 'repos/' + repository + suffix], text=True, timeout=30))


def run_identity(environment):
    repository = environment.get('GITHUB_REPOSITORY', '')
    run = environment.get('GITHUB_RUN_ID', '')
    attempt = environment.get('GITHUB_RUN_ATTEMPT', '')
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)
            or not re.fullmatch(r'[1-9][0-9]{0,23}', run) or not re.fullmatch(r'[1-9][0-9]{0,8}', attempt)):
        raise ValueError('native orchestration requires the current repository/run/attempt')
    return repository, run, attempt


def artifact_name(source, entry, environment):
    _repository, run, attempt = run_identity(environment)
    return f'cookbook-native-{source}-{entry["id"]}-{run}-{attempt}'


def download(root, source, environment, wave=None):
    repository, run, attempt = run_identity(environment)
    directory = relative(root, '.artifacts/native-downloads')
    helper('clean').clean(root, ['.artifacts/native-downloads'])
    directory.mkdir(parents=True, mode=0o700)
    subprocess.run(['gh', 'run', 'download', run, '--repo', repository, '--pattern',
                    f'cookbook-native-{source}-{str(wave) + "-" if wave is not None else ""}*-{run}-{attempt}', '--dir', str(directory)],
                   check=True, timeout=300)
    return directory


def validate_jobs(requirements, jobs):
    if jobs.get('total_count') != len(jobs.get('jobs', [])):
        raise ValueError('complete same-attempt Actions job records are required')
    accepted = []
    for entry in requirements:
        matches = [value for value in jobs['jobs'] if value.get('name', '').endswith(entry['job'])]
        if (len(matches) != 1 or matches[0].get('status') != 'completed'
                or matches[0].get('conclusion') != 'success' or entry['runner'] not in matches[0].get('labels', [])):
            raise ValueError('every declared native matrix job must actually succeed on its declared runner')
        accepted.append({'entry': entry['id'], 'id': matches[0]['id'], 'name': matches[0]['name']})
    return accepted


def import_packets(root, config, source, tools, requirements, directory, environment):
    expected_names = {artifact_name(source, entry, environment) for entry in requirements}
    actual = {path.name for path in Path(directory).iterdir()}
    if actual != expected_names:
        raise ValueError('native artifact matrix is incomplete, stale or contains unexpected jobs')
    proofs = []
    graph = {value['name']: value for value in native_components.records(config)} if config.get('components', {}).get('native') else {}
    for entry in requirements:
        source_directory = Path(directory) / artifact_name(source, entry, environment)
        proof = packet(root, source_directory, config, source, tools, entry, environment)
        proofs.append(proof)
        for name in proof['build']['artifacts']:
            destination = None
            if name.startswith('.artifacts/native-package/'):
                destination = root / '.artifacts/native-archives' / Path(name).name
            elif graph:
                allowed = {native_components.output(root, value, entry['platform'] + '/' + entry['arch']).relative_to(root).as_posix()
                           for value in graph.values() if value['name'] in entry['components']}
                if name in allowed or name.removesuffix('.json') in allowed:
                    destination = relative(root, name)
            if destination is not None:
                incoming = relative(source_directory / 'payload', name, exists=True)
                if destination.exists() and digest(destination) != digest(incoming):
                    raise ValueError('native artifact imports disagree about an immutable output')
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(incoming, destination)
                if not destination.name.endswith('.json'):
                    destination.chmod(0o755)
    return proofs


def run_entry(root, source, tools, entry_id):
    config = load(root)
    ci.identity(root, source, tools)
    declaration = plan(root, config)
    matches = [value for value in declaration['requirements'] if value['id'] == entry_id]
    if len(matches) != 1:
        raise ValueError('select one exact declared native matrix entry')
    entry = matches[0]
    if helper('native_cli').host() != (entry['platform'], entry['arch']):
        raise ValueError('native runner host differs from the declared platform/architecture')
    os.environ['RUST_TARGET'] = entry['target']
    os.environ['COOKBOOK_STAGE'] = 'native'
    os.environ['COOKBOOK_ROOT'] = str(COOKBOOK)
    if entry['components']:
        os.environ['COOKBOOK_NATIVE_COMPONENTS'] = json.dumps(entry['components'])
    else:
        os.environ.pop('COOKBOOK_NATIVE_COMPONENTS', None)
    for name in ['COOKBOOK_TEST_PATHS', 'COOKBOOK_NATIVE_TESTS', 'NATIVE_CLI_ARCHIVES', 'COOKBOOK_NATIVE_BUILD_CHECKS']:
        os.environ.pop(name, None)
    recipe.configure_environment(root, config)
    if entry['wave'] == 2:
        first = [value for value in declaration['requirements'] if value['wave'] == 1]
        directory = download(root, source, os.environ, wave=1)
        import_packets(root, config, source, tools, first, directory, os.environ)
        helper('clean').clean(root, ['.artifacts/native-downloads'])
    for command in ['setup', 'build']:
        subprocess.run(sdk_environment.python_entry(COOKBOOK / 'cookbook/dispatch.py', command), cwd=root, env=os.environ, check=True)
    if entry['groups']:
        subprocess.run(sdk_environment.python_entry(COOKBOOK / 'cookbook/dispatch.py', 'test:unit'), cwd=root, env=os.environ, check=True)
    if entry['cargo']:
        subprocess.run(sdk_environment.python_entry(COOKBOOK / 'cookbook/dispatch.py', 'test:unit'), cwd=root,
                       env={**os.environ, 'COOKBOOK_NATIVE_TESTS': json.dumps(entry['cargo'])}, check=True)
    if entry['checks']:
        subprocess.run(sdk_environment.python_entry(COOKBOOK / 'cookbook/dispatch.py', 'test:integration'), cwd=root,
                       env={**os.environ, 'COOKBOOK_TEST_PATHS': json.dumps(entry['checks'])}, check=True)
    proof = collect(root, config, source, tools, entry)
    export(root, proof)


def prepare_host(root, config, source, tools, *, directory=None, jobs=None):
    declaration = plan(root, config)
    if not declaration['requirements']:
        return None
    environment = dict(os.environ)
    downloaded = directory is None
    directory = Path(directory) if directory is not None else download(root, source, environment)
    proofs = import_packets(root, config, source, tools, declaration['requirements'], directory, environment)
    versions = {(value['version_identity'], value['date_identity']) for value in proofs}
    if len(versions) != 1:
        raise ValueError('native builders disagree about release version/date inputs')
    version, date = versions.pop()
    if (not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}', version)
            or not isinstance(date, str) or len(date) > 128 or any(ord(character) < 32 for character in date)
            or ('VERSION' in environment and environment['VERSION'] != version)
            or ('DATE' in environment and environment['DATE'] != date)):
        raise ValueError('native artifact build inputs differ from the requested host gate')
    repository, run, attempt = run_identity(environment)
    jobs = jobs if jobs is not None else github(repository, f'/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100')
    accepted_jobs = validate_jobs(declaration['requirements'], jobs)
    if downloaded:
        helper('clean').clean(root, ['.artifacts/native-downloads'])
    os.environ.update(VERSION=version, DATE=date, NATIVE_CLI_ARCHIVES=str(root / '.artifacts/native-archives'))
    os.environ['COOKBOOK_NATIVE_BUILD_CHECKS'] = json.dumps(sorted({name for row in declaration['requirements'] for name in row['build_checks']}))
    os.environ.pop('COOKBOOK_NATIVE_COMPONENTS', None)
    os.environ.pop('COOKBOOK_NATIVE_TESTS', None)
    os.environ.pop('COOKBOOK_TEST_PATHS', None)
    return {'version': 1, 'requirements': declaration['requirements'], 'host_groups': declaration['host_groups'], 'jobs': accepted_jobs,
            'source_commit': source, **dependencies.binding(tools), 'proofs': proofs}


def finalize_host(host, matrix):
    if matrix is None:
        return host
    dependencies.validate_record(host)
    dependencies.validate_record(matrix)
    if not isinstance(matrix, dict) or any(host.get(key) != matrix.get(key) for key in dependencies.BINDING_KEYS):
        raise ValueError('native host and matrix must retain the same separate SDK source identities')
    if (host.get('status') != 'passed' or host.get('build_verified') is not True or host.get('tests_verified') is not True
            or set(host.get('test_groups', [])) != set(matrix['host_groups'])
            or host.get('coverage') == 'native-partial'
            or host.get('source_commit') != matrix['source_commit'] or host.get('cookbook_commit') != matrix['cookbook_commit']):
        raise ValueError('native partial coverage cannot replace complete host validation')
    return {**host, 'coverage': 'complete-host-and-native-matrix', 'native_matrix': matrix}


def validate_acceptance(matrix, config, jobs, source, tools, environment, configuration_sha256, *, tools_revision=None):
    dependencies.validate_binding(matrix, tools, tools_revision)
    expected = plan(None, config)
    if (not isinstance(matrix, dict) or matrix.get('version') != 1
            or matrix.get('requirements') != expected['requirements']
            or matrix.get('host_groups') != expected['host_groups']
            or matrix.get('source_commit') != source or matrix.get('cookbook_commit') != tools):
        raise ValueError('accepted native coverage differs from the candidate source contract')
    proofs = matrix.get('proofs', [])
    by_id = {value.get('entry', {}).get('id'): value for value in proofs if isinstance(value, dict)}
    if len(by_id) != len(proofs) or set(by_id) != {value['id'] for value in expected['requirements']}:
        raise ValueError('accepted native matrix omitted or repeated platform coverage')
    for entry in expected['requirements']:
        proof = by_id[entry['id']]
        dependencies.validate_binding(proof, tools, tools_revision)
        if (proof.get('entry') != entry or proof.get('coverage') != 'native-partial' or proof.get('complete_host') is not False
                or proof.get('source_commit') != source or proof.get('cookbook_commit') != tools
                or proof.get('configuration_sha256') != configuration_sha256 or proof.get('status') != 'passed'
                or set(proof.get('tests', {})) != set(expected_receipts(entry))):
            raise ValueError('accepted native test scope or source metadata changed')
        context_matches(proof.get('execution_context', {}), environment, job='native_one' if entry['wave'] == 1 else 'native_two')
    actual = validate_jobs(expected['requirements'], jobs)
    if actual != matrix.get('jobs'):
        raise ValueError('accepted native proof belongs to different matrix job executions')
    return actual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['plan', 'run'])
    parser.add_argument('--source', required=True)
    parser.add_argument('--cookbook-ref', required=True)
    parser.add_argument('--entry')
    args = parser.parse_args()
    root = Path.cwd().resolve()
    try:
        if args.operation == 'plan':
            ci.identity(root, args.source, args.cookbook_ref)
            declaration = plan(root, load(root))
            if os.environ.get('GITHUB_OUTPUT'):
                with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                    for name in ['wave1', 'wave2']:
                        output.write(name + '=' + json.dumps(declaration[name], separators=(',', ':')) + '\n')
                        output.write('has-' + name + '=' + ('true' if declaration[name]['include'] else 'false') + '\n')
            print(json.dumps(declaration, indent=2))
        elif not args.entry:
            raise ValueError('native runs require one declared matrix entry')
        else:
            run_entry(root, args.source, args.cookbook_ref, args.entry)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
