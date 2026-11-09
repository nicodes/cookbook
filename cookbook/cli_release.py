#!/usr/bin/env python3
"""Release a closed native CLI payload after complete same-run source validation."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import tomllib

import ci
from configuration import load, ci_settings, relative
import native_ci
import native_extras
import orchestration
import rust_cli

import dependencies

COOKBOOK = Path(__file__).resolve().parents[1]
TOOLS = dependencies.tools_root()
spec = importlib.util.spec_from_file_location('cookbook_cli_github_release', TOOLS / 'helpers/github_release.py')
github_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(github_release)


def policy(config):
    values = config.get('publication', {})
    allowed = {'tag_prefix', 'initial', 'initial_version', 'version_identity', 'cargo_manifest', 'acceptance_timeout', 'validation_timeout', 'acceptance', 'attestation_platforms'}
    if config.get('recipe') != 'rust-cli' or not isinstance(values, dict) or set(values)-allowed:
        raise ValueError('CLI publication requires native semantic release policy')
    result = {'tag_prefix': 'v', 'initial': 'first', 'version_identity': 'number', 'acceptance_timeout': 10, **values}
    if (not re.fullmatch(r'(?:[a-z][a-z0-9-]*-)?v', result['tag_prefix'])
            or result['initial'] not in {'first', 'bump'} or result['version_identity'] not in {'number', 'tag'}
            or type(result['acceptance_timeout']) is not int or not 1 <= result['acceptance_timeout'] <= 120):
        raise ValueError('invalid CLI release identity or existing acceptance budget')
    if 'initial_version' in result and (not isinstance(result['initial_version'], str)
            or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', result['initial_version'])):
        raise ValueError('CLI initial version must be a stable semantic version')
    if 'validation_timeout' in result and (type(result['validation_timeout']) is not int or not 1 <= result['validation_timeout'] <= 180):
        raise ValueError('CLI full release validation must retain a bounded source deadline')
    if 'cargo_manifest' in result:
        value = result['cargo_manifest']
        if not isinstance(value, str) or not value.endswith('Cargo.toml') or Path(value).is_absolute() or '..' in Path(value).parts:
            raise ValueError('CLI package version must select its native Cargo manifest')
    platforms = result.get('attestation_platforms', [])
    known = {value['platform'] + '/' + value['arch'] for value in config.get('package', {}).get('native_targets', [])}
    if (not isinstance(platforms, list) or len(platforms) != len(set(platforms))
            or any(not isinstance(value, str) or value not in known for value in platforms)):
        raise ValueError('release provenance must select actual declared native archive platforms')
    acceptance = result.get('acceptance')
    if acceptance is not None:
        if (not isinstance(acceptance, dict) or not {'checks', 'verification', 'files'} <= set(acceptance)
                or set(acceptance)-{'checks', 'verification', 'files', 'diagnostics'}):
            raise ValueError('CLI acceptance requires owned assertion checks, verification and exact evidence names')
        files = acceptance['files']
        if (not isinstance(files, list) or not files or len(files) > 16 or len(files) != len(set(files))
                or any(not isinstance(value, str) or value == 'receipt.json' or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*\.json', value) for value in files)):
            raise ValueError('CLI acceptance evidence requires distinct bounded native JSON filenames')
        diagnostics = acceptance.get('diagnostics', [])
        if (not isinstance(diagnostics, list) or len(diagnostics) > 16 or len(diagnostics) != len(set(diagnostics))
                or set(diagnostics) & set(files)
                or any(not isinstance(value, str) or value == 'receipt.json' or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*\.json', value) for value in diagnostics)):
            raise ValueError('release diagnostics require distinct explicit native JSON filenames')
        for phase in ['checks', 'verification']:
            entries = acceptance[phase]
            if not isinstance(entries, list) or not entries or len(entries) > 16:
                raise ValueError('CLI acceptance must execute real owned assertions and independent verification')
            for entry in entries:
                if (not isinstance(entry, dict) or set(entry)-{'runner', 'path', 'args', 'timeout', 'cwd'}
                        or entry.get('runner') != 'python' or type(entry.get('timeout', 300)) is not int
                        or not 1 <= entry.get('timeout', 300) <= 3600):
                    raise ValueError('release assertions require the pinned native Python runner and bounded deadline')
                path = Path(entry.get('path', ''))
                if (not path.parts or path.is_absolute() or '..' in path.parts or '\\' in str(path)
                        or not any(value in {'tests', 'test'} for value in path.parts)):
                    raise ValueError('release assertions belong to their owned test sources')
                arguments = entry.get('args', [])
                if not isinstance(arguments, list) or any(not isinstance(value, str) or '\0' in value for value in arguments):
                    raise ValueError('release assertions accept literal native data arguments')
    orchestration.contract(config)
    return result


def context(source, environment=None):
    values = environment if environment is not None else os.environ
    repository = values.get('GITHUB_REPOSITORY', '')
    github_release.validate_request(repository, source, 'v0.0.1', values)
    if values.get('GITHUB_WORKFLOW_REF') != repository + '/.github/workflows/release.yml@refs/heads/main':
        raise ValueError('CLI publication must retain its source release workflow identity')
    for name in ['GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT']:
        if not re.fullmatch(r'[1-9][0-9]*', values.get(name, '')):
            raise ValueError('CLI publication requires an identified Actions execution')
    return values


def select_version(root, config, bump, tags):
    values = policy(config)
    tags = list(tags)
    if values.get('initial_version') and not any(re.fullmatch(re.escape(values['tag_prefix']) + r'[0-9]+\.[0-9]+\.[0-9]+', value) for value in tags):
        tags.append(values['tag_prefix'] + values['initial_version'])
    number, tag = github_release.version(bump, values['tag_prefix'], tags, values['initial'])
    if tag in tags:
        raise ValueError('release tag already exists; refusing to replace it')
    if 'cargo_manifest' in values:
        manifest = tomllib.loads(relative(root, values['cargo_manifest'], exists=True).read_text())
        if manifest.get('package', {}).get('version') != number:
            raise ValueError('commit the native Cargo package version before releasing')
    return {'tag': tag, 'version': tag if values['version_identity'] == 'tag' else number,
            'date': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}


def identity(root, config, source, tools, bump):
    context(source)
    ci.identity(root, source, tools)
    subprocess.run(['git', 'fetch', '--tags', '--force'], cwd=root, check=True, timeout=60)
    tags = subprocess.check_output(['git', 'tag', '--list'], cwd=root, text=True, timeout=30).splitlines()
    result = select_version(root, config, bump, tags)
    # Never mutate a tag or release while planning. The publisher claims it atomically.
    github_release.absent(os.environ['GITHUB_REPOSITORY'], 'git/ref/tags/' + result['tag'])
    github_release.absent(os.environ['GITHUB_REPOSITORY'], 'releases/tags/' + result['tag'])
    return result


def read_proof(root):
    path = relative(root, '.artifacts/cookbook-validation.json', exists=True)
    if not path.is_file() or path.stat().st_size > 2_000_000:
        raise ValueError('CLI release proof must be a bounded regular file')
    return json.loads(path.read_text())


def accepted_payload(root, config, proof, source, tools, tag, environment=None, jobs=None, *, before_acceptance=False):
    dependencies.validate_binding(proof, tools)
    environment = context(source, environment)
    ci.identity(root, source, tools)
    values = policy(config)
    github_release.tag(tag)
    if not re.fullmatch(re.escape(values['tag_prefix']) + r'[0-9]+\.[0-9]+\.[0-9]+', tag):
        raise ValueError('CLI release tag differs from its native prefix')
    configuration_sha256 = hashlib.sha256(subprocess.check_output(['git', '-C', str(root), 'show', 'HEAD:cookbook.toml'], timeout=30)).hexdigest()
    if (proof.get('source_commit') != source or proof.get('source_tree') != ci.git(root, 'rev-parse', 'HEAD^{tree}')
            or proof.get('cookbook_commit') != tools or proof.get('recipe') != 'rust-cli'
            or proof.get('configuration_sha256') != configuration_sha256
            or proof.get('status') != 'passed' or proof.get('tracked_source_clean') is not True
            or proof.get('build_verified') is not True or proof.get('tests_verified') is not True
            or proof.get('test_groups') != orchestration.contract(config)['test_groups']
            or proof.get('coverage') != 'complete-host-and-native-matrix'):
        raise ValueError('CLI release requires complete exact-source host and native proof')
    native_ci.context_matches(proof.get('execution_context', {}), environment, job='validate')
    repository, run, attempt = native_ci.run_identity(environment)
    jobs = jobs if jobs is not None else native_ci.github(repository, f'/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100')
    native_ci.validate_acceptance(proof.get('native_matrix'), config, jobs, source, tools, environment, configuration_sha256)
    host = [value for value in jobs.get('jobs', []) if value.get('name', '').endswith('Make full')]
    if (len(host) != 1 or host[0].get('status') != 'completed' or host[0].get('conclusion') != 'success'
            or ci_settings(config)['runner'] not in host[0].get('labels', [])):
        raise ValueError('CLI release requires the actual successful complete host job')
    expected_version = tag if values['version_identity'] == 'tag' else tag[len(values['tag_prefix']):]
    native_proofs = proof['native_matrix']['proofs']
    if not native_proofs or any(value.get('version_identity') != expected_version for value in native_proofs):
        raise ValueError('native CLI archives were built with another release version')
    package, native = rust_cli.package_data(root, config), rust_cli.helper('native_cli')
    native_extras.verify_publication(root,config,proof,source,expected_version,root/'dist')
    names = [native.archive_name(package, value) for value in package['native_targets']]
    names += [*native_extras.names(config), 'checksums.txt']
    if package.get('release_metadata'):
        names.append('release.json')
    if len(names) != len(set(names)):
        raise ValueError('CLI release assets must have unique native names')
    expected = {}
    for name in names:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name):
            raise ValueError('CLI release assets require plain native names')
        path = relative(root, 'dist/' + name, exists=True)
        if not path.is_file():
            raise ValueError('CLI release assets must be regular files')
        digest = native.digest(path)
        if proof.get('artifacts', {}).get('dist/' + name) != digest:
            raise ValueError('CLI release bytes differ from the accepted full gate')
        expected[name] = digest
    if values.get('acceptance') and not before_acceptance:
        expected.update(accepted_evidence(root, config, source, tools, tag, environment, proof=proof, original=dict(expected), jobs=jobs))
    github_release.payload(root / 'dist', expected)
    for target in package['native_targets']:
        native.inspect_archive(root / 'dist' / native.archive_name(package, target), package, target)
    if (root / 'dist/checksums.txt').read_text() != native.checksums(root / 'dist', [name for name in names if name not in {'checksums.txt', 'release.json'}]):
        raise ValueError('CLI release checksums differ from the complete native archive set')
    if not before_acceptance:
        repository = environment['GITHUB_REPOSITORY']
        subjects=[native.archive_name(package,target) for target in package['native_targets']
            if target['platform']+'/'+target['arch'] in values.get('attestation_platforms',[])]
        subjects += [subject for name,(_component,platform) in native_extras.records(config).items()
            if platform in values.get('attestation_platforms',[]) for subject in [name, name + '.json']]
        for name in subjects:
            subprocess.run(['gh', 'attestation', 'verify', str(root / 'dist' / name),
                '--repo', repository, '--cert-identity', 'https://github.com/' + repository + '/.github/workflows/release.yml@refs/heads/main',
                '--cert-oidc-issuer', 'https://token.actions.githubusercontent.com', '--signer-digest', source,
                '--source-digest', source, '--source-ref', 'refs/heads/main', '--deny-self-hosted-runners'],
                check=True, timeout=300)
    return expected


def run_assertions(root, config, tag, source, phase, *, deadline):
    from native_tests import run_entries
    native = rust_cli.helper('native_cli')
    values = {'source': source, 'tag': tag, 'dist': str(root / 'dist'),
              'evidence': str(root / '.artifacts/cli-publication-evidence'),
              'binary': str(root / rust_cli.package_data(root, config)['output'])}
    entries = [{**entry, 'args': [native.render(value, values) for value in entry.get('args', [])]}
               for entry in policy(config)['acceptance'][phase]]
    for entry in entries:
        remaining = deadline - time.time()
        if remaining < 1:
            raise ValueError('product release acceptance exhausted its declared aggregate deadline')
        bounded = {**entry, 'timeout': min(entry.get('timeout', 300), remaining)}
        run_entries(root, {**config, 'tests': {'release': [bounded]}}, dict(os.environ), 'release')


def evidence_files(root, config):
    values = policy(config)['acceptance']['files']
    ordinary = {rust_cli.helper('native_cli').archive_name(config['package'], row) for row in config['package']['native_targets']}
    ordinary.update([*native_extras.names(config), 'checksums.txt', 'release.json'])
    if ordinary & set(values):
        raise ValueError('release evidence cannot replace an existing native asset')
    expected = {}
    for name in values:
        path = relative(root, '.artifacts/cli-publication-evidence/' + name, exists=True)
        if not path.is_file() or not 0 < path.stat().st_size <= 1 << 20:
            raise ValueError('release evidence requires bounded nonempty native JSON files')
        if not isinstance(json.loads(path.read_text()), dict):
            raise ValueError('release evidence must contain a native JSON object')
        expected[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return expected


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z', value):
        raise ValueError('release acceptance requires actual GitHub job timestamps')
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def authority_jobs(environment, source, *, jobs=None, completed):
    repository, run, attempt = native_ci.run_identity(environment)
    jobs = jobs if jobs is not None else native_ci.github(repository, f'/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100')
    rows = jobs.get('jobs')
    if not isinstance(rows, list) or jobs.get('total_count') != len(rows) or len(rows) > 100:
        raise ValueError('release acceptance requires the complete bounded current-attempt job set')
    selected = []
    for name in ['Attest accepted source archives', 'Verify product release']:
        matches = [row for row in rows if row.get('name', '').split(' / ')[-1] == name]
        if len(matches) != 1:
            raise ValueError('release acceptance requires one actual archive signer and product job')
        row = matches[0]
        if (type(row.get('id')) is not int or row['id'] <= 0 or row.get('head_sha') != source
                or str(row.get('run_id')) != run or 'ubuntu-24.04' not in row.get('labels', [])
                or 'self-hosted' in row.get('labels', [])):
            raise ValueError('release acceptance job differs from this hosted source run')
        if name == 'Attest accepted source archives' or completed:
            if row.get('status') != 'completed' or row.get('conclusion') != 'success':
                raise ValueError('release acceptance requires actual successful completed jobs')
            if timestamp(row.get('completed_at')) < timestamp(row.get('started_at')):
                raise ValueError('release acceptance job times are inconsistent')
        elif row.get('status') != 'in_progress' or row.get('conclusion') is not None:
            raise ValueError('product assertions must execute inside their actual running job')
        selected.append(row)
    archive, product = selected
    if timestamp(product.get('started_at')) < timestamp(archive.get('completed_at')):
        raise ValueError('product assertions must follow accepted archive attestation')
    return archive, product


def acceptance_binding(root, config, proof, source, tools, tag, environment, original, archive, product):
    return {'kind': 'cookbook.cli-acceptance', 'version': 2, 'status': 'passed',
        'source_commit': source, 'source_tree': proof['source_tree'], **dependencies.binding(tools), 'tag': tag,
        'configuration_sha256': proof['configuration_sha256'],
        'acceptance_sha256': hashlib.sha256(canonical(policy(config)['acceptance'])).hexdigest(),
        'proof_sha256': hashlib.sha256(canonical(proof)).hexdigest(), 'input_artifacts': original,
        'archive_job': {'id': archive['id'], 'started_at': archive['started_at']},
        'product_job': {'id': product['id'], 'started_at': product['started_at']},
        'execution_context': {**{key: environment[key] for key in native_ci.CONTEXT}, 'GITHUB_JOB': 'product-acceptance'}}


def acceptance_deadline(config, archive, product, *, completed):
    deadline = timestamp(archive['started_at']) + 60 * policy(config)['acceptance_timeout']
    if time.time() >= deadline or timestamp(product['started_at']) >= deadline:
        raise ValueError('product release acceptance exhausted its declared aggregate deadline')
    if completed and timestamp(product['completed_at']) > deadline:
        raise ValueError('product release acceptance completed after its declared aggregate deadline')
    return deadline


def accepted_evidence(root, config, source, tools, tag, environment, *, proof, original, jobs=None):
    """Admit data from the read-only job without executing any product program."""
    path = relative(root, '.artifacts/cli-publication-evidence/receipt.json', exists=True)
    receipt = native_ci.read_json(path, 65536)
    if path.read_bytes() != canonical(receipt):
        raise ValueError('release acceptance receipt must retain closed canonical bytes')
    expected = evidence_files(root, config)
    native_ci.context_matches(receipt.get('execution_context', {}), environment, job='product-acceptance')
    archive, product = authority_jobs(environment, source, jobs=jobs, completed=True)
    acceptance_deadline(config, archive, product, completed=True)
    binding = acceptance_binding(root, config, proof, source, tools, tag, environment, original, archive, product)
    if receipt != {**binding, 'files': expected}:
        raise ValueError('release evidence differs from the same-run accepted source, archives or product job')
    if {item.name for item in path.parent.iterdir()} != {*expected, 'receipt.json'}:
        raise ValueError('product acceptance artifact must contain only declared evidence and its receipt')
    for name, digest in expected.items():
        source_path = path.parent / name
        destination = root / 'dist' / name
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_file() or hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                raise ValueError('published release evidence differs from the accepted assertion bytes')
        else:
            shutil.copyfile(source_path, destination)
    return expected


def accept_domain(root, config, proof, source, tools, tag):
    environment = context(source)
    if environment.get('GITHUB_JOB') != 'product-acceptance':
        raise ValueError('owned release assertions require the separate read-only product job')
    original = accepted_payload(root, config, proof, source, tools, tag, before_acceptance=True)
    if not policy(config).get('acceptance'):
        raise ValueError('this CLI has no additional product release assertions')
    archive, product = authority_jobs(environment, source, completed=False)
    deadline = acceptance_deadline(config, archive, product, completed=False)
    directory = relative(root, '.artifacts/cli-publication-evidence')
    if directory.exists():
        raise ValueError('release assertion evidence must be fresh')
    directory.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    run_assertions(root, config, tag, source, 'checks', deadline=deadline)
    run_assertions(root, config, tag, source, 'verification', deadline=deadline)
    expected = evidence_files(root, config)
    # Assertions have no authority to replace the independently accepted inputs.
    after = accepted_payload(root, config, proof, source, tools, tag, before_acceptance=True)
    if after != original:
        raise ValueError('product assertions changed accepted native inputs')
    acceptance_deadline(config, archive, product, completed=False)
    receipt = {**acceptance_binding(root, config, proof, source, tools, tag, environment, original, archive, product), 'files': expected}
    (directory / 'receipt.json').write_bytes(canonical(receipt))
    ci.identity(root, source, tools)
    return expected


def verify_evidence_attestations(root, config, source, environment):
    repository = environment['GITHUB_REPOSITORY']
    for name in policy(config).get('acceptance', {}).get('files', []):
        subprocess.run(['gh', 'attestation', 'verify', str(root / 'dist' / name),
            '--repo', repository, '--cert-identity', 'https://github.com/' + repository + '/.github/workflows/release.yml@refs/heads/main',
            '--cert-oidc-issuer', 'https://token.actions.githubusercontent.com', '--signer-digest', source,
            '--source-digest', source, '--source-ref', 'refs/heads/main', '--deny-self-hosted-runners'], check=True, timeout=300)
    if policy(config).get('acceptance'):
        archive, product = authority_jobs(environment, source, completed=True)
        acceptance_deadline(config, archive, product, completed=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['identity', 'accept', 'acceptance', 'publish'])
    parser.add_argument('--source', required=True)
    parser.add_argument('--cookbook-ref', required=True)
    parser.add_argument('--bump', default='')
    parser.add_argument('--tag', default='')
    parser.add_argument('--before-acceptance', action='store_true')
    args = parser.parse_args()
    root = Path.cwd().resolve()
    try:
        config = load(root)
        if args.before_acceptance and args.operation != 'accept':
            raise ValueError('only pre-attestation acceptance may omit the additional release assertions')
        if args.operation == 'identity':
            values = identity(root, config, args.source, args.cookbook_ref, args.bump)
            if os.environ.get('GITHUB_OUTPUT'):
                with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                    for key, value in values.items():
                        output.write(key + '=' + value + '\n')
            print(json.dumps(values))
        elif args.operation == 'acceptance':
            print(json.dumps(accept_domain(root, config, read_proof(root), args.source, args.cookbook_ref, args.tag), sort_keys=True))
        else:
            expected = accepted_payload(root, config, read_proof(root), args.source, args.cookbook_ref, args.tag, before_acceptance=args.before_acceptance)
            if args.operation == 'publish':
                verify_evidence_attestations(root, config, args.source, os.environ)
                github_release.publish(root / 'dist', expected, args.tag, args.source, os.environ['GITHUB_REPOSITORY'])
            print(json.dumps(expected, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
