#!/usr/bin/env python3
"""Validate a requested candidate, merge current accepted source and request main CI."""
import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import tomllib
import urllib.error
import urllib.request
from configuration import ci_settings, validate_data
import orchestration
import mise_entrypoint

import dependencies

COOKBOOK = Path(__file__).resolve().parents[1]
TOOLS = dependencies.tools_root()
spec = importlib.util.spec_from_file_location('request_merge_candidate', TOOLS / 'helpers/merge-candidate.py')
candidate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(candidate)


def api(path, method='GET', data=None):
    if method == 'GET':
        return candidate.api(path)
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token:
        raise ValueError('authenticated GitHub access required')
    request = urllib.request.Request('https://api.github.com/' + path, method=method,
        data=json.dumps(data).encode(), headers={'Authorization': 'Bearer ' + token,
        'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
        'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'tools-request-merge'})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise ValueError('GitHub response exceeds its bound')
        return json.loads(body) if body else None


def dispatch_context(repository, environment=None):
    environment = environment or os.environ
    if (environment.get('GITHUB_REPOSITORY') != repository
            or environment.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
            or environment.get('GITHUB_REF') != 'refs/heads/main'
            or environment.get('GITHUB_WORKFLOW_REF') != repository + '/.github/workflows/request-merge.yml@refs/heads/main'):
        raise ValueError('request merge must run through the main-branch request-merge workflow')
    for key in ['GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT']:
        if not re.fullmatch('[1-9][0-9]*', environment.get(key, '')):
            raise ValueError('request merge requires an identified Actions execution')


def source_file(repository, revision, path):
    value = api(f'repos/{repository}/contents/{path}?ref={revision}')
    if (value.get('type') != 'file' or value.get('encoding') != 'base64'
            or value.get('size', 262145) > 262144 or value.get('target')):
        raise ValueError('merge recipe inputs must be bounded regular source files')
    return base64.b64decode(value['content'].replace('\n', ''), validate=True).decode('utf-8')


def status(repository, head, state, description):
    api(f'repos/{repository}/statuses/{candidate.exact_sha(head)}', 'POST', {
        'state': state, 'context': 'Full validation', 'description': description,
        'target_url': f'https://github.com/{repository}/actions/runs/{os.environ["GITHUB_RUN_ID"]}'})


def expected_candidate(receipt, head='', base=''):
    if head or base:
        if (not re.fullmatch('[a-f0-9]{40}', head) or not re.fullmatch('[a-f0-9]{40}', base)
                or receipt['head'] != head or receipt['base'] != base):
            raise ValueError('requested head or base moved since the dependency policy decision')


def snapshot(repository, number, expected_head='', expected_base=''):
    pull = api(f'repos/{repository}/pulls/{number}')
    revision = candidate.exact_sha(pull.get('merge_commit_sha'))
    commit = api(f'repos/{repository}/git/commits/{revision}')
    receipt = candidate.snapshot(repository, pull, commit)
    expected_candidate(receipt, expected_head, expected_base)
    source = source_file(repository, revision, 'cookbook.toml')
    declaration = validate_data(tomllib.loads(source))
    mise = source_file(repository, revision, 'mise.toml')
    model = orchestration.contract(declaration)
    identity = mise_entrypoint.matches(mise_entrypoint.decode(mise), declaration, source.encode(),
                                      test_groups=model['test_groups'])
    tools, groups = identity['revision'], identity['test_groups']
    accepted = api(f'repos/nicodes/cookbook/compare/{tools}...main')
    if accepted.get('status') not in {'ahead', 'identical'}:
        raise ValueError('recipe Cookbook pin must already be accepted on Cookbook main')
    dependency = dependencies.sdk_source.identity(source_file('nicodes/cookbook', tools, 'mise.toml'))
    accepted_tools = api(f'repos/nicodes/tools/compare/{dependency["revision"]}...main')
    if accepted_tools.get('status') not in {'ahead', 'identical'}:
        raise ValueError('selected Cookbook dependency must already be accepted on Tools main')
    receipt.update(**dependencies.binding(tools, dependency['revision']), test_groups=groups,
                   recipe=model['recipe'], declaration=declaration,
                   configuration_sha256=hashlib.sha256(source.encode()).hexdigest())
    settings = ci_settings(declaration)
    receipt.update(validation_runner=settings['runner'], validation_timeout=str(settings['timeout_minutes']))
    receipt['preview_environment'] = settings.get('preview_environment', '')
    status(repository, receipt['head'], 'pending', 'Validating current head with base ' + receipt['base'][:12])
    return receipt


def workflow_acceptance(receipt, proof, run, jobs, environment=None):
    environment = environment or os.environ
    candidate.tested_candidate(receipt, proof)
    dependencies.validate_binding(receipt, receipt['cookbook_commit'], receipt['tools_commit'])
    dependencies.validate_binding(proof, receipt['cookbook_commit'], receipt['tools_commit'])
    if proof.get('cookbook_commit') != receipt['cookbook_commit'] or proof.get('test_groups') != receipt['test_groups']:
        raise ValueError('validation used another cookbook or omitted test groups')
    declaration = receipt['declaration']
    model = orchestration.contract(declaration)
    if (proof.get('recipe') != model['recipe'] or receipt.get('recipe') != model['recipe']
            or receipt['test_groups'] != model['test_groups']
            or proof.get('configuration_sha256') != receipt['configuration_sha256']
            or proof.get('coverage') in {'native-partial', 'godot-partial'}):
        raise ValueError('validation differs from the exact candidate recipe declaration')
    context = proof.get('execution_context', {})
    for key in ['GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_WORKFLOW_REF']:
        if context.get(key) != environment.get(key):
            raise ValueError('candidate proof belongs to another workflow execution')
    if (str(run.get('id')) != environment['GITHUB_RUN_ID']
            or str(run.get('run_attempt')) != environment['GITHUB_RUN_ATTEMPT']
            or run.get('event') != 'workflow_dispatch' or run.get('head_branch') != 'main'
            or run.get('path') != '.github/workflows/request-merge.yml'
            or run.get('repository', {}).get('full_name') != receipt['repository']):
        raise ValueError('validation run is not this trusted main-branch merge request')
    if jobs.get('total_count') != len(jobs.get('jobs', [])):
        raise ValueError('complete current-run jobs are required')
    completed = [job for job in jobs['jobs'] if not job['name'].endswith('Merge validated candidate')]
    snapshots = [job for job in completed if job['name'].endswith('Snapshot current candidate')]
    hosts = [job for job in completed if job['name'].endswith('Make full')]
    if (len(snapshots) != 1 or len(hosts) != 1
            or any(job.get('conclusion') != 'success' for job in snapshots + hosts)
            or any(job.get('conclusion') not in {'success', 'skipped'} for job in completed)):
        raise ValueError('all snapshot and full validation jobs must actually succeed')
    module = orchestration.matrix_module(declaration)
    namespace = 'native_matrix' if model['route'] == 'native' else 'godot_matrix'
    if module is not None:
        expected_coverage = 'complete-host-and-' + ('native' if model['route'] == 'native' else 'godot') + '-matrix'
        if proof.get('coverage') != expected_coverage:
            raise ValueError('native acceptance requires complete host and platform coverage together')
        module.validate_acceptance(proof.get(namespace), declaration, jobs, receipt['candidate'],
                                   receipt['cookbook_commit'], environment, receipt['configuration_sha256'],
                                   tools_revision=receipt['tools_commit'])
    elif any(name in proof for name in ['native_matrix', 'godot_matrix']):
        raise ValueError('host-only recipe cannot accept unrelated partial matrix evidence')


def finish(receipt, proof, validation_result):
    repository = receipt['repository']
    if validation_result != 'success':
        status(repository, receipt['head'], 'failure', 'Full validation did not pass; merge refused')
        raise ValueError('full validation failed, was skipped or was cancelled')
    run_id = os.environ['GITHUB_RUN_ID']
    run = api(f'repos/{repository}/actions/runs/{run_id}')
    jobs = api(f'repos/{repository}/actions/runs/{run_id}/jobs?filter=latest&per_page=100')
    workflow_acceptance(receipt, proof, run, jobs)
    pull = api(f'repos/{repository}/pulls/{receipt["pull_request"]}')
    base = api(f'repos/{repository}/branches/main')['commit']['sha']
    candidate.current_candidate(receipt, pull, base)
    # GitHub enforces native repository rules and atomically checks the PR head.
    # The base is rechecked immediately before requesting the merge. Its accepted
    # tree is checked again before any downstream validation/delivery dispatch.
    status(repository, receipt['head'], 'success', 'Full validation passed against base ' + base[:12])
    merged = api(f'repos/{repository}/pulls/{receipt["pull_request"]}/merge', 'PUT',
                 {'sha': receipt['head'], 'merge_method': 'squash'})
    if merged.get('merged') is not True:
        raise ValueError('GitHub did not merge the accepted pull request')
    source = candidate.exact_sha(merged.get('sha'))
    commit = api(f'repos/{repository}/git/commits/{source}')
    if commit['tree']['sha'] != receipt['tree'] or [p['sha'] for p in commit['parents']] != [receipt['base']]:
        status(repository, receipt['head'], 'error', 'Merged tree changed; downstream dispatch refused')
        raise ValueError('merge result differs from the validated candidate; no downstream dispatch')
    # Token-created merges do not ordinarily trigger push workflows. Exact final
    # source CI is explicit; candidate bytes are never relabeled as final source.
    final_workflow = 'cd.yml' if receipt.get('declaration', {}).get('delivery') else 'ci.yml'
    api(f'repos/{repository}/actions/workflows/{final_workflow}/dispatches', 'POST',
        {'ref': 'main', 'inputs': {'source': source, 'cookbook': receipt['cookbook_commit']}})
    return {**receipt, 'merged_source': source, 'merged_tree': commit['tree']['sha'],
            'final_source_ci': 'requested', 'candidate_artifacts_promoted': False}


def read_json(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 524288:
        raise ValueError('merge evidence must be a bounded regular file')
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['snapshot', 'finish'])
    parser.add_argument('--repository', required=True)
    parser.add_argument('--pull-request', required=True, type=int)
    parser.add_argument('--receipt', required=True, type=Path)
    parser.add_argument('--proof', type=Path)
    parser.add_argument('--validation-result', default='')
    parser.add_argument('--expected-head', default='')
    parser.add_argument('--expected-base', default='')
    args = parser.parse_args()
    try:
        if not candidate.REPOSITORY.fullmatch(args.repository) or not 0 < args.pull_request < 10**10:
            raise ValueError('invalid request identity')
        dispatch_context(args.repository)
        if args.operation == 'snapshot':
            value = snapshot(args.repository, args.pull_request, args.expected_head, args.expected_base)
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            args.receipt.write_text(json.dumps(value, indent=2) + '\n')
            if os.environ.get('GITHUB_OUTPUT'):
                with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                    for name in ['head', 'base', 'candidate', 'tree', 'cookbook_commit', 'validation_runner', 'validation_timeout', 'recipe', 'preview_environment']:
                        output.write(name + '=' + value[name] + '\n')
        else:
            value = read_json(args.receipt)
            if value['repository'] != args.repository or value['pull_request'] != args.pull_request:
                raise ValueError('receipt names another request')
            proof = read_json(args.proof) if args.proof and args.proof.exists() else {}
            accepted = finish(value, proof, args.validation_result)
            Path('.artifacts/merge-acceptance.json').write_text(json.dumps(accepted, indent=2) + '\n')
    except (OSError, ValueError, KeyError, TypeError, urllib.error.HTTPError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
