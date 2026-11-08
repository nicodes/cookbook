#!/usr/bin/env python3
"""Run the shared recipe gate and bind its result to this exact Actions execution."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

import sdk_environment

from configuration import load, relative
from generate import check_entrypoint, workflow_files
import recipe
import orchestration

import dependencies

COOKBOOK = Path(__file__).resolve().parents[1]
TOOLS = dependencies.tools_root()
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
    for name, expected in workflow_files(root, tools).items():
        if (root / name).read_text() != expected:
            raise ValueError('workflow differs from the pinned recipe generator: ' + name)
    return git(root, 'rev-parse', 'HEAD^{tree}')


def verification():
    return dependencies.helper('verification')


def proof(root, source, tools, config):
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
    suites = set(recipe.test_suites(root, config))
    if set(test.get('test_receipts', {})) != suites or test.get('tested_commit') != source:
        raise ValueError('not every implemented group passed in this execution')
    if build['identity'] != test.get('input_identity') or build['provenance']['input_identity'] != build['identity']:
        raise ValueError('build and tests did not accept the same source inputs')
    declaration = recipe.adapter(config).verification_scope(root, config)
    if verify.identity(declaration) != build['identity'] or verify.artifacts(declaration) != build['artifacts']:
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
            **({'providers': providers} if providers is not None else {})}


def run(root, mode, source, tools, release_bump='', head='', base=''):
    if (head or base) and (not SHA.fullmatch(head) or not SHA.fullmatch(base)):
        raise ValueError('merge validation requires exact head and base identities together')
    config = load(root)
    model = orchestration.model(config)
    identity(root, source, tools)
    os.environ['COOKBOOK_STAGE'] = mode
    matrix = None
    matrix_module = None
    if mode == 'full':
        matrix_module = orchestration.matrix_module(config)
        if matrix_module is not None:
            matrix = matrix_module.prepare_host(root, config, source, tools)
    recipe.configure_environment(root, config)
    if release_bump:
        if mode != 'full':
            raise ValueError('release acceptance requires full validation')
        from publication import identity as release_identity
        release_identity(root, config, release_bump)
    environment = {**os.environ, 'COOKBOOK_ROOT': str(COOKBOOK), 'COOKBOOK_STAGE': mode}
    imported_build = mode == 'full' and matrix_module is not None and hasattr(matrix_module, 'revalidate_host_build')
    commands = ['setup', 'test:lint'] if mode == 'fast' else ['setup', *([] if imported_build else ['build']), 'test']
    receipt = root / '.artifacts/cookbook-validation.json'
    receipt.unlink(missing_ok=True)
    for command in commands:
        if command == 'test' and imported_build:
            matrix_module.revalidate_host_build(root, config, matrix, environment)
        subprocess.run(sdk_environment.python_entry(COOKBOOK / 'cookbook/dispatch.py', command), cwd=root, env=environment, check=True)
    identity(root, source, tools)
    if mode == 'fast':
        command = ['python3', str(TOOLS / 'helpers/source-secrets.py')]
        if config.get('security', {}).get('fixtures'):
            command += ['--fixtures', str(relative(root, config['security']['fixtures'], exists=True))]
        subprocess.run(command, cwd=root, env=environment, check=True)
        return
    candidate = head or os.environ.get('COOKBOOK_CANDIDATE_COMMIT') or source
    if not SHA.fullmatch(candidate):
        raise ValueError('candidate provenance requires an exact source head')
    module = recipe.adapter(config)
    if hasattr(module, 'post_test_evidence'):
        module.post_test_evidence(root, config, environment, candidate)
    accepted = proof(root, source, tools, config)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['fast', 'full'], required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--cookbook-ref', required=True)
    parser.add_argument('--release-bump', default='')
    parser.add_argument('--head', default='')
    parser.add_argument('--base', default='')
    args = parser.parse_args()
    try:
        run(Path.cwd().resolve(), args.mode, args.source, args.cookbook_ref, args.release_bump, args.head, args.base)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
