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

from configuration import ci_settings, load, relative
import native_components
import native_extras
import recipe
import recipe_contract
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
LOCKFILE_AUDIT_STEP = 'Audit Rust lockfiles'
# The job ids a wave's proofs carry in their execution context, per wave, in the multi-job matrix.
MATRIX_JOBS = {1: 'native_one', 2: 'native_two'}


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


def plan(root, config, environment=None):
    targets = config.get('package', {}).get('native_targets', [])
    host_groups = recipe.execution_test_suites(root, config, environment) if targets else []
    audit_platform = config.get('ci', {}).get('rust_lockfile_audit')
    reuse_unit = config.get('ci', {}).get('rust_reuse_native_unit', False)
    evidence = config.get('artifacts', {}).get('native_e2e_evidence_paths', [])
    if (not isinstance(evidence, list) or len(evidence) > 32
            or any(not isinstance(name, str) or not name.startswith('.artifacts/') for name in evidence)
            or len(evidence) != len(set(evidence)) or set(evidence) & set(config.get('artifacts', {}).get('evidence_paths', []))):
        raise ValueError('native end-to-end evidence requires distinct checkout artifact paths')
    for name in evidence:
        recipe_contract._path(name, 'native end-to-end evidence')
    if not targets:
        if audit_platform is not None or reuse_unit:
            raise ValueError('native Rust checks require a declared native matrix')
        if evidence:
            raise ValueError('native end-to-end evidence requires a native matrix')
        return {'version': 1, 'requirements': [], 'wave1': {'include': []}, 'wave2': {'include': []}}
    if config['recipe'] != 'rust-cli' or not isinstance(targets, list) or len(targets) > 16:
        raise ValueError('this native matrix requires explicit Rust CLI target records')
    native_extras.records(config)
    integration = {value['path'] for value in config.get('tests', {}).get('integration', [])}
    first, second = phase_names(config)
    graph = bool(config.get('components', {}).get('native'))
    if evidence and (not graph or second or not config.get('tests', {}).get('e2e')
                     or not any((target.get('platform'), target.get('arch')) == ('Linux', 'x86_64') for target in targets)):
        raise ValueError('native end-to-end tests require a complete Linux x86_64 producer and declared checks')
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
            selection_environment = {'COOKBOOK_NATIVE_COMPONENTS': json.dumps(names)} if graph else {}
            selected = native_components.selection(config, selection_environment) if graph else []
            checks = sorted({path for value in selected for path in value.get('checks', [])}) if graph else target.get('checks', [])
            if not isinstance(checks, list) or len(checks) != len(set(checks)) or not set(checks) <= integration:
                raise ValueError('native boundary selectors must name declared integration tests')
            cargo = [value['name'] for value in rust_tests.eligible(root, config, selection_environment, system + '/' + arch)]
            builds = [value['name'] for value in rust_tests.eligible(root, config, selection_environment, system + '/' + arch, 'build')]
            groups = [] if any(value.get('suite', 'unit') == 'unit' for value in config.get('components', {}).get('rust_tests', [])) else ['unit']
            budget = target.get('second_wave_timeout_minutes', target.get('timeout_minutes', 30)) if wave == 2 else target.get('timeout_minutes', 30)
            if type(budget) is not int or not 1 <= budget <= 120:
                raise ValueError('native matrix timeouts must be bounded source data')
            requirements.append({'id': f'{wave}-{system}-{arch}', 'wave': wave, 'runner': target['runner'],
                'platform': system, 'arch': arch, 'target': target['target'], 'extension': target['extension'], 'components': names,
                'groups': groups, 'cargo': cargo, 'checks': checks, 'build_checks': builds,
                'job': f'Test (wave {wave}, {system}-{arch})', 'timeout_minutes': budget})
            if evidence and 'e2e' in host_groups and (system, arch) == ('Linux', 'x86_64'):
                requirements[-1]['e2e_evidence'] = evidence
    if evidence and audit_platform is None:
        audit_platform = 'Linux/arm64' if ('Linux', 'arm64') in seen else 'Linux/x86_64'
    if audit_platform is not None:
        audit = [entry for entry in requirements if entry['wave'] == 1 and entry['platform'] == 'Linux'
                 and 'Linux/' + entry['arch'] == audit_platform]
        if len(audit) != 1:
            raise ValueError('the Rust lockfile audit requires its declared first-wave Linux platform')
        audit[0]['audit_lockfiles'] = True
    if reuse_unit:
        matches = [entry for entry in requirements if entry['id'] == '1-Linux-x86_64' and 'unit' in entry['groups']]
        if len(matches) != 1 or graph:
            raise ValueError('native unit reuse requires the complete Linux x86_64 Cargo test group')
        matches[0]['host_unit_verified'] = True
    result = {'version': 1, 'requirements': requirements, 'host_groups': host_groups,
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


SAME_ATTEMPT = ('native packets are exchanged only between jobs of the same run attempt: use "Re-run all jobs", '
                'not "Re-run failed jobs"')


def another_attempt(value, expected):
    """Refuse, with the way out, a proof that is this run's but another attempt's; other mismatches are not its business."""
    if (isinstance(value, dict) and expected.get('GITHUB_RUN_ATTEMPT') and value.get('GITHUB_RUN_ATTEMPT') != expected['GITHUB_RUN_ATTEMPT']
            and all(value.get(key) == expected.get(key) for key in CONTEXT if key != 'GITHUB_RUN_ATTEMPT')):
        raise ValueError('native proof belongs to attempt ' + str(value.get('GITHUB_RUN_ATTEMPT')) + ' of this run, not attempt '
                         + expected['GITHUB_RUN_ATTEMPT'] + '; ' + SAME_ATTEMPT)


def context_matches(value, expected, *, job=None):
    if not isinstance(value, dict) or any(not expected.get(key) or value.get(key) != expected[key] for key in CONTEXT):
        raise ValueError('native proof belongs to another Actions execution')
    if job is not None and value.get('GITHUB_JOB') != job:
        raise ValueError('native proof belongs to another matrix wave')


def expected_receipts(entry):
    return [*entry['groups'], *[slug('cargo', name) for name in entry['cargo']],
            *[slug('boundary', name) for name in entry['checks']], *(['e2e'] if entry.get('e2e_evidence') else [])]


def e2e_evidence(proof, entry):
    evidence = proof.get('evidence', {})
    if (not isinstance(evidence, dict) or set(evidence) != set(entry.get('e2e_evidence', []))
            or any(not isinstance(value, str) or not SHA256.fullmatch(value) for value in evidence.values())):
        raise ValueError('native packet must carry its declared end-to-end evidence hashes')
    return evidence


def imported_checks(matrix):
    checks = {}
    if matrix is not None:
        for proof in matrix['proofs']:
            entry = proof.get('entry', {})
            for group, selected in [('e2e', entry.get('e2e_evidence')), ('unit', entry.get('host_unit_verified'))]:
                if selected:
                    if group in checks or group not in proof['tests']:
                        raise ValueError('the declared native test group requires exactly one passing producer')
                    checks[group] = proof
    return checks


def imported_cargo(matrix, config):
    wanted = rust_tests.host_unit_names(config)
    if not wanted or matrix is None or any(entry['wave'] != 1 for entry in matrix['requirements']):
        return None, []
    runner = ci_settings(config)['runner']
    for proof in matrix['proofs']:
        entry = proof['entry']
        if (entry['platform'], entry['arch'], entry['runner']) == ('Linux', 'x86_64', runner):
            names = sorted(wanted.intersection(entry['cargo']))
            if names:
                if not all(slug('cargo', name) in proof['tests'] for name in names):
                    raise ValueError('native Cargo reuse requires every selected unit receipt')
                return proof, names
    return None, []


def build_payloads(root, config, entry):
    adapter = recipe.adapter(config)
    package = adapter.package_data(root, config)
    graph = config.get('components', {}).get('native')
    selected = [value for value in native_components.records(config) if value['name'] in entry['components']] if graph else []
    required = set()
    if graph:
        for value in selected:
            path = native_components.output(root, value, entry['platform'] + '/' + entry['arch'])
            required.update([path.relative_to(root).as_posix(), path.with_name(path.name + '.json').relative_to(root).as_posix()])
        required.update(native_extras.producer_paths(root,config,entry['platform']+'/'+entry['arch'],entry['components']))
    if not graph or any(value['role'] == 'cli' for value in selected):
        native = helper('native_cli')
        archive = native.archive_name(package, entry)
        output = package['output'] + ('.exe' if entry['platform'] == 'Windows' and not package['output'].endswith('.exe') else '')
        required.update(['dist/' + archive, 'dist/checksums.txt', output,
                         '.artifacts/native-package/' + archive, '.artifacts/native-package/' + archive + '.json'])
        required.update('dist/' + name for name in adapter.package_extra_names(config, native=True))
        if package.get('release_metadata'):
            required.add('dist/release.json')
    scopes = adapter.base_artifact_paths(root, config) if not graph or any(value['role'] == 'cli' for value in selected) else []
    return required, [*scopes, *required]


def collect(root, config, source, tools, entry):
    tree = ci.identity(root, source, tools)
    verify = ci.verification()
    scope = recipe.adapter(config).verification_scope(root, config)
    build = read_json(root / '.artifacts/verification/build.json')
    execution = verify.execution_context()
    if (build.get('identity') != verify.identity(scope) or build.get('artifacts') != ci.artifacts(root, config, scope)):
        raise ValueError('native source or build outputs changed after verification')
    records = {name: read_json(root / '.artifacts/verification' / (name + '.json')) for name in expected_receipts(entry)}
    if any(value.get('tested_commit') != source for value in records.values()):
        raise ValueError('native test receipts must prove actually tested source')
    for value in [build.get('provenance', {}), *records.values()]:
        if (value.get('source_commit') != source or value.get('source_tree') != tree
                or value.get('tracked_source_clean') is not True or value.get('input_identity') != build['identity']
                or value.get('execution_context') != execution):
            raise ValueError('native checks did not accept the same source, outputs and job execution')
    evidence = {name: digest(relative(root, name, exists=True)) for name in entry.get('e2e_evidence', [])}
    if set(evidence) & set(build['artifacts']):
        raise ValueError('native end-to-end evidence must be separate from build outputs')
    return {'version': 1, 'coverage': 'native-partial', 'complete_host': False,
            'repository': os.environ.get('GITHUB_REPOSITORY'), 'source_commit': source, 'source_tree': tree,
            **dependencies.binding(tools), 'configuration_sha256': configuration_hash(root),
            'entry': entry, 'execution_context': execution, 'status': 'passed',
            'version_identity': os.environ.get('VERSION', 'dev'), 'date_identity': os.environ.get('DATE', 'unknown'),
            'build': build, 'tests': records, **({'evidence': evidence} if evidence else {})}


def export(root, proof):
    stage = relative(root, '.artifacts/native-export')
    helper('clean').clean(root, ['.artifacts/native-export'])
    stage.mkdir(parents=True, mode=0o700)
    artifacts = proof['build']['artifacts'] | proof.get('evidence', {})
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


def packet(root, directory, config, source, tools, entry, environment, jobs=None):
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
    if jobs:
        another_attempt(proof.get('execution_context', {}), environment)
    context_matches(proof.get('execution_context', {}), environment, job=(jobs or MATRIX_JOBS)[entry['wave']])
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
    evidence = e2e_evidence(proof, entry)
    required, allowed = build_payloads(root, config, entry)
    if not isinstance(artifacts, dict) or not artifacts or len(artifacts) > MAX_FILES or not required <= set(artifacts):
        raise ValueError('native packet omitted required executable/archive/source receipts')
    for name, expected in artifacts.items():
        if (not isinstance(name, str) or not isinstance(expected, str) or not SHA256.fullmatch(expected)
                or not any(name == scope or name.startswith(scope.rstrip('/') + '/') for scope in allowed)):
            raise ValueError('native packet contains undeclared output paths or hashes')
        if digest(relative(directory / 'payload', name, exists=True)) != expected:
            raise ValueError('native packet payload differs from its accepted build hash')
    if set(evidence) & set(artifacts) or any(digest(relative(directory / 'payload', name, exists=True)) != expected
                                            for name, expected in evidence.items()):
        raise ValueError('native end-to-end evidence differs from its producer hash')
    actual = set()
    for path in directory.rglob('*'):
        if path.is_symlink():
            raise ValueError('native packets may not contain symlinks')
        if path.is_file():
            actual.add(path.relative_to(directory).as_posix())
    if actual != {'native-proof.json', *['payload/' + name for name in artifacts | evidence]}:
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
    try:
        subprocess.run(['gh', 'run', 'download', run, '--repo', repository, '--pattern',
                        f'cookbook-native-{source}-{str(wave) + "-" if wave is not None else ""}*-{run}-{attempt}', '--dir', str(directory)],
                       check=True, timeout=300)
    except subprocess.CalledProcessError as error:
        raise ValueError('no native packets of attempt ' + attempt + ' of run ' + run + ' could be downloaded (gh exit status '
                         + str(error.returncode) + '); ' + SAME_ATTEMPT) from error
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


def audit_record(jobs, name, environment):
    """The one same-attempt job record with this exact name whose lockfile audit step succeeded."""
    _repository, run, attempt = run_identity(environment)
    matches = [job for job in jobs['jobs'] if job.get('name') == name]
    if len(matches) != 1:
        raise ValueError('native validation requires one same-attempt lockfile audit job')
    job = matches[0]
    steps = [step for step in job.get('steps', []) if step.get('name') == LOCKFILE_AUDIT_STEP]
    if (job.get('run_id') != int(run) or job.get('run_attempt') != int(attempt)
            or job.get('status') != 'completed' or job.get('conclusion') != 'success'
            or len(steps) != 1 or steps[0].get('status') != 'completed'
            or steps[0].get('conclusion') != 'success'):
        raise ValueError('the declared job must pass the current Rust lockfile audit')
    return {'id': job['id'], 'name': job['name']}


def validate_lockfile_audit(entries, accepted_jobs, jobs, environment):
    prefix = accepted_jobs[0]['name'].removesuffix(entries[0]['job'])
    audit_name = next((entry['job'] for entry in entries if entry.get('audit_lockfiles')), 'Plan')
    return audit_record(jobs, prefix + audit_name, environment)


def packet_destination(root, config, graph, entry, name):
    """Where an imported packet file lands in the host checkout, or None for files that stay in the packet."""
    if name.startswith('.artifacts/native-package/'):
        return root / '.artifacts/native-archives' / Path(name).name
    if graph:
        allowed = {native_components.output(root, value, entry['platform'] + '/' + entry['arch']).relative_to(root).as_posix()
                   for value in graph.values() if value['name'] in entry['components']}
        allowed.update(name.removesuffix('.json') for name in native_extras.producer_paths(root, config,
            entry['platform'] + '/' + entry['arch'], entry['components']))
        if name in allowed or name.removesuffix('.json') in allowed:
            return relative(root, name)
    return None


def import_packets(root, config, source, tools, requirements, directory, environment, jobs=None):
    expected_names = {artifact_name(source, entry, environment) for entry in requirements}
    actual = {path.name for path in Path(directory).iterdir()}
    if actual != expected_names:
        raise ValueError('native artifact matrix is incomplete, stale or contains unexpected jobs' + ('; ' + SAME_ATTEMPT if jobs else ''))
    proofs = []
    graph = {value['name']: value for value in native_components.records(config)} if config.get('components', {}).get('native') else {}
    for entry in requirements:
        source_directory = Path(directory) / artifact_name(source, entry, environment)
        proof = packet(root, source_directory, config, source, tools, entry, environment, jobs)
        proofs.append(proof)
        for name in e2e_evidence(proof, entry):
            incoming = relative(source_directory / 'payload', name, exists=True)
            destination = relative(root, name)
            if destination.exists():
                raise ValueError('native end-to-end evidence requires a fresh host destination')
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(incoming, destination)
        for name in proof['build']['artifacts']:
            destination = packet_destination(root, config, graph, entry, name)
            if destination is not None:
                incoming = relative(source_directory / 'payload', name, exists=True)
                if destination.exists() and digest(destination) != digest(incoming):
                    raise ValueError('native artifact imports disagree about an immutable output')
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(incoming, destination)
                if not destination.name.endswith('.json'):
                    destination.chmod(0o755)
    return proofs


def select_entry(root, config, entry_id):
    declaration = plan(root, config)
    matches = [value for value in declaration['requirements'] if value['id'] == entry_id]
    if len(matches) != 1:
        raise ValueError('select one exact declared native matrix entry')
    return declaration, matches[0]


def entry_environment(root, config, entry):
    """Point this process at one declared platform: its target, component wave and no inherited selections."""
    if helper('native_cli').host() != (entry['platform'], entry['arch']):
        raise ValueError('native runner host differs from the declared platform/architecture')
    os.environ['RUST_TARGET'] = entry['target']
    os.environ['COOKBOOK_STAGE'] = 'native'
    os.environ['COOKBOOK_ROOT'] = str(COOKBOOK)
    if entry['components']:
        os.environ['COOKBOOK_NATIVE_COMPONENTS'] = json.dumps(entry['components'])
    else:
        os.environ.pop('COOKBOOK_NATIVE_COMPONENTS', None)
    for name in ['COOKBOOK_TEST_PATHS', 'COOKBOOK_NATIVE_TESTS', 'NATIVE_CLI_ARCHIVES', 'COOKBOOK_NATIVE_BUILD_CHECKS',
                 recipe.IMPORTED_NATIVE_GROUPS, rust_tests.IMPORTED_UNITS]:
        os.environ.pop(name, None)
    recipe.configure_environment(root, config)


def activate_sdk(root):
    """Retain the verified Rust SDK activation for nested compilers launched by later native Python checks."""
    protected = {key: value for key, value in os.environ.items()
                 if key.startswith(('GITHUB_', 'COOKBOOK_')) or key in {
                     'TOOLS_ROOT', 'TOOLS_REF', 'GH_TOKEN', 'GITHUB_TOKEN',
                     'TOOLS_VERIFICATION_SESSION', 'COMMIT', 'VERSION', 'DATE', 'RUST_TARGET'}}
    prepared = helper('pinned-tools').activation(root,
        [('rust', 'cargo'), ('rust', 'rustc'), ('rust', 'rustup')], os.environ)
    if any(prepared.get(key) != value for key, value in protected.items()):
        raise ValueError('native SDK activation must preserve caller/source and Actions identity')
    os.environ.update(prepared)


def run_groups(root, entry):
    """Run this platform's own test selections, each leaving its own receipt."""
    entry_point = COOKBOOK / 'cookbook/dispatch.py'
    if entry['groups']:
        subprocess.run(sdk_environment.python_entry(entry_point, 'test:unit'), cwd=root, env=os.environ, check=True)
    if entry['cargo']:
        subprocess.run(sdk_environment.python_entry(entry_point, 'test:unit'), cwd=root,
                       env={**os.environ, 'COOKBOOK_NATIVE_TESTS': json.dumps(entry['cargo'])}, check=True)
    if entry['checks']:
        subprocess.run(sdk_environment.python_entry(entry_point, 'test:integration'), cwd=root,
                       env={**os.environ, 'COOKBOOK_TEST_PATHS': json.dumps(entry['checks'])}, check=True)
    if entry.get('e2e_evidence'):
        subprocess.run(sdk_environment.python_entry(entry_point, 'test:e2e'), cwd=root, env=os.environ, check=True)


def run_entry(root, source, tools, entry_id):
    config = load(root)
    ci.identity(root, source, tools)
    declaration, entry = select_entry(root, config, entry_id)
    entry_environment(root, config, entry)
    if entry['wave'] == 2:
        first = [value for value in declaration['requirements'] if value['wave'] == 1]
        directory = download(root, source, os.environ, wave=1)
        import_packets(root, config, source, tools, first, directory, os.environ)
        helper('clean').clean(root, ['.artifacts/native-downloads'])
    for command in ['setup', 'build']:
        subprocess.run(sdk_environment.python_entry(COOKBOOK / 'cookbook/dispatch.py', command), cwd=root, env=os.environ, check=True)
        if command == 'setup':
            # This declared native matrix is Rust CLI-only. Child setup prepares
            # its Rust SDK; retain the verified activation for nested compilers
            # launched by the subsequent native Python checks as well.
            activate_sdk(root)
    run_groups(root, entry)
    proof = collect(root, config, source, tools, entry)
    export(root, proof)


# One-job mode. Every platform is its own job. The primary platform also hosts the groups that the multi-job
# matrix runs once on the Integration runner (lint, unit, security, integration, e2e); it validates its own build
# completely, which covers its platform selections too. Every other platform builds and tests its own selections.
#
# A recipe whose components consume other platforms' artifacts has a second wave. Its jobs stay one per platform and
# wave, ordered by needs: the first wave (every platform, as above but without the groups), the second wave (every
# platform, after importing the first wave's packets) and one integration job on the primary platform that imports
# every packet and hosts the groups. Each job hands its accepted work on as the same packet the multi-job matrix
# exchanges, and the importing job verifies it with the same proof checks.
PRIMARY = ('Linux', 'x86_64')
LOCAL_TARGET = 'COOKBOOK_COMPILER_CACHE_TARGET'
WAVE = 'COOKBOOK_NATIVE_WAVE'  # job env naming the job's role: '1' (default), '2' or HOST
HOST = 'host'
SINGLE_CHECK = 'Build and test'
INTEGRATION = SINGLE_CHECK + ' (integration)'
# The job ids whose execution context the packets of each wave carry in one-job mode.
SINGLE_JOBS = {1: 'build-test', 2: 'build-test-wave2'}
HOST_MATRIX = '.artifacts/native-single-matrix.json'
# A release run assembles and signs every platform's archives together, so it always has an integration job. The
# release workflow's jobs name this in their environment; the release verifier asks for the same declaration.
RELEASE = 'COOKBOOK_NATIVE_RELEASE'
RELEASE_REASON = 'a release assembles, attests and publishes every platform archive together'
SINGLE_HOST_JOB = 'build-test-host'
# The release plan job audits the lockfiles, before any platform job compiles anything.
RELEASE_PLAN = 'Plan release'


def second_wave(config):
    """Components that consume another platform's artifacts, and so need a second wave of jobs."""
    values = native_components.records(config) if config.get('components', {}).get('native') else []
    first, second = phase_names(config)
    needed = {}
    for value in values:
        if value['name'] in second:
            needed[value['name']] = [item['component'] + ' for ' + item['platform'] for item in value.get('needs', [])
                                     if item.get('platform', 'native') != 'native']
    return needed


def single_check(entry):
    """The check a platform's job reports in one-job mode: stable, one per platform and wave."""
    return SINGLE_CHECK + ' (' + entry['platform'] + '-' + entry['arch'] + (' wave 2' if entry['wave'] == 2 else '') + ')'


def integration_reasons(config):
    """What the complete build on the primary platform needs from other platform jobs, one sentence each.

    A second wave's components consume other platforms' outputs, and a package companion is another platform's
    component output published beside the archives. The multi-job Integration job gets both from the platform packets.
    """
    reasons = [name + (' needs ' + ' and '.join(needs) if needs else ' follows those components')
               for name, needs in second_wave(config).items()]
    primary = '/'.join(PRIMARY)
    for name, (component, platform) in native_extras.records(config).items():
        if platform != primary:
            reasons.append('package file ' + name + ' is ' + component['name'] + ' for ' + platform)
    return reasons


def release_selected(environment=None):
    """Whether this job belongs to a one-job release run, whose complete build is always assembled by the integration job."""
    environment = os.environ if environment is None else environment
    value = environment.get(RELEASE, '')
    if value not in {'', 'true'}:
        raise ValueError(RELEASE + ' must be true when set')
    return value == 'true'


def annotate(config, declaration, release=False):
    """Mark whether this recipe needs the integration job; refuse producers that no declared platform job provides."""
    declared = {target.get('platform') + '/' + target.get('arch') for target in config.get('package', {}).get('native_targets', [])}
    wanted = {item['platform'] for value in (native_components.records(config) if config.get('components', {}).get('native') else [])
              for item in value.get('needs', []) if item.get('platform', 'native') != 'native'}
    wanted.update(platform for _component, platform in native_extras.records(config).values())
    missing = sorted(wanted - declared)
    if missing:
        raise ValueError('single-job CI needs a declared native target for ' + ', '.join(missing)
                         + ': another platform\'s job must build the output that the complete build consumes (' + '; '.join(integration_reasons(config)) + ')')
    declaration['integration_reasons'] = integration_reasons(config) + ([RELEASE_REASON] if release else [])
    declaration['integration'] = bool(declaration['integration_reasons'])
    return declaration


def single_plan(root, config, release=None, environment=None):
    """The declared matrix with each entry's job named by the check its one-job job reports.

    release defaults to the job environment's selection; the release verifier passes True.
    """
    release = release_selected() if release is None else release
    declaration = plan(root, config, environment)
    if not declaration['requirements']:
        return declaration
    annotate(config, declaration, release)
    if declaration['integration']:
        # Only the jobs that exchange packets are identified by their one-job check; other recipes keep their receipts.
        for entry in declaration['requirements']:
            entry['job'] = single_check(entry)
    return declaration


def single_job_support(config, release=False):
    """Refuse recipes that the one-job jobs cannot cover, naming the platforms and the reason."""
    declaration = plan(None, config)
    if not declaration['requirements']:
        raise ValueError('single-job Rust CLI CI requires its declared native matrix')
    if not any((entry['platform'], entry['arch']) == PRIMARY for entry in declaration['wave1']['include']):
        raise ValueError('single-job CI needs a Linux x86_64 target to host the lint, unit, security, integration and e2e groups')
    return annotate(config, declaration, release)


def local_role(environment=None):
    environment = os.environ if environment is None else environment
    role = environment.get(WAVE, '1')
    if role not in {'1', '2', HOST}:
        raise ValueError('the native job role must be 1, 2 or ' + HOST)
    return role


def local_entry(root, config, environment=None, declaration=None):
    """The one declared platform this job is: Prepare's compilation target selects it, the runner host must agree.

    The job's wave comes from COOKBOOK_NATIVE_WAVE: 1 (the default), 2 for a second-wave platform, or host for the
    integration job, which is the first-wave Linux x86_64 platform's target and runner.
    """
    environment = os.environ if environment is None else environment
    role = local_role(environment)
    declaration = declaration or single_plan(root, config)
    if role == '2' and not declaration['wave2']['include']:
        raise ValueError('the native job role 2 needs a declared second native wave')
    if role == HOST and not declaration['integration']:
        raise ValueError('the native job role ' + HOST + ' needs a recipe whose complete build consumes other platforms\' outputs')
    matches = [entry for entry in declaration['wave2' if role == '2' else 'wave1']['include']
               if entry['target'] == environment.get(LOCAL_TARGET)]
    if len(matches) != 1:
        raise ValueError('select one declared native platform by its compilation target in the Prepare target input')
    if role == HOST and (matches[0]['platform'], matches[0]['arch']) != PRIMARY:
        raise ValueError('the integration job runs on the platform that hosts the groups')
    return matches[0]


def hosts_groups(entry, declaration=None):
    """Whether this platform's job validates a complete build and runs the groups.

    When the complete build consumes other platforms' outputs (a second wave, or package files built on another
    platform) it needs every platform's packet, so no platform job does: the separate integration job does.
    """
    if declaration is not None and declaration.get('integration'):
        return False
    return (entry['platform'], entry['arch']) == PRIMARY and entry['wave'] == 1


def primary_environment(entry):
    """The platform that hosts the groups validates its complete build for its own target, not a native-stage selection."""
    if helper('native_cli').host() != (entry['platform'], entry['arch']):
        raise ValueError('native runner host differs from the declared platform/architecture')
    os.environ['RUST_TARGET'] = entry['target']
    for name in ['COOKBOOK_NATIVE_COMPONENTS', 'COOKBOOK_TEST_PATHS', 'COOKBOOK_NATIVE_TESTS', 'NATIVE_CLI_ARCHIVES',
                 'COOKBOOK_NATIVE_BUILD_CHECKS', recipe.IMPORTED_NATIVE_GROUPS, rust_tests.IMPORTED_UNITS]:
        os.environ.pop(name, None)


def run_local(root, config, entry, phase, source, tools, head='', base='', handoff=False, declaration=None):
    """Run one phase for a platform that does not host the groups: build and test only its own selections.

    This is run_entry without the Actions API job checks: nothing joins here. handoff is set when later jobs consume
    this platform's work: the test phase then also stages the accepted proof and outputs as the packet the multi-job
    matrix exports, for the job's upload step.
    """
    if hosts_groups(entry, declaration or single_plan(root, config)):
        raise ValueError('local native runs cover only the platforms that do not host the groups')
    if phase not in {'setup', 'build', 'test'}:
        raise ValueError('single-job mode requires the setup, build or test phase')
    entry_environment(root, config, entry)
    entry_point = COOKBOOK / 'cookbook/dispatch.py'
    if phase != 'setup':
        activate_sdk(root)
    receipt = root / '.artifacts/cookbook-validation.json'
    if phase != 'build':
        receipt.unlink(missing_ok=True)
    if phase in {'setup', 'build'}:
        subprocess.run(sdk_environment.python_entry(entry_point, phase), cwd=root, env=os.environ, check=True)
        return
    run_groups(root, entry)
    proof = collect(root, config, source, tools, entry)
    if handoff:
        export(root, proof)
    accepted = {**proof, 'coverage': 'single-job-native-platform'}
    if head:
        accepted.update(head=head, base=base)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps(accepted, indent=2) + '\n')
    required, _allowed = build_payloads(root, config, entry)
    paths = [*sorted(required), '.artifacts/verification/', '.artifacts/cookbook-validation.json']
    for name in paths:
        relative(root, name)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('artifact-paths<<COOKBOOK_ARTIFACT_PATHS\n' + '\n'.join(paths) + '\nCOOKBOOK_ARTIFACT_PATHS\n')


def import_wave(root, config, entry, source, tools, declaration=None):
    """In a second-wave job: download the first wave's packets of this run, verify each and import its outputs."""
    if entry['wave'] != 2:
        raise ValueError('only a second-wave job imports the first wave')
    first = (declaration or single_plan(root, config))['wave1']['include']
    directory = download(root, source, os.environ, wave=1)
    import_packets(root, config, source, tools, first, directory, os.environ, SINGLE_JOBS)
    helper('clean').clean(root, ['.artifacts/native-downloads'])


MATRIX_DIGEST = 'COOKBOOK_NATIVE_MATRIX_SHA256'


def import_host(root, config, source, tools, *, directory=None, jobs=None, declaration=None):
    """In the integration job: verify every packet of both waves and the Actions record of every job, then import them.

    This is prepare_host with this workflow's job ids and names. The accepted matrix is kept for the build and test
    phases, which run as separate processes; host_matrix re-verifies it there. The Rust lockfile audit has no job
    record here: it is the daily Security workflow's cargo audit over every lockfile, whatever platform is declared.
    """
    declaration = declaration or single_plan(root, config)
    if not declaration.get('integration'):
        raise ValueError('only a recipe whose complete build consumes other platforms\' outputs has an integration job')
    environment = dict(os.environ)
    downloaded = directory is None
    directory = Path(directory) if directory is not None else download(root, source, environment)
    proofs = import_packets(root, config, source, tools, declaration['requirements'], directory, environment, SINGLE_JOBS)
    version, date = build_inputs(proofs, environment)
    repository, run, attempt = run_identity(environment)
    jobs = jobs if jobs is not None else github(repository, f'/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100')
    accepted_jobs = validate_jobs(declaration['requirements'], jobs)
    if downloaded:
        helper('clean').clean(root, ['.artifacts/native-downloads'])
    matrix = {'version': 1, 'requirements': declaration['requirements'], 'host_groups': declaration['host_groups'],
              'jobs': accepted_jobs, 'source_commit': source, **dependencies.binding(tools), 'proofs': proofs,
              'lockfile_audit': None}
    body = json.dumps(matrix) + '\n'
    relative(root, HOST_MATRIX).write_text(body)
    # The digest travels in the job environment, so a later phase notices a file changed in between.
    value = hashlib.sha256(body.encode()).hexdigest()
    os.environ[MATRIX_DIGEST] = value
    if os.environ.get('GITHUB_ENV'):
        with open(os.environ['GITHUB_ENV'], 'a') as handle:
            handle.write(MATRIX_DIGEST + '=' + value + '\n')
    host_environment(root, version, date, declaration['requirements'])
    return matrix


def verify_matrix(root, config, matrix, source, tools, declaration, environment):
    """Everything about the accepted matrix that can be checked again without the network.

    The declaration, every proof's identity, source, tools, configuration, coverage, receipts and execution context,
    the recorded job names, and the digest of every imported file still in this checkout. Not re-checked: the Actions
    job records (captured at import) and the packet files themselves (removed after import).
    """
    dependencies.validate_binding(matrix, tools)
    if matrix.get('source_commit') != source or matrix.get('version') != 1:
        raise ValueError('the imported native matrix belongs to another source')
    if matrix.get('requirements') != declaration['requirements'] or matrix.get('host_groups') != declaration['host_groups']:
        raise ValueError('the imported native matrix differs from the declared platforms')
    proofs = matrix.get('proofs')
    expected = {entry['id']: entry for entry in declaration['requirements']}
    if (not isinstance(proofs, list) or len(proofs) != len(expected)
            or {value.get('entry', {}).get('id') for value in proofs if isinstance(value, dict)} != set(expected)):
        raise ValueError('the imported native matrix omitted or repeated platform proofs')
    recorded = {value.get('entry'): value.get('name') for value in matrix.get('jobs', []) if isinstance(value, dict)}
    if (len(recorded) != len(matrix.get('jobs', [])) or set(recorded) != set(expected)
            or any(not isinstance(recorded[name], str) or not recorded[name].endswith(entry['job']) for name, entry in expected.items())):
        raise ValueError('the imported native matrix records other jobs than the declared checks')
    graph = {value['name']: value for value in native_components.records(config)} if config.get('components', {}).get('native') else {}
    for proof in proofs:
        entry = expected[proof['entry']['id']]
        dependencies.validate_binding(proof, tools)
        if (proof.get('entry') != entry or proof.get('coverage') != 'native-partial' or proof.get('complete_host') is not False
                or proof.get('status') != 'passed' or proof.get('source_commit') != source
                or proof.get('configuration_sha256') != configuration_hash(root)
                or set(proof.get('tests', {})) != set(expected_receipts(entry))):
            raise ValueError('an imported native proof differs from the declared platform, source or coverage')
        another_attempt(proof.get('execution_context', {}), environment)
        context_matches(proof.get('execution_context', {}), environment, job=SINGLE_JOBS[entry['wave']])
        evidence = e2e_evidence(proof, entry)
        for name, wanted in [*proof['build']['artifacts'].items(), *evidence.items()]:
            destination = relative(root, name) if name in evidence else packet_destination(root, config, graph, entry, name)
            if destination is not None and (not destination.is_file() or digest(destination) != wanted):
                raise ValueError('an imported native output changed after it was verified: ' + name)


def host_matrix(root, config, source, tools, declaration=None):
    """The matrix the import phase accepted, verified again, with the adopted version and archives applied here."""
    path = relative(root, HOST_MATRIX, exists=True)
    body = path.read_bytes()
    expected = os.environ.get(MATRIX_DIGEST)
    if (os.environ.get('GITHUB_ACTIONS') == 'true' and not expected) or (expected and hashlib.sha256(body).hexdigest() != expected):
        raise ValueError('the imported native matrix changed after the import phase')
    matrix = read_json(path, maximum=256 << 20)
    verify_matrix(root, config, matrix, source, tools, declaration or single_plan(root, config), os.environ)
    version, date = build_inputs(matrix['proofs'], {})
    host_environment(root, version, date, matrix['requirements'])
    return matrix


def build_inputs(proofs, environment):
    """The release version and date every native builder agreed on, which the host adopts."""
    versions = {(value['version_identity'], value['date_identity']) for value in proofs}
    if len(versions) != 1:
        raise ValueError('native builders disagree about release version/date inputs')
    version, date = versions.pop()
    if (not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}', version)
            or not isinstance(date, str) or len(date) > 128 or any(ord(character) < 32 for character in date)
            or ('VERSION' in environment and environment['VERSION'] != version)
            or ('DATE' in environment and environment['DATE'] != date)):
        raise ValueError('native artifact build inputs differ from the requested host gate')
    return version, date


def host_environment(root, version, date, requirements):
    """Point this process at the imported platform archives and the adopted version, with no worker selections."""
    os.environ.update(VERSION=version, DATE=date, NATIVE_CLI_ARCHIVES=str(root / '.artifacts/native-archives'))
    os.environ['COOKBOOK_NATIVE_BUILD_CHECKS'] = json.dumps(sorted({name for row in requirements for name in row['build_checks']}))
    os.environ.pop('COOKBOOK_NATIVE_COMPONENTS', None)
    os.environ.pop('COOKBOOK_NATIVE_TESTS', None)
    os.environ.pop('COOKBOOK_TEST_PATHS', None)


def prepare_host(root, config, source, tools, *, directory=None, jobs=None):
    declaration = plan(root, config)
    if not declaration['requirements']:
        return None
    environment = dict(os.environ)
    downloaded = directory is None
    directory = Path(directory) if directory is not None else download(root, source, environment)
    proofs = import_packets(root, config, source, tools, declaration['requirements'], directory, environment)
    version, date = build_inputs(proofs, environment)
    repository, run, attempt = run_identity(environment)
    jobs = jobs if jobs is not None else github(repository, f'/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100')
    accepted_jobs = validate_jobs(declaration['requirements'], jobs)
    lockfile_audit = validate_lockfile_audit(declaration['requirements'], accepted_jobs, jobs, environment)
    if downloaded:
        helper('clean').clean(root, ['.artifacts/native-downloads'])
    host_environment(root, version, date, declaration['requirements'])
    return {'version': 1, 'requirements': declaration['requirements'], 'host_groups': declaration['host_groups'], 'jobs': accepted_jobs,
            'source_commit': source, **dependencies.binding(tools), 'proofs': proofs, 'lockfile_audit': lockfile_audit}


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


def validate_imported_outputs(root, config, host, producer):
    entry = producer['entry']
    required, _allowed = build_payloads(root, config, entry)
    # The checksum inventory expands when the other platform archives arrive.
    expected = producer['build']['artifacts']
    if (any(host['artifacts'].get(name) != expected[name] for name in required - {'dist/checksums.txt'})
            or any(host['evidence'].get(name) != value for name, value in e2e_evidence(producer, entry).items())):
        raise ValueError('assembled build or evidence differs from the tested native inputs')


def validate_acceptance(matrix, config, jobs, source, tools, environment, configuration_sha256, *, tools_revision=None,
                        single=False):
    """Check a complete matrix against this run. single selects the one-job release shape: its declaration, its
    job ids and names, and a lockfile audit that the integration job records itself (not part of the matrix)."""
    dependencies.validate_binding(matrix, tools, tools_revision)
    expected = single_plan(None, config, True, environment) if single else plan(None, config, environment)
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
        e2e_evidence(proof, entry)
        if (proof.get('entry') != entry or proof.get('coverage') != 'native-partial' or proof.get('complete_host') is not False
                or proof.get('source_commit') != source or proof.get('cookbook_commit') != tools
                or proof.get('configuration_sha256') != configuration_sha256 or proof.get('status') != 'passed'
                or set(proof.get('tests', {})) != set(expected_receipts(entry))):
            raise ValueError('accepted native test scope or source metadata changed')
        context_matches(proof.get('execution_context', {}), environment,
                        job=(SINGLE_JOBS if single else MATRIX_JOBS)[entry['wave']])
    actual = validate_jobs(expected['requirements'], jobs)
    if actual != matrix.get('jobs'):
        raise ValueError('accepted native proof belongs to different matrix job executions')
    if single:
        if matrix.get('lockfile_audit') is not None:
            raise ValueError('a one-job release matrix records no earlier lockfile audit')
        audit_record(jobs, RELEASE_PLAN, environment)
        return actual
    if validate_lockfile_audit(expected['requirements'], actual, jobs, environment) != matrix.get('lockfile_audit'):
        raise ValueError('accepted lockfile audit belongs to a different job')
    return actual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['plan', 'audit', 'run'])
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
                    audit_in_plan = not any(entry.get('audit_lockfiles') for entry in declaration['requirements'])
                    output.write('audit-in-plan=' + str(audit_in_plan).lower() + '\n')
            print(json.dumps(declaration, indent=2))
        elif args.operation == 'audit':
            import rust_cli

            ci.identity(root, args.source, args.cookbook_ref)
            config = load(root)
            rust_cli.audit_lockfiles(root, config, rust_cli.environment(root, config))
        elif not args.entry:
            raise ValueError('native runs require one declared matrix entry')
        else:
            run_entry(root, args.source, args.cookbook_ref, args.entry)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
