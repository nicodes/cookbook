#!/usr/bin/env python3
"""Generate the canonical pinned Mise entrypoint from caller-owned recipe data."""
import argparse
import hashlib
from pathlib import Path
import re
import tomllib

from configuration import load, ci_settings, read_source_data
import orchestration
import recipe as native_recipe
import workflow_names

ATTRIBUTES_START = '# BEGIN GENERATED COOKBOOK SOURCE ATTRIBUTES v1\n'
ATTRIBUTES_END = '# END GENERATED COOKBOOK SOURCE ATTRIBUTES v1\n'
ATTRIBUTES_BLOCK = (ATTRIBUTES_START + '/mise.toml -text\n/cookbook.toml -text\n' + ATTRIBUTES_END)


def completion_jobs(revision, runner):
    """Expose one required result for the repository's complete recipe checks."""
    return '''  build-test:
    name: Build and test
    if: always()
    needs: [validate]
    runs-on: RUNNER
    timeout-minutes: 1
    permissions: {}
    steps:
      - name: Require the complete current source validation
        timeout-minutes: 1
        uses: nicodes/cookbook/ci-result@PIN
        with:
          event-name: ${{ github.event_name }}
          needs: ${{ toJSON(needs) }}
'''.replace('PIN', revision).replace('RUNNER', runner)


def source_attributes(root):
    """Preserve native Git attributes and own only the two raw recipe inputs."""
    import mise_entrypoint
    path = Path(root) / '.gitattributes'
    source = mise_entrypoint.discovery().read_regular(path) if path.exists() or path.is_symlink() else b''
    text = mise_entrypoint.source_text(source)
    lines = re.split(r'(?<=\n)|(?<=\r)(?!\n)', text)
    starts = [i for i, line in enumerate(lines) if mise_entrypoint.universal_text(line) == ATTRIBUTES_START]
    ends = [i for i, line in enumerate(lines) if mise_entrypoint.universal_text(line) == ATTRIBUTES_END]
    if starts or ends:
        if (len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]
                or ''.join(lines[ends[0] + 1:])):
            raise ValueError('Git attributes require one complete generated source suffix')
        text = ''.join(lines[:starts[0]])
    if text and not text.endswith('\n'):
        text += '\n'
    result = text + ATTRIBUTES_BLOCK
    if len(result.encode()) > mise_entrypoint.MAX_SOURCE:
        raise ValueError('generated Git source attributes exceed the bounded source size')
    return result


def workflow_files(root, revision, config=None):
    if not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('cookbook workflows require a full source pin')
    config = load(root) if config is None else config
    ci = ci_settings(config)
    model = orchestration.model(config)
    workflow = {'host': 'build-and-test-host.yml', 'native': 'build-and-test-native.yml',
                'godot': 'build-and-test-godot.yml'}[model['route']]
    header = '# Generated cookbook caller. Change recipe data and regenerate.\n'
    files = {'.github/workflows/build-and-test.yml': header + '''name: Build and test
run-name: "Build and test · ${{ github.event.pull_request.number && format('PR #{0}', github.event.pull_request.number) || inputs.source || github.ref_name }}"
'on':
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
  merge_group:
    types: [checks_requested]
  push:
    branches: [main]
  workflow_dispatch:
    inputs:
      source:
        description: Exact source commit; defaults to this main revision
        type: string
        default: ''
      cookbook:
        description: Accepted cookbook commit; defaults to the pinned recipe
        type: string
        default: ''
permissions: {}
concurrency:
  group: ci-${{ github.ref }}-${{ inputs.source || 'current' }}
  cancel-in-progress: true
jobs:
  validate:
    name: Test
    uses: nicodes/cookbook/.github/workflows/WORKFLOW@PIN
    permissions:
      contents: read
      actions: read
      pull-requests: read
    with:
      cookbook-ref: ${{ inputs.cookbook || 'PIN' }}
      source: ${{ inputs.source || github.sha }}
'''.replace('WORKFLOW', workflow).replace('PIN', revision), '.github/workflows/merge-pr.yml': header + '''name: Merge PR
run-name: "Merge PR #${{ inputs.pull-request }}"
'on':
  workflow_dispatch:
    inputs:
      pull-request:
        description: PR number to fully validate and merge when current
        type: string
        required: true
      expected-head:
        description: Exact head selected by a dependency policy decision; paired with expected-base
        type: string
        default: ''
      expected-base:
        description: Exact main selected by a dependency policy decision; paired with expected-head
        type: string
        default: ''
permissions: {}
concurrency:
  group: cookbook-merge-${{ github.repository }}
  cancel-in-progress: false
jobs:
  request:
    name: Merge
    uses: nicodes/cookbook/.github/workflows/merge-pr.yml@PIN
    permissions:
      contents: write
      pull-requests: read
      statuses: write
      actions: write
    with:
      cookbook-ref: PIN
      pull-request: ${{ inputs.pull-request }}
      expected-head: ${{ inputs.expected-head }}
      expected-base: ${{ inputs.expected-base }}
'''.replace('PIN', revision) + '      runner: ' + ci['runner'] + '\n'}
    validation_parameters = ''
    if model['route'] == 'host':
        validation_parameters += '      mode: full\n'
    if ci.get('preview_environment'):
        validation_parameters += ("      preview-environment: ${{ github.event_name == 'pull_request' && '"
            + ci['preview_environment'] + "' || '' }}\n")
    validation_parameters += ('      runner: ' + ci['runner'] + '\n'
        + '      timeout-minutes: ' + str(ci['timeout_minutes']) + '\n')
    files['.github/workflows/build-and-test.yml'] += validation_parameters
    if config.get('delivery'):
        if config['recipe'] not in {'react-native', 'godot-game'}:
            raise ValueError('this delivery composition is enrolled for application images only')
        import delivery_workflows
        # Host admission authenticates the caller's deploy.yml workflow identity.
        # Production validates once here; PR and requested candidates use build-and-test.yml.
        original = files['.github/workflows/build-and-test.yml']
        files['.github/workflows/build-and-test.yml'] = original.replace('  push:\n    branches: [main]\n', '')
        production = original.replace('name: Build and test\n', 'name: Deploy\n', 1)
        production = re.sub(r'^run-name:.*$', 'run-name: "Deploy · ${{ inputs.source || github.ref_name }}"',
                            production, count=1, flags=re.MULTILINE)
        production = production.replace('  pull_request:\n    types: [opened, synchronize, reopened, ready_for_review]\n', '')
        production = production.replace('  merge_group:\n    types: [checks_requested]\n', '')
        production = re.sub(r'^      preview-environment:.*\n', '', production, flags=re.MULTILINE)
        production = production.replace('group: ci-${{ github.ref }}-', 'group: deploy-source-${{ github.sha }}-', 1).replace('cancel-in-progress: true', 'cancel-in-progress: false', 1)
        delivery=delivery_workflows.declaration(root,config)
        if delivery.get('validation_timeout_minutes'):
            production=production.replace('      timeout-minutes: '+str(ci['timeout_minutes'])+'\n',
                '      timeout-minutes: '+str(delivery['validation_timeout_minutes'])+'\n',1)
        production, verification = delivery_workflows.verification_entry(root, config, revision, production)
        files['.github/workflows/deploy.yml'] = production + delivery_workflows.jobs(root, config, revision, verification=verification)

    if config.get('preview'):
        import preview_workflows
        files.update(preview_workflows.workflow_files(root, revision, config))
    dependency_policy = Path(root) / '.github/dependency-policy.toml'
    if dependency_policy.is_file():
        policy = read_source_data(dependency_policy, maximum=262144)
        if policy.get('version') == 2:
            if 'Documentation policy' in policy.get('required_checks', []):
                raise ValueError('dependency policy must require complete source CI instead of the retired documentation check')
            files['.github/workflows/dependency-policy.yml'] = header + """name: Dependency policy
'on':
  pull_request_target:
    types: [opened, synchronize, reopened, ready_for_review]
  workflow_run:
    workflows: [Build and test]
    types: [completed]
    branches: ['dependabot/**', 'dependabot-*', 'dependabot_*']
  workflow_dispatch: {}
  schedule:
    - cron: '4 6 * * *'
permissions: {}
jobs:
  request:
    name: Review dependencies
    if: github.event_name != 'workflow_run' || github.event.workflow_run.event == 'pull_request'
    uses: nicodes/cookbook/.github/workflows/dependency-policy.yml@PIN
    permissions:
      contents: read
      pull-requests: read
      actions: write
      checks: read
      statuses: read
    with:
      cookbook-ref: PIN
""".replace('PIN', revision) + '      runner: ' + ci['runner'] + '\n'
    if config.get('publication') and config['recipe'] not in {'godot-addon', 'rust-cli'}:
        raise ValueError('this recipe release caller is not enrolled yet; preserve its existing publication authority')
    if config.get('publication'):
        if config['recipe'] == 'rust-cli':
            import cli_publication_workflows
            files.update(cli_publication_workflows.workflow_files(root, revision))
        else:
            import publication_workflows
            files.update(publication_workflows.workflow_files(root, revision))
    if config.get('operations'):
        import operational_workflows
        if (Path(root) / '.github/workflows/dependency-maintenance.yml').exists():
            if not operational_workflows.declaration(root, config).get('maintenance'):
                raise ValueError('declare native maintenance settings before retiring dependency maintenance')
        operations = operational_workflows.workflow_files(root, revision, config)
        if set(operations) & set(files):
            raise ValueError('each generated workflow requires one native lifecycle owner')
        files.update(operations)
    files['.github/workflows/build-and-test.yml'] += completion_jobs(revision, ci['runner'])
    workflow_names.check(files)
    return files


def entrypoint_files(root, revision):
    """Render the one canonical Mise authority from unchanged native recipes."""
    import mise_entrypoint
    root = Path(root).resolve()
    config = load(root)
    import repository_hygiene
    repository_hygiene.check_configuration(config)
    source = mise_entrypoint.discovery().read_regular(root / 'mise.toml')
    configuration = mise_entrypoint.discovery().read_regular(root / 'cookbook.toml')
    suites = native_recipe.test_suites(root, config)
    capabilities = (['dev'] if config.get('dev') else []) + [
        'db:' + name for name in mise_entrypoint.DATABASE if name in config.get('database', {})]
    return {'mise.toml': mise_entrypoint.render(source, revision, config['recipe'],
                                               configuration, suites, capabilities),
            '.gitattributes': source_attributes(root)}


def check_entrypoint(root, revision):
    root = Path(root)
    if any((root / name).exists() or (root / name).is_symlink()
           for name in ['Makefile', 'makefile', 'GNUmakefile']):
        raise ValueError('canonical Mise recipe must have one task authority')
    import repository_hygiene
    repository_hygiene.check_tracked(root)
    import mise_entrypoint
    record = mise_entrypoint.read(root)
    if record['revision'] != revision:
        raise ValueError('Mise source differs from its selected Cookbook revision')
    for name, expected in entrypoint_files(root, revision).items():
        actual = mise_entrypoint.discovery().read_regular(Path(root) / name).decode('utf-8')
        if mise_entrypoint.universal_text(actual) != mise_entrypoint.universal_text(expected):
            raise ValueError('generated entrypoint differs from its pinned recipe data: ' + name)


def task_command(operation):
    """The single native task ABI of this Cookbook edition."""
    return ['mise', 'run', operation]


def check_workflows(root, revision):
    """Generated workflow source is UTF-8 on every runner, independent of locale."""
    files = workflow_files(root, revision)
    obsolete = workflow_names.obsolete_recipe_files(root, files)
    if obsolete:
        raise ValueError('retired recipe files must be removed: ' + ', '.join(obsolete))
    for name, expected in files.items():
        if (Path(root) / name).read_text(encoding='utf-8') != expected:
            raise ValueError('workflow differs from the pinned recipe generator: ' + name)
    workflow_names.check(workflow_names.sources(root))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--revision', required=True)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--workflows', action='store_true')
    args = parser.parse_args()
    try:
        if args.check:
            check_entrypoint(args.root, args.revision)
            if args.workflows:
                check_workflows(args.root, args.revision)
            return
        files = entrypoint_files(args.root, args.revision)
        if args.workflows:
            workflows = workflow_files(args.root, args.revision)
            for name in workflow_names.obsolete_recipe_files(args.root, workflows):
                (args.root / name).unlink()
            files.update(workflows)
        for name, expected in files.items():
            path = args.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(expected.encode('utf-8'))
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
