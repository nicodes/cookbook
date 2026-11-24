"""Generate the daily Security workflow of one-job recipes that have no delivery to audit."""
import hashlib
import json
import re

from configuration import ci_settings
import orchestration
import single_job_workflows

# Application recipes audit through their operations declaration instead.
RECIPES = ('rust-cli', 'godot-addon')
FILE = '.github/workflows/security.yml'
# Daily at an off-peak UTC time: 02:07 to 05:52, never at the top of an hour, when scheduled runs bunch up.
HOURS = (2, 3, 4, 5)
MINUTES = tuple(range(7, 53))


def scanners(config):
    """The advisory scans the recipe declares; empty means there is nothing to scan and no Security workflow.

    A Rust CLI always has Cargo components, so its lockfiles are always audited. Node packages add their audit
    and declared trivy paths add a filesystem scan. A Godot addon has no Cargo component: only Node packages.
    """
    result = []
    if config['recipe'] == 'rust-cli':
        result.append('cargo audit')
        if config.get('security', {}).get('trivy_paths'):
            result.append('trivy fs')
    if config.get('node', {}).get('packages'):
        result.append('npm audit')
    return result


def selected(config):
    return config.get('recipe') in RECIPES and not config.get('operations') and bool(scanners(config))


def identity(config):
    """A stable name from recipe data that every checkout of the repository shares: the packaged binary or archive."""
    package = config.get('package', {})
    name = package.get('binary') if config['recipe'] == 'rust-cli' else package.get('archive')
    return config['recipe'] + ':' + (name if isinstance(name, str) and name else '')


def cron(config):
    """Spread projects over the window by hashing their identity, so a fleet does not fire at one minute."""
    value = int.from_bytes(hashlib.sha256(identity(config).encode()).digest()[:8], 'big')
    return '%d %d * * *' % (MINUTES[value % len(MINUTES)], HOURS[(value // len(MINUTES)) % len(HOURS)])


def needs_build(config):
    """Declared security assertions may run the built product; scans alone need no build."""
    return bool(config.get('tests', {}).get('security'))


def workflow(config, revision):
    if not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('cookbook workflows require a full source pin')
    single_job_workflows.unsupported(config)
    ci = ci_settings(config)
    native = orchestration.model(config)['route'] == 'native'
    # A recipe whose complete build needs other platforms' outputs has no product to build in this one job: the daily
    # workflow runs the scans only and the integration job runs the product-running security tests on every pull request.
    build = needs_build(config) and not (native and single_job_workflows.integration_needed(config))
    target = ''
    runner = ci['runner']
    if native:
        row = next(row for row in single_job_workflows.platforms(config) if (row['platform'], row['arch']) == ('Linux', 'x86_64'))
        runner, target = row['runner'], row['target']
    prepare = single_job_workflows.PREPARE_CAP
    step = ci['timeout_minutes']
    job = min(single_job_workflows.JOB_CAP, prepare + step * (2 if build else 1) + 5 + 5)
    pin = '@' + revision
    lines = ['# Generated cookbook caller. Change recipe data and regenerate.', 'name: Security',
             'run-name: "Security · ${{ github.event.schedule || github.event_name }}"', "'on':",
             '  schedule:', '    - cron: ' + json.dumps(cron(config)), '  workflow_dispatch:',
             'permissions: {}', 'concurrency:', '  group: security-${{ github.ref }}', '  cancel-in-progress: false',
             'jobs:', '  security:', '    name: Security', "    if: github.ref == 'refs/heads/main'",
             '    runs-on: ' + runner, '    timeout-minutes: ' + str(job),
             '    permissions:', '      contents: read', '      issues: write',
             '    steps:', '      - name: Prepare', '        timeout-minutes: ' + str(prepare),
             '        uses: nicodes/cookbook/prepare' + pin, '        with:', '          cookbook-ref: ' + revision,
             '          source: ${{ github.sha }}', '          advisories: run']
    if target:
        lines += ['          target: ' + target]
    if build:
        lines += ['      - name: Build', '        timeout-minutes: ' + str(step), '        uses: nicodes/cookbook/build' + pin]
    lines += ['      - name: Audit', '        timeout-minutes: ' + str(step), '        uses: nicodes/cookbook/security' + pin,
              '      - name: Finish', '        if: always()', '        timeout-minutes: 5',
              '        uses: nicodes/cookbook/finish' + pin, '        with:', '          status: ${{ job.status }}',
              '      - name: Report failure', '        if: failure()', '        timeout-minutes: 5',
              '        run: python3 "$TOOLS_ROOT/helpers/report-failure.py"', '        env:',
              '          GH_TOKEN: ${{ github.token }}']
    return '\n'.join(lines) + '\n'


def workflow_files(config, revision):
    """The Security caller of a one-job recipe, or nothing for other recipes and for recipes with nothing to scan."""
    return {FILE: workflow(config, revision)} if selected(config) else {}
