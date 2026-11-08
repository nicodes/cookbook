"""Derive Godot job topology and verify complete, disjoint native suite selections."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re

import sdk_environment

from configuration import relative, read_source_data

RUNNERS = {'ubuntu-22.04', 'ubuntu-24.04', 'blacksmith-4vcpu-ubuntu-2204', 'blacksmith-4vcpu-ubuntu-2404'}
NAME = re.compile(r'(?:test-|suite-)[a-z0-9_-]{1,120}\Z')
SHA = re.compile(r'[a-f0-9]{40}\Z')
GROUPS = {'lint', 'unit', 'security', 'integration', 'e2e'}


def partition(targets, shard):
    if not isinstance(targets, list) or not targets or len(targets) != len(set(targets)):
        raise ValueError('suite inventory must be nonempty and unique')
    if any(not isinstance(value, str) or not NAME.fullmatch(value) for value in targets):
        raise ValueError('invalid named native suite identity')
    if not isinstance(shard, str) or not re.fullmatch(r'[1-9][0-9]*/[1-9][0-9]*', shard):
        raise ValueError('shard must be INDEX/COUNT')
    index, count = map(int, shard.split('/'))
    if index > count or count > len(targets):
        raise ValueError('invalid or empty native test shard')
    return sorted(targets)[index - 1::count]


def run_cases(cases, jobs, execute):
    if type(jobs) is not int or not 1 <= jobs <= 4:
        raise ValueError('native per-runner workers must stay between one and four')
    partition(list(cases), '1/1')
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(lambda name: execute(cases[name]), sorted(cases)))
    return next((value for value in results if value), 0)


def verify_selections(directory, source, targets, count, run_id, attempt):
    if not isinstance(source, str) or not SHA.fullmatch(source) or type(count) is not int or count < 1:
        raise ValueError('exact source and a positive shard count are required')
    inventory = partition(targets, '1/1')
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('test evidence requires an unlinked directory')
    paths = list(directory.rglob('godot-test-selection.json'))
    if len(paths) != count:
        raise ValueError('complete native shard evidence is required')
    seen = set()
    for path in paths:
        if (path.is_symlink() or not path.is_file() or path.stat().st_size > 1000000
                or any(parent.is_symlink() for parent in path.parents)):
            raise ValueError('native selection evidence requires a bounded regular file')
        row = json.loads(path.read_text())
        shard = row.get('shard')
        if shard in seen or shard not in {f'{number}/{count}' for number in range(1, count + 1)}:
            raise ValueError('unexpected or repeated native shard')
        seen.add(shard)
        if (row.get('source_commit') != source or row.get('run_id') != run_id
                or row.get('attempt') != attempt or row.get('status') != 'passed'
                or row.get('workers') != 4 or row.get('inventory') != inventory
                or row.get('selected') != partition(targets, shard)):
            raise ValueError('native selections differ from the current source, execution or full inventory')
    return {'source_commit': source, 'run_id': run_id, 'attempt': attempt,
            'shards': count, 'suite_targets': len(inventory), 'status': 'passed'}


def case_id(suite, entry):
    wire = json.dumps({'suite': suite, 'entry': entry}, sort_keys=True, separators=(',', ':'))
    return 'case-' + hashlib.sha256(wire.encode()).hexdigest()


def unit_groups(config):
    groups = {}
    executions = set()
    for entry in config.get('tests', {}).get('unit', []):
        if entry['runner'] not in {'godot', 'godot-check'}:
            continue
        name = entry.get('name', 'test-' + hashlib.sha256(entry['path'].encode()).hexdigest()[:24])
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError('Godot shard entries require bounded literal native suite identities')
        signature = json.dumps({key: value for key, value in entry.items() if key != 'name'}, sort_keys=True)
        if signature in executions:
            raise ValueError('native Godot coverage repeats the same written assertion execution')
        executions.add(signature)
        groups.setdefault(name, []).append(case_id('unit', entry))
    if any(len(values) != len(set(values)) for values in groups.values()):
        raise ValueError('native Godot coverage repeats the same written assertion entry')
    return groups


def budget(value):
    if type(value) is not int or not 1 <= value <= 120:
        raise ValueError('Godot job deadlines must be bounded source integers')
    return value


def effective_host_budget(root, config, original, environment=None):
    """A tracked delivery policy may retain a distinct exact-main CD deadline."""
    selected=config.get('delivery')
    if root is None or not isinstance(selected,dict) or set(selected)!={'definition'}:
        return original
    path=relative(Path(root),selected['definition'],exists=True)
    if not path.is_file() or path.stat().st_size>32768:
        raise ValueError('Godot delivery deadlines require bounded native source data')
    data=read_source_data(path, maximum=32768)
    if not isinstance(data,dict) or 'validation_timeout_minutes' not in data:
        return original
    value=data['validation_timeout_minutes']
    if type(data.get('version')) is not int or data['version']!=1 or type(value) is not int or not 1<=value<=180:
        raise ValueError('Godot CD validation requires its bounded native deadline')
    environment=os.environ if environment is None else environment
    repository=environment.get('GITHUB_REPOSITORY','')
    source=environment.get('GITHUB_SHA','')
    trusted=(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository)
        and SHA.fullmatch(source) and environment.get('GITHUB_REF')=='refs/heads/main'
        and environment.get('GITHUB_WORKFLOW_SHA')==source
        and environment.get('GITHUB_WORKFLOW_REF')==repository+'/.github/workflows/cd.yml@refs/heads/main'
        and environment.get('GITHUB_EVENT_NAME') in {'push','workflow_dispatch'}
        and environment.get('SOURCE_SHA',source)==source)
    if not trusted:return original
    requested=environment.get('COOKBOOK_VALIDATION_TIMEOUT',str(value))
    if requested!=str(value):raise ValueError('CD caller deadline differs from native delivery policy')
    subprocess.run(['git','-C',str(root),'ls-files','--error-unmatch','--',selected['definition']],
        check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
    if (subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True,timeout=10).strip()!=source
            or subprocess.check_output(['git','-C',str(root),'show','HEAD:'+selected['definition']],timeout=10)!=path.read_bytes()):
        raise ValueError('CD validation deadline differs from its exact tracked source')
    return value


def plan(root, config, environment=None):
    if config.get('recipe') != 'godot-game':
        raise ValueError('Godot topology requires the native game recipe')
    values = config.get('ci', {}).get('godot')
    if values is None:
        return {'version': 1, 'enrolled': False, 'requirements': [], 'units': {'include': []}, 'gates': {'include': []}}
    if (not isinstance(values, dict) or set(values) - {'unit_shards', 'unit_workers', 'build_timeout_minutes',
            'unit_timeout_minutes', 'host_timeout_minutes', 'unit_artifacts', 'gates', 'host_unit_name'}):
        raise ValueError('unsupported Godot CI topology declaration')
    runner = config.get('ci', {}).get('runner', 'ubuntu-24.04')
    if not isinstance(runner, str) or runner not in RUNNERS:
        raise ValueError('Godot runners must use the reviewed native Linux platforms')
    workers = values.get('unit_workers', 4)
    if workers != 4:
        raise ValueError('accepted parallel Godot coverage requires four workers')
    groups = unit_groups(config)
    native_unit = (any(value['runner'] not in {'godot', 'godot-check'} for value in config.get('tests', {}).get('unit', []))
        or any(value.get('test', {}).get('enabled', True) for value in config.get('components', {}).get('rust', []))
        or any(value.get('test_packages') for value in config.get('components', {}).get('go', []))
        or any(isinstance(value, dict) and (value.get('test') or value.get('test_paths')) for value in config.get('node', {}).get('packages', [])))
    native_name = values.get('host_unit_name', 'test-native') if native_unit else None
    if native_name is not None and (not isinstance(native_name, str) or not NAME.fullmatch(native_name) or native_name in groups):
        raise ValueError('native host unit coverage requires one distinct literal group identity')
    if not native_unit and values.get('host_unit_name'):
        raise ValueError('native host unit identity cannot name absent assertions')
    shards = values.get('unit_shards', 1 if groups else 0)
    if type(shards) is not int or not 0 <= shards <= 8 or (groups and not 1 <= shards <= len(groups)) or (not groups and shards):
        raise ValueError('Godot shard count must cover nonempty actual native groups')
    artifacts = values.get('unit_artifacts', [])
    if not isinstance(artifacts, list) or len(artifacts) != len(set(artifacts)):
        raise ValueError('Godot unit inputs require distinct declared artifact paths')
    for path in artifacts:
        if root is not None:
            relative(root, path)
        elif not isinstance(path, str) or not path or Path(path).is_absolute() or '..' in Path(path).parts or '\\' in path:
            raise ValueError('Godot unit artifacts must stay inside the checkout')
    declared = {case_id(suite, entry): (suite, entry) for suite, entries in config.get('tests', {}).items() for entry in entries}
    if len(declared) != sum(len(entries) for entries in config.get('tests', {}).values()):
        raise ValueError('Godot coverage repeats an identical native assertion entry')
    requirements = [{'id': 'build', 'kind': 'build', 'job': 'Godot build', 'runner': runner,
        'timeout_minutes': budget(values.get('build_timeout_minutes', 30))}]
    for number in range(1, shards + 1):
        selected = partition(list(groups), f'{number}/{shards}')
        requirements.append({'id': f'unit-{number}-{shards}', 'kind': 'unit', 'job': f'Godot unit ({number}/{shards})',
            'runner': runner, 'timeout_minutes': budget(values.get('unit_timeout_minutes', 20)),
            'shard': f'{number}/{shards}', 'workers': workers, 'groups': selected,
            'cases': sorted(case for name in selected for case in groups[name]), 'artifacts': artifacts})
    gate_records = values.get('gates', [])
    if not isinstance(gate_records, list) or len(gate_records) > 16:
        raise ValueError('Godot auxiliary coverage requires bounded source records')
    assigned = set(case for value in requirements for case in value.get('cases', []))
    names = set()
    production = []
    for record in gate_records:
        allowed = {'name', 'runner', 'timeout_minutes', 'suite', 'tests', 'kind', 'entry', 'setup', 'packages', 'artifacts', 'browsers', 'xvfb', 'probe', 'virtual_display'}
        if not isinstance(record, dict) or set(record) - allowed or not {'name', 'timeout_minutes'} <= set(record):
            raise ValueError('invalid native auxiliary gate')
        name, kind = record['name'], record.get('kind', 'test')
        gate_runner = record.get('runner', runner)
        if (not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9-]{1,63}', name)
                or name in names or not isinstance(gate_runner, str) or gate_runner not in RUNNERS
                or kind not in {'test', 'capability', 'production'}):
            raise ValueError('auxiliary gates require distinct names, native runners and coverage roles')
        names.add(name)
        public_fields=set(record)&{'browsers','xvfb','probe'}
        if public_fields and kind!='production':
            raise ValueError('public production launcher policy must not alter product or capability coverage')
        browsers=record.get('browsers',['chromium'])
        if browsers not in [['chromium'],['chromium','firefox','webkit']]:
            raise ValueError('public browser coverage must retain its actual one or three native engines')
        xvfb=record.get('xvfb',[])
        if (not isinstance(xvfb,list) or len(xvfb)!=len(set(xvfb)) or not set(xvfb)<=set(browsers)
                or type(record.get('probe',False)) is not bool):
            raise ValueError('public display and probe policy requires literal covered browsers and a boolean')
        setup = record.get('setup', 'native')
        if setup not in {'native', 'godot', 'browser'}:
            raise ValueError('native gate setup must select a shared SDK capability')
        if ('virtual_display' in record and (type(record['virtual_display']) is not bool
                or kind != 'capability' or setup != 'browser')):
            raise ValueError('virtual display must explicitly select a browser capability assertion')
        selected = record.get('tests', [])
        cases = []
        suite = record.get('suite', 'security' if kind == 'capability' else 'e2e')
        if suite not in GROUPS:
            raise ValueError('native gate must select an actual test group')
        if kind == 'test':
            if 'entry' in record or not isinstance(selected, list) or not selected or len(selected) != len(set(selected)):
                raise ValueError('product auxiliary gates select distinct existing written assertions')
            for selected_name in selected:
                found = [key for key, (group, entry) in declared.items()
                         if group == suite and entry.get('name', entry['path']) == selected_name]
                if len(found) != 1 or found[0] in assigned:
                    raise ValueError('auxiliary coverage must name one previously unassigned native assertion')
                cases += found
            assigned.update(cases)
        else:
            entry = record.get('entry')
            if (selected or not isinstance(entry, dict) or set(entry) - {'runner', 'path', 'args', 'timeout', 'name', 'python_environment'}
                    or entry.get('runner') not in {'python', 'node', 'bun', 'bash'}):
                raise ValueError('platform and production gates require one real owned native assertion program')
            path = Path(entry.get('path', ''))
            if (not path.parts or path.is_absolute() or '..' in path.parts or '\\' in str(path)
                    or not any(part in {'tests', 'test'} for part in path.parts)):
                raise ValueError('platform and production assertions belong to owned tests')
            if root is not None and not relative(root, path.as_posix(), exists=True).is_file():
                raise ValueError('native assertion entry must be a source program')
            arguments = entry.get('args', [])
            if not isinstance(arguments, list) or any(not isinstance(value, str) or '\0' in value for value in arguments):
                raise ValueError('native assertion arguments must be literal strings')
            timeout = entry.get('timeout', 120 if kind == 'capability' else 360)
            if type(timeout) is not int or not 1 <= timeout <= 3600:
                raise ValueError('native assertions require bounded original deadlines')
            cases = [case_id(suite, entry)]
        inputs = record.get('artifacts', [])
        packages = record.get('packages', [])
        if not isinstance(inputs, list) or not isinstance(packages, list):
            raise ValueError('native gate inputs and packages must be literal native paths')
        for path in [*inputs, *packages]:
            if not isinstance(path, str) or not path or Path(path).is_absolute() or '..' in Path(path).parts or '\\' in path:
                raise ValueError('native gate dependencies must stay inside the checkout')
        known_packages = {value if isinstance(value, str) else value['directory'] for value in config.get('node', {}).get('packages', [])}
        if not set(packages) <= known_packages:
            raise ValueError('native gate packages must select declared locked Node components')
        value = {'id': 'gate-' + name, 'name': name, 'kind': kind, 'job': 'Godot gate (' + name + ')',
            'runner': gate_runner, 'timeout_minutes': budget(record['timeout_minutes']),
            'suite': suite, 'cases': sorted(cases), 'setup': setup, 'packages': packages, 'artifacts': inputs}
        if kind != 'test':
            value['entry'] = entry
        if 'virtual_display' in record:
            value['virtual_display'] = record['virtual_display']
        if kind=='production':
            value.update({key:record[key] for key in ['browsers','xvfb','probe'] if key in record})
        (production if kind == 'production' else requirements).append(value)
    host_cases = sorted(set(declared) - assigned)
    host_groups = ['lint', 'unit', 'security', *[name for name in ['integration', 'e2e'] if config.get('tests', {}).get(name)]]
    requirements.append({'id': 'host', 'kind': 'host', 'job': 'Godot host', 'runner': runner,
        'timeout_minutes': effective_host_budget(root,config,
            budget(values.get('host_timeout_minutes', config.get('ci', {}).get('timeout_minutes', 30))),environment),
        'groups': host_groups, 'cases': host_cases, 'native_unit_name': native_name})
    result = {'version': 1, 'enrolled': True, 'requirements': requirements, 'unit_inventory': sorted([*groups, *([native_name] if native_name else [])]),
        'godot_unit_inventory': sorted(groups),
        'host_groups': host_groups, 'written_case_inventory': sorted(declared),
        'units': {'include': [row for row in requirements if row['kind'] == 'unit']},
        'gates': {'include': [row for row in requirements if row['kind'] in {'test', 'capability'}]},
        'production': {'include': production}}
    if len(json.dumps(result)) > 200000:
        raise ValueError('Godot topology exceeds the bounded Actions matrix output')
    return result

# Partial worker evidence deliberately has a different schema from a full host
# acceptance. Only the host join may issue the complete product test receipt.
import argparse
import os
import shutil
import subprocess
import sys
import tomllib

import dependencies

COOKBOOK = Path(__file__).resolve().parents[1]
TOOLS = dependencies.tools_root()
CONTEXT = ('GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_WORKFLOW_REF', 'GITHUB_WORKFLOW_SHA')
HASH = re.compile(r'[a-f0-9]{64}\Z')
MAX_FILES = 20000
MAX_FILE = 2 << 30
MAX_TOTAL = 16 << 30


def read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 << 20:
        raise ValueError('Godot evidence must be bounded regular JSON')
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError('Godot evidence must be an object')
    return value


def digest(path):
    path = Path(path)
    if (path.is_symlink() or not path.is_file() or not 0 <= path.stat().st_size <= MAX_FILE
            or any(parent.is_symlink() for parent in path.parents)):
        raise ValueError('Godot transport requires bounded unlinked regular files')
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def configuration_hash(root):
    return hashlib.sha256(subprocess.check_output(['git', '-C', str(root), 'show', 'HEAD:cookbook.toml'], timeout=30)).hexdigest()


def context_matches(context, environment, job=None):
    if (not isinstance(context, dict) or any(not environment.get(key) or context.get(key) != environment[key] for key in CONTEXT)
            or (job is not None and context.get('GITHUB_JOB') != job)):
        raise ValueError('Godot evidence belongs to another Actions execution or job')


def execution_job(entry):
    return {'build': 'build', 'unit': 'units', 'test': 'gates', 'capability': 'gates', 'production': 'production'}[entry['kind']]


def run_identity(environment):
    repository, run, attempt = (environment.get(key, '') for key in CONTEXT[:3])
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)
            or not re.fullmatch(r'[1-9][0-9]{0,23}', run) or not re.fullmatch(r'[1-9][0-9]{0,8}', attempt)):
        raise ValueError('Godot orchestration requires the actual current repository/run/attempt')
    return repository, run, attempt


def artifact_name(source, entry, environment):
    _, run, attempt = run_identity(environment)
    return f'cookbook-godot-{source}-{entry["id"]}-{run}-{attempt}'


def github(repository, suffix):
    return json.loads(subprocess.check_output(['gh', 'api', 'repos/' + repository + suffix], timeout=30, text=True))


def validate_jobs(requirements, jobs):
    if not isinstance(jobs, dict) or jobs.get('total_count') != len(jobs.get('jobs', [])):
        raise ValueError('complete same-attempt job records are required')
    accepted = []
    for entry in requirements:
        matches = [row for row in jobs['jobs'] if row.get('name', '').endswith(entry['job'])]
        if (len(matches) != 1 or matches[0].get('status') != 'completed' or matches[0].get('conclusion') != 'success'
                or entry['runner'] not in matches[0].get('labels', [])):
            raise ValueError('every declared Godot job must succeed on its exact runner')
        accepted.append({'entry': entry['id'], 'id': matches[0]['id'], 'name': matches[0]['name']})
    return accepted


def declaration_entry(root, config, entry_id):
    matches = [row for row in plan(root, config)['requirements'] if row['id'] == entry_id and row['kind'] != 'host']
    if len(matches) != 1:
        raise ValueError('select one exact source-declared Godot worker')
    return matches[0]


def validate_partial(proof, entry, source, tools, environment, canonical_hash, *, tools_revision=None):
    dependencies.validate_binding(proof, tools, tools_revision)
    if proof.get('providers') is not None:
        import deployment
        providers = deployment.validate_providers(proof['providers'])
        if providers['cookbook_revision'] != tools or providers['tools_revision'] != proof['tools_commit']:
            raise ValueError('Godot worker providers differ from its exact SDK source identities')
    if (not isinstance(proof, dict) or proof.get('version') != 1 or proof.get('coverage') != 'godot-partial'
            or proof.get('complete_host') is not False or proof.get('status') != 'passed'
            or proof.get('source_commit') != source or proof.get('cookbook_commit') != tools
            or proof.get('configuration_sha256') != canonical_hash or proof.get('entry') != entry
            or proof.get('repository') != environment.get('GITHUB_REPOSITORY')
            or not isinstance(proof.get('source_tree'), str) or not SHA.fullmatch(proof['source_tree'])):
        raise ValueError('Godot partial evidence differs from exact source, cookbook or declared coverage')
    context_matches(proof.get('execution_context'), environment, execution_job(entry))
    expected_cases = entry.get('cases', [])
    if proof.get('executed_cases') != expected_cases:
        raise ValueError('Godot partial evidence omitted or invented written assertion coverage')
    build = proof.get('build')
    if (not isinstance(build, dict) or not HASH.fullmatch(build.get('identity', ''))
            or not isinstance(build.get('artifacts'), dict) or not build['artifacts']):
        raise ValueError('Godot worker requires the accepted build identity and artifact closure')
    provenance = build.get('provenance', {})
    if (provenance.get('source_commit') != source or provenance.get('source_tree') != proof['source_tree']
            or provenance.get('tracked_source_clean') is not True or provenance.get('input_identity') != build['identity']):
        raise ValueError('Godot build provenance changed')
    context_matches(provenance.get('execution_context'), environment, 'build')
    if len(build['artifacts']) > MAX_FILES:
        raise ValueError('Godot artifact inventory exceeds its reviewed bound')
    for name, value in build['artifacts'].items():
        if (not isinstance(name, str) or not isinstance(value, str) or
                (not re.fullmatch(r'image:[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9][a-z0-9/._-]*:' + re.escape(source), name)
                 if name.startswith('image:') else not HASH.fullmatch(value))):
            raise ValueError('Godot artifact identity is not a bounded exact source image or file digest')
        if name.startswith('image:') and not re.fullmatch(r'sha256:[a-f0-9]{64}', value):
            raise ValueError('Godot image requires its immutable native identity')
    if entry['kind'] != 'build':
        receipt = proof.get('receipt', {})
        if (receipt.get('tested_commit') != source or receipt.get('source_commit') != source
                or receipt.get('source_tree') != proof['source_tree'] or receipt.get('tracked_source_clean') is not True
                or receipt.get('input_identity') != proof.get('worker_identity') or not HASH.fullmatch(proof.get('worker_identity', ''))):
            raise ValueError('Godot worker did not prove its actually executed source inputs')
        context_matches(receipt.get('execution_context'), environment, execution_job(entry))
    return proof


def allowed_output(name, config, root):
    import godot_game
    return any(name == path or name.startswith(path.rstrip('/') + '/') for path in godot_game.verification_scope(root, config)['artifacts'])


def export_packet(root, proof, config=None):
    import godot_game
    stage = root / '.artifacts/godot-export'
    godot_game.helper('clean').clean(root, ['.artifacts/godot-export'])
    stage.mkdir(parents=True, mode=0o700)
    if proof['entry']['kind'] == 'build':
        files = {name: value for name, value in proof['build']['artifacts'].items() if not name.startswith('image:')}
        if not files or len(files) > MAX_FILES or sum(relative(root, name, exists=True).stat().st_size for name in files) > MAX_TOTAL:
            raise ValueError('Godot build transport exceeds the reviewed file/byte budget')
        modes = {}
        for name, expected in files.items():
            path = relative(root, name, exists=True)
            if digest(path) != expected:
                raise ValueError('Godot build output changed before transport')
            destination = relative(stage / 'payload', name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
            modes[name] = bool(path.stat().st_mode & 0o111)
        proof['executables'] = modes
    if proof.get('browser_evidence') is not None:
        if config is None:
            raise ValueError('browser report export requires its actual configured source scope')
        import godot_browser_evidence
        godot_browser_evidence.stage(root, stage, config, proof)
    (stage / 'godot-proof.json').write_text(json.dumps(proof, indent=2) + '\n')
    return stage


def packet(root, directory, config, source, tools, entry, environment):
    import ci
    proof = validate_partial(read_json(Path(directory) / 'godot-proof.json'), entry, source, tools, environment, configuration_hash(root))
    if proof['source_tree'] != ci.git(root, 'rev-parse', 'HEAD^{tree}'):
        raise ValueError('Godot packet belongs to another source tree')
    expected_files = {'godot-proof.json'}
    import godot_browser_evidence
    expected_files.update(godot_browser_evidence.packet(config, proof, directory))
    if entry['kind'] == 'build':
        files = {name: value for name, value in proof['build']['artifacts'].items() if not name.startswith('image:')}
        if (not files or len(files) > MAX_FILES or set(proof.get('executables', {})) != set(files)
                or any(type(value) is not bool for value in proof['executables'].values())):
            raise ValueError('Godot packet omitted executable metadata or complete build outputs')
        total = 0
        for name, value in files.items():
            if not HASH.fullmatch(value) or not allowed_output(name, config, root):
                raise ValueError('Godot packet contains an undeclared output')
            incoming = relative(Path(directory) / 'payload', name, exists=True)
            total += incoming.stat().st_size
            if digest(incoming) != value:
                raise ValueError('Godot packet differs from its accepted output bytes')
            expected_files.add('payload/' + name)
        if total > MAX_TOTAL:
            raise ValueError('Godot packet exceeds the reviewed transfer budget')
    actual = set()
    for path in Path(directory).rglob('*'):
        if path.is_symlink():
            raise ValueError('Godot transport may not contain links')
        if path.is_file():
            actual.add(path.relative_to(directory).as_posix())
    if actual != expected_files:
        raise ValueError('Godot transport contains missing or unexpected files')
    return proof


def download(root, source, environment, entries):
    import godot_game
    repository, run, _ = run_identity(environment)
    directory = root / '.artifacts/godot-downloads'
    godot_game.helper('clean').clean(root, ['.artifacts/godot-downloads'])
    directory.mkdir(parents=True, mode=0o700)
    for entry in entries:
        destination = directory / artifact_name(source, entry, environment)
        subprocess.run(['gh', 'run', 'download', run, '--repo', repository, '--name', destination.name,
                        '--dir', str(destination)], check=True, timeout=300)
    return directory


def import_build(root, config, source_directory, proof):
    import ci
    import godot_game
    if Path.cwd().resolve() != root.resolve():
        raise ValueError('Godot build import requires the current exact checkout')
    files = {name: value for name, value in proof['build']['artifacts'].items() if not name.startswith('image:')}
    images = {name: value for name, value in proof['build']['artifacts'].items() if name.startswith('image:')}
    declared = ci.verification().resolve_images(godot_game.verification_scope(root, config), proof['source_commit'])
    if images or declared:
        ci.identity(root, proof['source_commit'], proof['cookbook_commit'])
        if proof.get('configuration_sha256') != configuration_hash(root):
            raise ValueError('image import requires its admitted exact source configuration')
        if {name[6:] for name in images} != set(declared):
            raise ValueError('image import requires the complete declared current-source image scope')
        for name, expected in files.items():
            if not allowed_output(name, config, root) or digest(relative(Path(source_directory) / 'payload', name, exists=True)) != expected:
                raise ValueError('image import requires all admitted build bytes before effects')
    for name in files:
        if not allowed_output(name, config, root):
            raise ValueError('Godot output is outside the exact declared build scope')
        destination = relative(root, name)
        if destination.exists() and digest(destination) != files[name]:
            raise ValueError('Godot import would replace different existing build bytes')
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(relative(Path(source_directory) / 'payload', name, exists=True), destination)
        destination.chmod(0o755 if proof['executables'][name] else 0o644)
    if images:
        import deployment
        import image_fixtures
        import image_import_cache
        release = deployment.verify(root, dict(os.environ, APP_VERSION=proof['source_commit']))
        names = ['.artifacts/release/images/' + name + '.oci.tar'
                 for name, artifact in release['artifacts'].items() if artifact['kind'] == 'image']
        fixture = image_fixtures.declaration(root, config)
        if fixture:
            identities = image_fixtures.verify(root, config, dict(os.environ, SOURCE_COMMIT=proof['source_commit']))
            if any(images.get('image:' + ref) != identity for ref, identity in identities.items()):
                raise ValueError('fixture differs from the admitted exact source build identity')
            names += [(fixture['directory'] / (name + '.oci.tar')).relative_to(root).as_posix()
                      for name in fixture['names']]
        if any(name not in files for name in names):
            raise ValueError('image import requires every admitted OCI archive')
        archives = [{'path': name, 'sha256': files[name]} for name in names]
        image_import_cache.reload(root, proof['source_commit'], archives,
            {name[6:]: value for name, value in images.items()}, dict(os.environ), timeout=600)
    ci.verification().write(Path('.artifacts/verification/build.json'), proof['build'])


def matrix_path(root):
    return root / '.artifacts/godot-host-matrix.json'


def prepare_host(root, config, source, tools, *, directory=None, jobs=None):
    declaration = plan(root, config)
    if not declaration.get('enrolled'):
        raise ValueError('Godot full validation requires an explicit source-owned CI topology')
    entries = [row for row in declaration['requirements'] if row['kind'] != 'host']
    environment = dict(os.environ)
    directory = Path(directory) if directory is not None else download(root, source, environment, entries)
    if {path.name for path in directory.iterdir()} != {artifact_name(source, row, environment) for row in entries}:
        raise ValueError('Godot matrix artifacts are incomplete or contain unexpected executions')
    proofs = [packet(root, directory / artifact_name(source, row, environment), config, source, tools, row, environment) for row in entries]
    build = next(value for value in proofs if value['entry']['kind'] == 'build')
    if any(value['build'] != build['build'] for value in proofs):
        raise ValueError('Godot workers accepted different product builds')
    repository, run, attempt = run_identity(environment)
    jobs = jobs if jobs is not None else github(repository, f'/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100')
    accepted = validate_jobs(entries, jobs)
    import godot_browser_evidence
    report_join = godot_browser_evidence.join_plan(root, config, proofs, directory,
        lambda proof: artifact_name(source, proof['entry'], environment))
    import_build(root, config, directory / artifact_name(source, build['entry'], environment), build)
    godot_browser_evidence.join(report_join)
    godot_browser_evidence.verify_host(root, config, proofs)
    providers = build.get('providers')
    if any(value.get('providers') != providers for value in proofs):
        raise ValueError('Godot workers selected different immutable release providers')
    matrix = {'version': 1, 'source_commit': source, **dependencies.binding(tools), 'providers': providers, 'requirements': declaration['requirements'],
              'host_groups': declaration['host_groups'], 'unit_inventory': declaration['unit_inventory'], 'proofs': proofs, 'jobs': accepted}
    matrix_path(root).write_text(json.dumps(matrix, indent=2) + '\n')
    os.environ['COOKBOOK_GODOT_HOST'] = str(matrix_path(root))
    os.environ.pop('COOKBOOK_GODOT_ENTRY', None)
    return matrix


def host_matrix(root, config, environment):
    expected = str(matrix_path(root))
    if environment.get('COOKBOOK_GODOT_HOST') != expected:
        raise ValueError('Godot host join requires its checkout-bound accepted matrix')
    matrix = read_json(matrix_path(root))
    declaration = plan(root, config)
    if matrix.get('requirements') != declaration['requirements'] or matrix.get('host_groups') != declaration['host_groups']:
        raise ValueError('Godot host join source contract changed')
    for proof in matrix.get('proofs', []):
        entry = declaration_entry(root, config, proof.get('entry', {}).get('id'))
        validate_partial(proof, entry, matrix['source_commit'], matrix['cookbook_commit'], environment, configuration_hash(root))
    import godot_browser_evidence
    godot_browser_evidence.verify_host(root, config, matrix.get('proofs', []))
    required = [row for row in declaration['requirements'] if row['kind'] != 'host']
    if (len(matrix.get('proofs', [])) != len(required) or
            {proof['entry']['id'] for proof in matrix.get('proofs', [])} != {row['id'] for row in required}):
        raise ValueError('Godot host join omitted partial worker coverage')
    return matrix


def revalidate_host_build(root, config, matrix, environment):
    import godot_game
    import ci
    if Path.cwd().resolve() != root.resolve():
        raise ValueError('Godot build validation requires the current exact checkout')
    if matrix != host_matrix(root, config, environment):
        raise ValueError('Godot host build join differs from the imported matrix')
    accepted = next(value['build'] for value in matrix['proofs'] if value['entry']['kind'] == 'build')
    scope = godot_game.verification_scope(root, config)
    if ci.verification().artifacts(scope) != accepted['artifacts']:
        raise ValueError('Godot imported products changed before host verification')
    ci.verification().run_command('build', 'build', scope,
        sdk_environment.python_entry(str(COOKBOOK / 'cookbook/godot_ci.py'), 'validate-import', '--source', matrix['source_commit'], '--cookbook-ref', matrix['cookbook_commit']),
        Path('.artifacts/verification'))


def selected_entries(root, config, environment, suite):
    declared = config.get('tests', {}).get(suite, [])
    if environment.get('COOKBOOK_GODOT_HOST'):
        matrix = host_matrix(root, config, environment)
        host = next(value for value in matrix['requirements'] if value['kind'] == 'host')
        return [entry for entry in declared if case_id(suite, entry) in host['cases']]
    if environment.get('COOKBOOK_GODOT_ENTRY'):
        row = declaration_entry(root, config, environment['COOKBOOK_GODOT_ENTRY'])
        if row['kind'] == 'build' or (row['kind'] == 'unit' and suite != 'unit') or (row['kind'] != 'unit' and row['suite'] != suite):
            raise ValueError('partial Godot worker may execute only its declared assertion group')
        if row['kind'] == 'capability':
            return [row['entry']]
        return [entry for entry in declared if case_id(suite, entry) in row['cases']]
    return declared


def require_units(root, config, environment):
    matrix = host_matrix(root, config, environment)
    declaration = plan(root, config)
    actual = [value['entry'] for value in matrix['proofs'] if value['entry']['kind'] == 'unit']
    expected = declaration['units']['include']
    if sorted(actual, key=lambda row: row['id']) != sorted(expected, key=lambda row: row['id']):
        raise ValueError('Godot whole-unit acceptance requires every declared disjoint shard')
    return declaration['unit_inventory']



def require_group(root, config, environment, suite):
    """A whole group passes only after its local checks and exact delegated union."""
    matrix=host_matrix(root,config,environment)
    if suite not in matrix['host_groups']:
        raise ValueError('host join may accept only an implemented whole test group')
    expected={case_id(suite,entry) for entry in config.get('tests',{}).get(suite,[])}
    host=next(row for row in matrix['requirements'] if row['kind']=='host')
    local=expected & set(host['cases'])
    delegated=[]
    for proof in matrix['proofs']:
        entry=proof['entry']
        if (entry['kind']=='unit' and suite=='unit') or (entry['kind']=='test' and entry['suite']==suite):
            if proof.get('executed_cases')!=entry['cases']:
                raise ValueError('delegated group evidence differs from its written assertion scope')
            delegated.extend(proof['executed_cases'])
    if len(delegated)!=len(set(delegated)) or local & set(delegated) or local | set(delegated)!=expected:
        raise ValueError('whole group requires the exact disjoint local and delegated assertion union')
    if suite=='unit':require_units(root,config,environment)
    return {'group':suite,'local_cases':sorted(local),'delegated_cases':sorted(delegated),'status':'passed'}

def finalize_host(host, matrix):
    dependencies.validate_record(host)
    dependencies.validate_record(matrix)
    if not isinstance(matrix, dict) or any(host.get(key) != matrix.get(key)
            for key in dependencies.BINDING_KEYS):
        raise ValueError('Godot host and matrix must retain the same separate SDK source identities')
    if host.get('providers') != matrix.get('providers'):
        raise ValueError('Godot host and matrix accepted different release providers')
    if (not isinstance(matrix, dict) or host.get('status') != 'passed' or host.get('build_verified') is not True
            or host.get('tests_verified') is not True or set(host.get('test_groups', [])) != set(matrix['host_groups'])
            or host.get('coverage') == 'godot-partial' or host.get('source_commit') != matrix['source_commit']
            or host.get('cookbook_commit') != matrix['cookbook_commit']):
        raise ValueError('Godot partial coverage cannot replace complete host validation')
    import godot_browser_evidence
    godot_browser_evidence.verify_final(host, matrix['proofs'])
    return {**host, 'coverage': 'complete-host-and-godot-matrix', 'godot_matrix': matrix}


def validate_acceptance(matrix, config, jobs, source, tools, environment, configuration_sha256, *, root=None, host=None, tools_revision=None):
    """Validate the matrix, and final report hashes when an aggregate is supplied."""
    dependencies.validate_binding(matrix, tools, tools_revision)
    declaration = plan(root, config, environment)
    if (not isinstance(matrix, dict) or matrix.get('version') != 1 or not declaration.get('enrolled')
            or matrix.get('source_commit') != source or matrix.get('cookbook_commit') != tools
            or matrix.get('requirements') != declaration['requirements'] or matrix.get('host_groups') != declaration['host_groups']
            or matrix.get('unit_inventory') != declaration['unit_inventory']):
        raise ValueError('Godot accepted matrix differs from the exact candidate contract')
    entries = [row for row in declaration['requirements'] if row['kind'] != 'host']
    proofs = matrix.get('proofs', [])
    if not isinstance(proofs, list) or len(proofs) != len(entries):
        raise ValueError('Godot matrix omitted or repeated partial worker coverage')
    by_id = {proof.get('entry', {}).get('id'): proof for proof in proofs if isinstance(proof, dict)}
    if len(by_id) != len(proofs) or set(by_id) != {entry['id'] for entry in entries}:
        raise ValueError('Godot matrix changed declared worker coverage')
    for entry in entries:
        validate_partial(by_id[entry['id']], entry, source, tools, environment, configuration_sha256,
                         tools_revision=tools_revision)
        import godot_browser_evidence
        godot_browser_evidence.validate(config, by_id[entry['id']])
    build = by_id['build']['build']
    if any(value['build'] != build for value in proofs):
        raise ValueError('Godot accepted matrix combines different product builds')
    accepted = validate_jobs(entries, jobs)
    if accepted != matrix.get('jobs'):
        raise ValueError('Godot acceptance belongs to different actual Actions jobs')
    if host is not None:
        finalize_host(host, matrix)
    return accepted



def worker_tools(root, config, environment):
    import godot_game as game
    if environment.get('COOKBOOK_GODOT_HOST'):
        matrix = host_matrix(root, config, environment)
        entry = next(value for value in matrix['requirements'] if value['kind'] == 'host')
    else:
        entry = declaration_entry(root, config, environment.get('COOKBOOK_GODOT_ENTRY'))
    if entry['kind'] == 'build':
        return game.required_tools(root, config, 'full')
    tools = {'python'}
    bindings = game.godot_addon_bindings.declaration(root, config)
    if bindings and (entry['kind'] in {'unit', 'host'} or entry.get('setup') == 'godot'):
        tools.add(bindings['tool'])
    if entry.get('setup')=='browser' or (entry['kind']=='host' and config.get('components',{}).get('playwright')):
        tools.add('node')
    if entry['kind'] == 'host':
        tools.update(game.required_tools(root, config, 'security'))
        tools.update(game.required_tools(root, config, 'fast'))
        if any(value.get('test_packages') for value in config.get('components',{}).get('go',[])):
            tools.add('go')
    packages = game.packages(config) if entry['kind'] == 'host' else [value for value in game.packages(config) if value['directory'] in entry.get('packages', [])]
    for package in packages:
        tools.add('bun' if package.get('manager', 'npm') == 'bun' else 'node')
    entries = [value for suite in config.get('tests', {}) for value in selected_entries(root, config, environment, suite)] if entry['kind'] == 'host' else [
        value for suite, records in config.get('tests', {}).items() for value in records if case_id(suite, value) in entry.get('cases', [])]
    if entry.get('entry'):
        entries.append(entry['entry'])
    for value in entries:
        if value['runner'] in {'node', 'node-test', 'bun', 'bun-test'}:
            tools.add(value['runner'].split('-')[0])
    pins = tomllib.loads((root / 'mise.toml').read_text()).get('tools', {})
    if not tools <= pins.keys():
        raise ValueError('Godot worker SDK capabilities require exact caller pins')
    return sorted(tools)

def setup_worker(root, config, environment, entry):
    """Install only the SDK capabilities used by this declared worker, explicitly."""
    import godot_game as game
    from configuration import godot_pins
    import node_component
    role = entry['kind']
    if role == 'build':
        game.setup(root, config, environment)
        return
    packages = game.packages(config) if role == 'host' else [value for value in game.packages(config) if value['directory'] in entry.get('packages', [])]
    tools = {'python'}
    bindings = game.godot_addon_bindings.declaration(root, config)
    if bindings and (role in {'unit', 'host'} or entry.get('setup') == 'godot'):
        tools.add(bindings['tool'])
    if entry.get('setup')=='browser' or (role=='host' and config.get('components',{}).get('playwright')):
        tools.add('node')
    if role == 'host':
        tools.update(game.required_tools(root, config, 'security'))
        tools.update(game.required_tools(root, config, 'fast'))
        if any(value.get('test_packages') for value in config.get('components',{}).get('go',[])):
            tools.add('go')
    for value in packages:
        tools.add('bun' if value.get('manager', 'npm') == 'bun' else 'node')
    native_entries = [value for suite in config.get('tests', {}) for value in selected_entries(root, config, environment, suite)] if role == 'host' else [
        value for suite, entries in config.get('tests', {}).items() for value in entries if case_id(suite, value) in entry.get('cases', [])]
    if entry.get('entry'):
        native_entries.append(entry['entry'])
    for value in native_entries:
        if value['runner'] in {'node', 'node-test', 'bun', 'bun-test'}:
            tools.add(value['runner'].split('-')[0])
    pins = tomllib.loads((root / 'mise.toml').read_text()).get('tools', {})
    if not tools <= pins.keys():
        raise ValueError('Godot worker SDK capabilities require exact caller pins')
    game.run(['mise', 'install', *sorted(tools)], root, environment)
    game.komizo_sdk.setup(root,config,environment)
    sdk = lambda argv, directory, env: game.run(argv, directory, env)
    node_component.install(root, packages, environment, sdk)
    if role=='host' and config.get('components',{}).get('playwright'):
        import playwright_sdk
        playwright_sdk.setup(root,config,environment,game.run)
    if config.get('components', {}).get('python'):
        sdk(['python3', str(COOKBOOK / 'cookbook/python_component.py'), 'setup'], root, environment)
    if role == 'host' and config.get('components',{}).get('image_security'):
        import godot_security
        godot_security.setup(root,config,environment,game.run)
    if role == 'host':
        import rust_component
        components = [value for value in game.cargo_records(config, environment) if value.get('test', {}).get('enabled', True)]
        if components:
            rust_component.setup(root, components, environment, sdk)
        if any(value.get('test_packages') for value in config.get('components',{}).get('go',[])):
            import go_component
            go_component.setup(root,config,environment,game.run)
    if entry.get('setup') == 'browser' and config.get('components', {}).get('browser'):
        import godot_browser
        godot_browser.setup(root, config, environment, capability=entry['kind']=='capability')
    elif entry.get('setup') == 'browser':
        import playwright_sdk
        playwright_sdk.install(root,config,environment,game.run,[package['directory'] for package in packages],['chromium'])
        for package in packages:
            directory = relative(root, package['directory'], exists=True)
            browser = subprocess.check_output(['node', '--input-type=module', '-e',
                'import {chromium} from "playwright"; console.log(chromium.executablePath())'],
                cwd=directory, env=sdk_environment.command_environment(['node'], root, environment), text=True, timeout=30).strip()
            if not Path(browser).is_absolute() or not Path(browser).is_file():
                raise ValueError('locked Playwright did not resolve an installed Chromium executable')
            environment['BROWSER_EXECUTABLE'] = browser
            os.environ['BROWSER_EXECUTABLE'] = browser
    if entry.get('virtual_display'):
        import virtual_display
        virtual_display.setup(root, environment, game.run)
    if role not in {'unit', 'host'} and entry.get('setup') != 'godot':
        return
    directory, settings = game.project(root, config)
    godot = godot_pins(root)
    (root / '.artifacts/tmp').mkdir(parents=True, exist_ok=True)
    (root / '.artifacts/.gdignore').touch()
    engine = sdk_environment.python_entry(str(TOOLS / 'helpers/godot-setup.py'), '--version', godot['GODOT_VERSION'], '--expected-version', godot['GODOT_EXPECTED_VERSION'], '--binary-checksum', godot['GODOT_BINARY_CHECKSUM'], '--root', str(root / '.artifacts/godot'), '--origin', game.GODOT_RELEASE_ORIGIN)
    if role == 'host':
        engine += ['--templates-checksum', godot['GODOT_TEMPLATES_CHECKSUM'], '--templates']
    game.run(engine, root, environment)
    if (directory / 'gdam.toml').exists():
        game.runtime_configuration().prepare(root, (directory / 'gdam.toml').relative_to(root), 'gdam')
        game.helper('godot-addons').restore(directory, taint_markers=settings.get('addon_taint_markers', []),
            bindings=game.godot_addon_bindings.installed(root, config, environment))
    game.import_native(root, config, environment)


def collect_partial(root, config, source, tools, entry, build):
    import ci
    import godot_game
    tree = ci.identity(root, source, tools)
    verify = ci.verification()
    proof = {'version': 1, 'coverage': 'godot-partial', 'complete_host': False, 'status': 'passed',
             'repository': os.environ.get('GITHUB_REPOSITORY'), 'source_commit': source, 'source_tree': tree,
             **dependencies.binding(tools), 'configuration_sha256': configuration_hash(root), 'entry': entry,
             'execution_context': verify.execution_context(), 'executed_cases': entry.get('cases', []), 'build': build}
    if (root / '.artifacts/release/release.json').exists():
        import deployment
        proof['providers'] = deployment.verify(root, dict(os.environ, APP_VERSION=source))['providers']
        if proof['providers'] != deployment.provider_provenance():
            raise ValueError('Godot worker selected providers differ from the accepted packet')
    if entry['kind'] == 'build':
        scope = godot_game.verification_scope(root, config)
        if build.get('identity') != verify.identity(scope) or build.get('artifacts') != verify.artifacts(scope):
            raise ValueError('Godot build outputs or inputs changed before transport')
    else:
        suite = 'unit' if entry['kind'] == 'unit' else entry['suite']
        proof['receipt'] = read_json(root / '.artifacts/verification' / (suite + '.json'))
        proof['worker_identity'] = verify.identity(godot_game.verification_scope(root, config))
    validate_partial(proof, entry, source, tools, os.environ, configuration_hash(root))
    import godot_browser_evidence
    godot_browser_evidence.capture(root, config, proof)
    return proof


def execute_entry(root, source, tools, entry_id):
    import ci
    import godot_game
    from configuration import load
    config = load(root)
    entry = declaration_entry(root, config, entry_id)
    os.environ.update(godot_game.configure_environment(root, config))
    if entry['kind'] == 'build':
        subprocess.run(sdk_environment.python_entry(COOKBOOK / 'cookbook/dispatch.py', 'build'), cwd=root, env=os.environ, check=True)
        build = read_json(root / '.artifacts/verification/build.json')
    else:
        build = read_json(root / '.artifacts/godot-imported-build.json')
        # Verify every transferred product still matches before native assertions.
        if ci.verification().artifacts(godot_game.verification_scope(root, config)) != build['artifacts']:
            raise ValueError('Godot worker products differ from the declared builder')
        suite = 'unit' if entry['kind'] == 'unit' else entry['suite']
        os.environ['COOKBOOK_GODOT_ENTRY'] = entry_id
        selection = None
        if entry['kind'] == 'unit':
            if Path.cwd().resolve() != root.resolve() or not SHA.fullmatch(source):
                raise ValueError('native selection diagnostics require the bound exact source checkout')
            _, run, attempt = run_identity(os.environ)
            inventory = plan(root, config)['godot_unit_inventory']
            selected = partition(inventory, entry['shard'])
            if entry['workers'] != 4 or selected != entry['groups']:
                raise ValueError('native selection diagnostics require the actual native shard and workers')
            selection = {'source_commit': source, 'run_id': run, 'attempt': attempt,
                         'shard': entry['shard'], 'workers': entry['workers'], 'inventory': inventory,
                         'selected': selected, 'status': 'running'}
            ci.verification().write(Path('.artifacts/godot-test-selection.json'), selection)
        completed = False
        try:
            subprocess.run(sdk_environment.python_entry(COOKBOOK / 'cookbook/dispatch.py', 'test:' + suite), cwd=root, env=os.environ, check=True)
            completed = True
        finally:
            if selection is not None:
                selection['status'] = 'passed' if completed else 'failed'
                ci.verification().write(Path('.artifacts/godot-test-selection.json'), selection)
    export_packet(root, collect_partial(root, config, source, tools, entry, build), config)


def run_entry(root, source, tools, entry_id):
    import ci
    import godot_game
    from configuration import load
    config = load(root)
    ci.identity(root, source, tools)
    entry = declaration_entry(root, config, entry_id)
    os.environ.update(COOKBOOK_STAGE='full', COOKBOOK_ROOT=str(COOKBOOK), COOKBOOK_GODOT_ENTRY=entry_id)
    os.environ.pop('COOKBOOK_GODOT_HOST', None)
    os.environ.update(godot_game.configure_environment(root, config))
    if entry['kind'] != 'build':
        build_entry = declaration_entry(root, config, 'build')
        directory = download(root, source, os.environ, [build_entry])
        source_directory = directory / artifact_name(source, build_entry, os.environ)
        proof = packet(root, source_directory, config, source, tools, build_entry, os.environ)
        repository, run, attempt = run_identity(os.environ)
        validate_jobs([build_entry], github(repository, f'/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100'))
        import_build(root, config, source_directory, proof)
        (root / '.artifacts/godot-imported-build.json').write_text(json.dumps(proof['build']) + '\n')
        godot_game.helper('clean').clean(root, ['.artifacts/godot-downloads'])
    setup_worker(root, config, dict(os.environ), entry)
    subprocess.run(sdk_environment.python_entry(COOKBOOK / 'cookbook/godot_ci.py', 'execute',
                    '--source', source, '--cookbook-ref', tools, '--entry', entry_id), cwd=root, env=os.environ, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['plan', 'run', 'execute', 'validate-import'])
    parser.add_argument('--source', required=True)
    parser.add_argument('--cookbook-ref', required=True)
    parser.add_argument('--entry')
    arguments = parser.parse_args()
    root = Path.cwd().resolve()
    try:
        import ci
        from configuration import load
        ci.identity(root, arguments.source, arguments.cookbook_ref)
        config = load(root)
        if arguments.operation == 'plan':
            declaration = plan(root, config)
            if not declaration.get('enrolled'):
                raise ValueError('Godot full CI is not enrolled for this retained source-only client')
            if os.environ.get('GITHUB_OUTPUT'):
                with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                    for name in ['units', 'gates']:
                        output.write(name + '=' + json.dumps(declaration[name], separators=(',', ':')) + '\n')
                        output.write('has-' + name + '=' + ('true' if declaration[name]['include'] else 'false') + '\n')
                    for name, kind in [('build', 'build'), ('host', 'host')]:
                        value = next(row for row in declaration['requirements'] if row['kind'] == kind)
                        output.write(name + '-runner=' + value['runner'] + '\n')
                        output.write(name + '-timeout=' + str(value['timeout_minutes']) + '\n')
            print(json.dumps(declaration, indent=2))
        elif arguments.operation == 'validate-import':
            import godot_game
            matrix = host_matrix(root, config, os.environ)
            build = next(value['build'] for value in matrix['proofs'] if value['entry']['kind'] == 'build')
            godot_game.validate_outputs(root, config, dict(os.environ))
            if ci.verification().artifacts(godot_game.verification_scope(root, config)) != build['artifacts']:
                raise ValueError('Godot imported product bytes changed during validation')
        elif not arguments.entry:
            raise ValueError('Godot workers require one exact declared entry')
        elif arguments.operation == 'execute':
            execute_entry(root, arguments.source, arguments.cookbook_ref, arguments.entry)
        else:
            run_entry(root, arguments.source, arguments.cookbook_ref, arguments.entry)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
