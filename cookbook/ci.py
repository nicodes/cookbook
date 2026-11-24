#!/usr/bin/env python3
"""Run the shared recipe gate and bind its result to this exact Actions execution."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

import sdk_environment

from configuration import load, relative
from generate import check_entrypoint, check_workflows
import recipe
import orchestration
import rust_tests

import advisories
import dependencies

COOKBOOK = Path(__file__).resolve().parents[1]
SHA = re.compile(r'[a-f0-9]{40}\Z')


def git(root, *arguments):
    return subprocess.check_output(['git', '-C', str(root), *arguments], text=True, timeout=30).strip()


def identity(root, source, tools):
    if not SHA.fullmatch(source) or not SHA.fullmatch(tools):
        raise ValueError('CI requires exact source and cookbook commits')
    if git(root, 'rev-parse', 'HEAD') != source or git(COOKBOOK, 'rev-parse', 'HEAD') != tools:
        raise ValueError('checked-out source or cookbook differs from the requested revision')
    for checkout in [root, COOKBOOK]:
        if subprocess.run(['git', '-C', str(checkout), 'diff', '--quiet', 'HEAD', '--'], timeout=30).returncode:
            raise ValueError('CI requires clean tracked source and cookbook code')
    import sdk_source
    sdk_source.verify(COOKBOOK, tools, repository='nicodes/cookbook')
    dependencies.tools_root()
    check_entrypoint(root, tools)
    check_workflows(root, tools)
    return git(root, 'rev-parse', 'HEAD^{tree}')


def verification():
    return dependencies.helper('verification')


def artifacts(root, config, declaration, environment=None):
    """Reinspect imported bytes without assigning names in the caller's engine."""
    verify = verification()
    if not declaration.get('images'):
        return verify.artifacts(declaration)
    import images
    source = git(root, 'rev-parse', 'HEAD')
    values = dict(os.environ if environment is None else environment)
    values.update(APP_VERSION=source, SOURCE_COMMIT=source)
    accepted = images.runtime_identities(root, config, values)
    if set(accepted) != set(verify.resolve_images(declaration, source)):
        raise ValueError('native runtime identities differ from the complete declared build scope')
    return verify.artifacts(declaration, image_identities=accepted)


def proof(root, source, tools, config, *, imported_groups=(), coverage=None):
    tree = identity(root, source, tools)
    directory = root / '.artifacts/verification'
    build = json.loads((directory / 'build.json').read_text())
    test = json.loads((directory / 'test.json').read_text())
    verify = verification()
    context = verify.execution_context()
    for record in [build['provenance'], test]:
        if (record.get('source_commit') != source or record.get('source_tree') != tree
                or record.get('tracked_source_clean') is not True
                or record.get('execution_context') != context):
            raise ValueError('build/test receipt belongs to another source or execution')
    suites = set(recipe.execution_test_suites(root, config)) - set(imported_groups)
    if set(test.get('test_receipts', {})) != suites or test.get('tested_commit') != source:
        raise ValueError('not every implemented group passed in this execution')
    if build['identity'] != test.get('input_identity') or build['provenance']['input_identity'] != build['identity']:
        raise ValueError('build and tests did not accept the same source inputs')
    declaration = recipe.adapter(config).verification_scope(root, config)
    if verify.identity(declaration) != build['identity'] or artifacts(root, config, declaration) != build['artifacts']:
        raise ValueError('source or artifact bytes changed after acceptance')
    evidence = {}
    providers = None
    if (root / '.artifacts/release/release.json').exists():
        import deployment
        providers = deployment.verify(root, dict(os.environ, APP_VERSION=source))['providers']
        if providers != deployment.provider_provenance():
            raise ValueError('tested release providers differ from the selected immutable SDK sources')
    module = recipe.adapter(config)
    for name in module.evidence_paths(root, config) if hasattr(module, 'evidence_paths') else []:
        path = relative(root, name, exists=True)
        if not path.is_file():
            raise ValueError('post-test evidence must be a regular declared artifact')
        evidence[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {'version': 1, 'repository': os.environ.get('GITHUB_REPOSITORY'),
            'source_commit': source, 'source_tree': tree, **dependencies.binding(tools),
            'recipe': config['recipe'],
                        'configuration_sha256': hashlib.sha256(subprocess.check_output(
                ['git', '-C', str(root), 'show', 'HEAD:cookbook.toml'], timeout=30)).hexdigest(),
            'status': 'passed', 'tracked_source_clean': True,
            'build_verified': True, 'tests_verified': True, 'test_groups': sorted(suites),
            'execution_context': context, 'artifacts': build['artifacts'], 'evidence': evidence,
            **({'providers': providers} if providers is not None else {}),
            **({'coverage': coverage} if coverage is not None else {}),
            **({'advisories': 'skip'} if advisories.skipped() else {})}


def run(root, mode, source, tools, release_bump='', head='', base=''):
    if mode != 'full':
        raise ValueError('CI requires complete validation')
    if (head or base) and (not SHA.fullmatch(head) or not SHA.fullmatch(base)):
        raise ValueError('merge validation requires exact head and base identities together')
    config = load(root)
    model = orchestration.model(config)
    identity(root, source, tools)
    os.environ['COOKBOOK_STAGE'] = mode
    # Godot's XDG_DATA_HOME also selects the rootless image store. Establish it
    # before import; native joins must first adopt authenticated version/date.
    configured = model['route'] == 'godot'
    if configured:
        recipe.configure_environment(root, config)
    matrix = None
    matrix_module = orchestration.matrix_module(config)
    if matrix_module is not None:
        if configured:
            matrix = matrix_module.prepare_host(root, config, source, tools, local_only=True)
        else:
            matrix = matrix_module.prepare_host(root, config, source, tools)
    if not configured:
        recipe.configure_environment(root, config)
    if release_bump:
        from publication import identity as release_identity
        release_identity(root, config, release_bump)
    environment = {**os.environ, 'COOKBOOK_ROOT': str(COOKBOOK), 'COOKBOOK_STAGE': mode}
    environment.pop(recipe.IMPORTED_NATIVE_GROUPS, None)
    environment.pop(rust_tests.IMPORTED_UNITS, None)
    imported = {}
    cargo_names = []
    cargo_producer = None
    if model['route'] == 'native':
        import rust_cli

        imported = matrix_module.imported_checks(matrix)
        cargo_producer, cargo_names = matrix_module.imported_cargo(matrix, config)
        if imported:
            environment[recipe.IMPORTED_NATIVE_GROUPS] = ','.join(sorted(imported))
        environment.pop(rust_cli.LOCKFILES_AUDITED, None)
        if matrix is not None and matrix.get('lockfile_audit'):
            environment[rust_cli.LOCKFILES_AUDITED] = 'true'
    imported_build = matrix_module is not None and hasattr(matrix_module, 'revalidate_host_build')
    commands = ['setup', *([] if imported_build else ['build']), 'test']
    receipt = root / '.artifacts/cookbook-validation.json'
    receipt.unlink(missing_ok=True)
    for command in commands:
        if command == 'test' and imported_build:
            matrix_module.revalidate_host_build(root, config, matrix, environment)
        values = environment
        if command == 'test' and cargo_names:
            values = {**environment, rust_tests.IMPORTED_UNITS: json.dumps(cargo_names)}
        subprocess.run(sdk_environment.python_entry(COOKBOOK / 'cookbook/dispatch.py', command), cwd=root, env=values, check=True)
    if configured:
        matrix = matrix_module.prepare_host(root, config, source, tools, primary=matrix['proofs'][0])
        for suite in matrix['host_groups']:
            matrix_module.require_group(root, config, environment, suite)
    identity(root, source, tools)
    candidate = head or os.environ.get('COOKBOOK_CANDIDATE_COMMIT') or source
    if not SHA.fullmatch(candidate):
        raise ValueError('candidate provenance requires an exact source head')
    module = recipe.adapter(config)
    if hasattr(module, 'post_test_evidence'):
        module.post_test_evidence(root, config, environment, candidate)
    accepted = proof(root, source, tools, config, imported_groups=imported)
    if imported:
        for producer in imported.values():
            matrix_module.validate_imported_outputs(root, config, accepted, producer)
        accepted['test_groups'] = sorted([*accepted['test_groups'], *imported])
    if cargo_producer is not None:
        if cargo_producer not in imported.values():
            matrix_module.validate_imported_outputs(root, config, accepted, cargo_producer)
        accepted['reused_native_cargo'] = cargo_names
    if matrix_module is not None:
        accepted = matrix_module.finalize_host(accepted, matrix)
    if head:
        accepted.update(head=head, base=base)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps(accepted, indent=2) + '\n')
    paths = [*module.artifact_paths(root, config), '.artifacts/verification/', '.artifacts/cookbook-validation.json']
    for name in paths:
        relative(root, name)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('artifact-paths<<COOKBOOK_ARTIFACT_PATHS\n' + '\n'.join(paths) + '\nCOOKBOOK_ARTIFACT_PATHS\n')


PHASES = ('setup', 'build', 'test')
# Environment that selects one worker or the host join of the multi-job Godot matrix.
MATRIX_SELECTORS = ('COOKBOOK_GODOT_ENTRY', 'COOKBOOK_GODOT_HOST')
# Auxiliary-gate fields that need a worker's own setup or display, which one process does not provide.
WORKER_GATE_FIELDS = {'setup', 'packages', 'artifacts', 'browsers', 'xvfb', 'probe', 'virtual_display', 'tools', 'entry'}


def single_job_support(config, model):
    """Refuse declarations whose coverage only the multi-job matrix provides."""
    if model['route'] == 'native':
        raise ValueError('Rust CLI recipes are not yet supported in single-job mode')
    if model['route'] == 'godot':
        for gate in (config.get('ci', {}).get('godot') or {}).get('gates', []):
            if gate.get('kind', 'test') != 'test' or WORKER_GATE_FIELDS & set(gate):
                raise ValueError('Godot capability, production and worker-bound gates are not yet supported in single-job mode')


def run_single_job(root, mode, phase, source, tools, release_bump='', head='', base=''):
    """Run one phase of the whole recipe in this process: no matrix, workers or job proofs."""
    if mode != 'full':
        raise ValueError('CI requires complete validation')
    if phase not in PHASES:
        raise ValueError('single-job mode requires the setup, build or test phase')
    if (head or base) and (not SHA.fullmatch(head) or not SHA.fullmatch(base)):
        raise ValueError('merge validation requires exact head and base identities together')
    config = load(root)
    model = orchestration.model(config)
    identity(root, source, tools)
    single_job_support(config, model)
    if any(os.environ.get(name) for name in MATRIX_SELECTORS):
        raise ValueError('single-job mode owns every group and cannot run as a selected matrix worker')
    os.environ['COOKBOOK_STAGE'] = mode
    # Godot's XDG_DATA_HOME also selects the rootless image store, so this precedes every phase.
    recipe.configure_environment(root, config)
    environment = {**os.environ, 'COOKBOOK_ROOT': str(COOKBOOK), 'COOKBOOK_STAGE': mode}
    environment.pop(recipe.IMPORTED_NATIVE_GROUPS, None)
    environment.pop(rust_tests.IMPORTED_UNITS, None)
    receipt = root / '.artifacts/cookbook-validation.json'
    if phase != 'build':
        receipt.unlink(missing_ok=True)
    if phase == 'setup' and release_bump:
        from publication import identity as release_identity
        release_identity(root, config, release_bump)
    if phase != 'test':
        subprocess.run(sdk_environment.python_entry(COOKBOOK / 'cookbook/dispatch.py', phase), cwd=root, env=environment, check=True)
        return
    subprocess.run(sdk_environment.python_entry(COOKBOOK / 'cookbook/dispatch.py', 'test'), cwd=root, env=environment, check=True)
    identity(root, source, tools)
    candidate = head or os.environ.get('COOKBOOK_CANDIDATE_COMMIT') or source
    if not SHA.fullmatch(candidate):
        raise ValueError('candidate provenance requires an exact source head')
    module = recipe.adapter(config)
    if hasattr(module, 'post_test_evidence'):
        module.post_test_evidence(root, config, environment, candidate)
    accepted = proof(root, source, tools, config, coverage='single-job')
    if head:
        accepted.update(head=head, base=base)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps(accepted, indent=2) + '\n')
    paths = [*module.artifact_paths(root, config), '.artifacts/verification/', '.artifacts/cookbook-validation.json']
    for name in paths:
        relative(root, name)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('artifact-paths<<COOKBOOK_ARTIFACT_PATHS\n' + '\n'.join(paths) + '\nCOOKBOOK_ARTIFACT_PATHS\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['full'], required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--cookbook-ref', required=True)
    parser.add_argument('--release-bump', default='')
    parser.add_argument('--head', default='')
    parser.add_argument('--base', default='')
    parser.add_argument('--single-job', action='store_true', help='run the whole recipe in this one job, one phase per call')
    parser.add_argument('--phase', choices=PHASES, help='the single-job phase to run')
    args = parser.parse_args()
    if args.single_job != (args.phase is not None):
        parser.error('--single-job and --phase must be given together')
    try:
        if args.single_job:
            run_single_job(Path.cwd().resolve(), args.mode, args.phase, args.source, args.cookbook_ref,
                           args.release_bump, args.head, args.base)
        else:
            run(Path.cwd().resolve(), args.mode, args.source, args.cookbook_ref, args.release_bump, args.head, args.base)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
