"""Generate the one-workflow, one-job Build and test caller for consumers that opt in with ci.single_job."""
import re

from configuration import ci_settings
import orchestration

CHECK = 'Build and test'
RETIRED_CHECK = CHECK + ' / Test'
PREPARE_CAP = 20
JOB_CAP = 180


def budgets(config):
    """Job and step deadlines for the one job, from the same recipe settings the legacy jobs read."""
    ci = ci_settings(config)
    model = orchestration.model(config)
    if model['route'] == 'native':
        # Native jobs differ per platform; see platforms(). This is the largest of them.
        job = max(row['timeout_minutes'] for row in platforms(config))
        build = test = job
    elif model['route'] == 'host':
        job = ci['timeout_minutes']
        build = test = job
    else:
        import godot_ci
        required = godot_ci.plan(None, config).get('requirements', [])
        if not required:
            raise ValueError('single-job Godot CI requires its source-owned worker topology')
        build = next(row['timeout_minutes'] for row in required if row['kind'] == 'build')
        unit = max([row['timeout_minutes'] for row in required if row['kind'] == 'unit'], default=0)
        host = next(row['timeout_minutes'] for row in required if row['kind'] == 'host')
        # Gates that need their own setup run in this job, one after another, after the ordinary groups.
        gates = sum(row['timeout_minutes'] for row in godot_ci.single_split(config)[0])
        job = min(JOB_CAP, build + unit + host + gates)
        build, test = min(build, job), min(unit + host + gates, job)
    return {'job': job, 'prepare': min(PREPARE_CAP, job), 'build': build, 'test': test}


def platform_check(entry):
    return CHECK + ' (' + entry['platform'] + '-' + entry['arch'] + ')'


def platforms(config):
    """The literal job matrix of a Rust CLI recipe: one job per declared platform, with its own deadline.

    The platform that hosts the lint, unit, security, integration and e2e groups also gets the recipe's CI deadline,
    because the multi-job form runs those groups in a separate job with that deadline.
    """
    import native_ci
    ci = ci_settings(config)
    rows = []
    for entry in native_ci.single_job_support(config)['wave1']['include']:
        job = entry['timeout_minutes'] + (ci['timeout_minutes'] if native_ci.hosts_groups(entry) else 0)
        rows.append({'id': entry['id'], 'runner': entry['runner'], 'platform': entry['platform'], 'arch': entry['arch'],
                     'target': entry['target'], 'timeout_minutes': min(JOB_CAP, job)})
    return rows


def required_checks(config):
    """The check names a generated Rust CLI caller reports, one per platform; there is no combined check."""
    return [platform_check(row) for row in platforms(config)]


# Inputs of the compiler cache identity (compiler_cache.plan): recipe data, SDK pins, manifests, lockfiles, cargo config.
CACHE_PATHS = ['.github/workflows/build-and-test.yml', 'cookbook.toml', 'mise.toml', '**/Cargo.toml', '**/Cargo.lock',
               '**/.cargo/config.toml']


def unsupported(config):
    """Refuse recipes and gates that one process cannot yet cover."""
    import ci as native_ci
    native_ci.single_job_support(config, orchestration.model(config))


def gate_jobs(config):
    """The extra jobs of gates that need a runner image other than the recipe's: one per distinct runner."""
    if orchestration.model(config)['route'] != 'godot':
        return []
    import godot_ci
    return godot_ci.single_jobs(config)


def source_checks(config):
    """Every check a non-Rust dependency policy must require: the main job and each different-runner gate job."""
    return [CHECK, *[job['check'] for job in gate_jobs(config)]]


def gate_deadline(config, job, deadline):
    """A gate job prepares and builds like the main job, then runs only its gates."""
    test = min(JOB_CAP, job['timeout_minutes'])
    return {'job': min(JOB_CAP, PREPARE_CAP + deadline['build'] + test), 'prepare': deadline['prepare'],
            'build': deadline['build'], 'test': test}


def job_text(ci, revision, deadline, *, job, name, runner, cookbook, source, candidate, gates=''):
    """One validation job: prepare, build, test, finish. gates names the runner whose gates the job alone runs."""
    text = '  ' + job + ':\n    name: ' + name + '\n    runs-on: ' + runner + '\n'
    text += '    timeout-minutes: ' + str(deadline['job']) + '\n'
    preview = ci.get('preview_environment')
    if preview:
        text += "    environment: ${{ github.event_name == 'pull_request' && '" + preview + "' || '' }}\n"
    text += '    permissions:\n      contents: read\n      actions: read\n      pull-requests: read\n'
    text += ('    env:\n      COOKBOOK_VALIDATION_TIMEOUT: \'' + str(ci['timeout_minutes']) + '\'\n'
             '      COOKBOOK_CANDIDATE_COMMIT: ${{ ' + candidate + ' }}\n')
    if gates:
        # The test action reads this selection; the gate job then runs only that runner's gates.
        text += '      COOKBOOK_GATE_RUNNER: ' + gates + '\n'
    pin = '@' + revision
    text += ('    steps:\n      - name: Prepare\n        timeout-minutes: ' + str(deadline['prepare']) + '\n'
             '        uses: nicodes/cookbook/prepare' + pin + '\n        with:\n'
             '          cookbook-ref: ' + cookbook + '\n          source: ' + source + '\n')
    if preview:
        text += ("          preview-clerk-key: ${{ github.event_name == 'pull_request' && vars.CLERK_PUBLISHABLE_KEY || '' }}\n")
    text += ('      - name: Build\n        timeout-minutes: ' + str(deadline['build']) + '\n'
             '        uses: nicodes/cookbook/build' + pin + '\n'
             '      - name: Test\n        timeout-minutes: ' + str(deadline['test']) + '\n'
             '        uses: nicodes/cookbook/test' + pin + '\n')
    text += ('      - name: Finish\n        if: always()\n        timeout-minutes: 5\n'
             '        uses: nicodes/cookbook/finish' + pin + '\n        with:\n'
             '          status: ${{ job.status }}\n')
    return text


def gate_job_texts(config, ci, revision, deadline, *, cookbook, source, candidate, condition=None):
    """The extra jobs, each named Build and test (<gate or runner>)."""
    result = []
    for job in gate_jobs(config):
        text = job_text(ci, revision, gate_deadline(config, job, deadline), job=job['id'], name=job['check'],
                        runner=job['runner'], cookbook=cookbook, source=source, candidate=candidate, gates=job['runner'])
        if condition:
            text = text.replace('    runs-on:', '    if: ' + condition + '\n    runs-on:', 1)
        result.append((job, text))
    return result


def build_and_test(config, revision, dispatch=False):
    """Render the caller. dispatch keeps the manual entry that post-merge dependency dispatch relies on."""
    if not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('cookbook workflows require a full source pin')
    unsupported(config)
    ci = ci_settings(config)
    native = orchestration.model(config)['route'] == 'native'
    deadline = budgets(config)
    source = '${{ inputs.source || github.sha }}' if dispatch else '${{ github.sha }}'
    cookbook = "${{ inputs.cookbook || '" + revision + "' }}" if dispatch else revision
    run_name = "Build and test · ${{ github.event.pull_request.number && format('PR #{0}', github.event.pull_request.number) || "
    run_name += ('inputs.source || ' if dispatch else '') + "github.ref_name }}"
    text = '# Generated cookbook caller. Change recipe data and regenerate.\nname: Build and test\n'
    text += 'run-name: "' + run_name + '"\n'
    text += "'on':\n  pull_request:\n    types: [opened, synchronize, reopened, ready_for_review]\n"
    text += '  merge_group:\n    types: [checks_requested]\n'
    if native:
        # A pull request restores compiler caches only from its own branch or main. Main saves them when the
        # inputs that define the cache identity change.
        text += '  push:\n    branches: [main]\n    paths:\n' + ''.join("      - '" + name + "'\n" for name in CACHE_PATHS)
    if dispatch:
        text += ('  workflow_dispatch:\n    inputs:\n      source:\n'
                 '        description: Exact source commit; defaults to this main revision\n'
                 '        type: string\n        default: \'\'\n      cookbook:\n'
                 '        description: Accepted cookbook commit; defaults to the pinned recipe\n'
                 '        type: string\n        default: \'\'\n')
    group = 'ci-${{ github.ref }}' + ("-${{ inputs.source || 'current' }}" if dispatch else '')
    text += 'permissions: {}\nconcurrency:\n  group: ' + group + '\n  cancel-in-progress: true\n'
    text += 'jobs:\n  build-test:\n'
    if native:
        text += '    name: ' + CHECK + ' (${{ matrix.platform }}-${{ matrix.arch }})\n'
        text += '    strategy:\n      fail-fast: false\n      matrix:\n        include:\n'
        for row in platforms(config):
            first = True
            for key, value in row.items():
                text += ('          - ' if first else '            ') + key + ': ' + str(value) + '\n'
                first = False
        text += '    runs-on: ${{ matrix.runner }}\n    timeout-minutes: ${{ matrix.timeout_minutes }}\n'
        deadline = {**deadline, 'job': '${{ matrix.timeout_minutes }}', 'build': '${{ matrix.timeout_minutes }}',
                    'test': '${{ matrix.timeout_minutes }}'}
    else:
        text += '    name: ' + CHECK + '\n    runs-on: ' + ci['runner'] + '\n'
        text += '    timeout-minutes: ' + str(deadline['job']) + '\n'
    preview = None if native else ci.get('preview_environment')
    if preview:
        text += "    environment: ${{ github.event_name == 'pull_request' && '" + preview + "' || '' }}\n"
    text += '    permissions:\n      contents: read\n      actions: read\n      pull-requests: read\n'
    candidate = 'github.event.pull_request.head.sha || ' + ('inputs.source || ' if dispatch else '') + 'github.sha'
    text += ('    env:\n      COOKBOOK_VALIDATION_TIMEOUT: \'' + str(ci['timeout_minutes']) + '\'\n'
             '      COOKBOOK_CANDIDATE_COMMIT: ${{ ' + candidate + ' }}\n')
    pin = '@' + revision
    text += ('    steps:\n      - name: Prepare\n        timeout-minutes: ' + str(deadline['prepare']) + '\n'
             '        uses: nicodes/cookbook/prepare' + pin + '\n        with:\n'
             '          cookbook-ref: ' + cookbook + '\n          source: ' + source + '\n')
    if native:
        text += '          target: ${{ matrix.target }}\n'
    if preview:
        text += ("          preview-clerk-key: ${{ github.event_name == 'pull_request' && vars.CLERK_PUBLISHABLE_KEY || '' }}\n")
    text += ('      - name: Build\n        timeout-minutes: ' + str(deadline['build']) + '\n'
             '        uses: nicodes/cookbook/build' + pin + '\n'
             '      - name: Test\n        timeout-minutes: ' + str(deadline['test']) + '\n'
             '        uses: nicodes/cookbook/test' + pin + '\n'
             '      - name: Finish\n        if: always()\n        timeout-minutes: 5\n'
             '        uses: nicodes/cookbook/finish' + pin + '\n        with:\n'
             '          status: ${{ job.status }}\n' + ('          entry: ${{ matrix.id }}\n' if native else ''))
    for _, extra in gate_job_texts(config, ci, revision, deadline, cookbook=cookbook, source=source, candidate=candidate):
        text += extra
    return text
