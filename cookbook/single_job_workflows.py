"""Generate the one-workflow, one-job Build and test caller for consumers that opt in with ci.single_job."""
import re

from configuration import ci_settings
import orchestration

CHECK = 'Build and test'
RETIRED_CHECK = CHECK + ' / Test'
PREPARE_CAP = 20
JOB_CAP = 180
# A split run builds once: the build job's check, and the cap on the step that packs or imports the build outputs.
BUILD_JOB = 'build'
BUILD_CHECK = CHECK + ' (' + BUILD_JOB + ')'
PACKET_CAP = 15
# A Rust CLI recipe with a second native wave adds a job per platform for it and one integration job.
WAVE2 = ' wave 2'
INTEGRATION = CHECK + ' (integration)'


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


def platforms(config, release=False):
    """The literal job matrix of a Rust CLI recipe: one job per declared platform, with its own deadline.

    The platform that hosts the lint, unit, security, integration and e2e groups also gets the recipe's CI deadline,
    because the multi-job form runs those groups in a separate job with that deadline. A recipe with a second native
    wave, or with package files built on another platform, has no such platform: see wave2_platforms and integration.
    """
    import native_ci
    ci = ci_settings(config)
    rows = []
    declaration = native_ci.single_job_support(config, release)
    for entry in declaration['wave1']['include']:
        job = entry['timeout_minutes'] + (ci['timeout_minutes'] if native_ci.hosts_groups(entry, declaration) else 0)
        if declaration['integration']:
            job += PACKET_CAP  # the job also exports its packet
        rows.append({'id': entry['id'], 'runner': entry['runner'], 'platform': entry['platform'], 'arch': entry['arch'],
                     'target': entry['target'], 'timeout_minutes': min(JOB_CAP, job)})
    return rows


def integration_needed(config):
    """Whether the complete build consumes other platforms' outputs, so that one integration job must host the groups."""
    import native_ci
    return native_ci.single_job_support(config)['integration']


def wave2_check(entry):
    return CHECK + ' (' + entry['platform'] + '-' + entry['arch'] + WAVE2 + ')'


def second_wave(config):
    """Whether a Rust CLI recipe has platforms that consume artifacts other platforms' jobs build."""
    import native_ci
    return bool(native_ci.single_job_support(config)['wave2']['include'])


def wave2_platforms(config):
    """The literal second-wave job matrix: one row per declared platform, after the first wave's packets arrive."""
    import native_ci
    rows = []
    for entry in native_ci.single_job_support(config)['wave2']['include']:
        rows.append({'id': entry['id'], 'runner': entry['runner'], 'platform': entry['platform'], 'arch': entry['arch'],
                     'target': entry['target'], 'timeout_minutes': min(JOB_CAP, entry['timeout_minutes'] + PACKET_CAP)})
    return rows


def integration(config, release=None):
    """The job that imports every packet and hosts the groups when the complete build needs them: Linux x86_64.

    It gets the platform's own deadline for building, the recipe's CI deadline for the groups and the packet allowance.
    A release passes the deadline its full validation is allowed instead of the recipe's CI deadline.
    """
    import native_ci
    entry = next(entry for entry in native_ci.single_job_support(config, release is not None)['wave1']['include']
                 if (entry['platform'], entry['arch']) == native_ci.PRIMARY)
    # A release also uploads the accepted outputs after its tests, so it budgets a second packet allowance.
    job = (entry['timeout_minutes'] + (ci_settings(config)['timeout_minutes'] + PACKET_CAP if release is None
                                       else release + 2 * PACKET_CAP))
    return {'id': entry['id'], 'runner': entry['runner'], 'platform': entry['platform'], 'arch': entry['arch'],
            'target': entry['target'], 'check': INTEGRATION, 'timeout_minutes': min(JOB_CAP, job)}


def required_checks(config):
    """The check names a generated Rust CLI caller reports: one per platform, then one per second-wave platform and
    the integration job when there is a second wave. There is no combined check."""
    checks = [platform_check(row) for row in platforms(config)]
    if second_wave(config):
        checks += [wave2_check(row) for row in wave2_platforms(config)]
    if integration_needed(config):
        checks.append(INTEGRATION)
    return checks


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


def split(config):
    """Whether this run builds once in its own job: only a recipe with a different-runner gate does."""
    return bool(gate_jobs(config))


def source_checks(config):
    """Every check a non-Rust dependency policy must require: the main job, each gate job and, when split, the build.

    The build check is required too: a job skipped because its build failed would otherwise count as passing.
    """
    jobs = gate_jobs(config)
    return [CHECK, *([BUILD_CHECK] if jobs else []), *[job['check'] for job in jobs]]


def gate_deadline(config, job, deadline):
    """A gate job prepares and imports the build, then runs only its gates."""
    test = min(JOB_CAP, job['timeout_minutes'])
    packet = min(PACKET_CAP, deadline['build'])
    return {'job': min(JOB_CAP, PREPARE_CAP + packet + test), 'prepare': deadline['prepare'], 'build': packet, 'test': test}


def build_deadline(deadline):
    """The build job prepares, builds and packs the outputs."""
    return {'job': min(JOB_CAP, PREPARE_CAP + deadline['build'] + PACKET_CAP), 'prepare': deadline['prepare'],
            'build': deadline['build'], 'test': 0}


def main_deadline(deadline):
    """The main job of a split run imports the build instead of making it."""
    packet = min(PACKET_CAP, deadline['build'])
    return {**deadline, 'job': max(deadline['job'] - deadline['build'] + packet, 1), 'build': packet}


def import_step(deadline, pin):
    return ('      - name: Import build\n        timeout-minutes: ' + str(deadline['build']) + '\n'
            '        uses: nicodes/cookbook/import-build' + pin + '\n        with:\n'
            '          digest: ${{ needs.' + BUILD_JOB + '.outputs.digest }}\n')


def job_text(ci, revision, deadline, *, job, name, runner, cookbook, source, candidate, gates='', role='whole'):
    """One validation job: prepare, build, test, finish. gates names the runner whose gates the job alone runs.

    role is whole (today's one job), build (prepare, build, pack the outputs for the others) or consume (prepare,
    import the packed build, test): a split run builds once and every other job imports that build.
    """
    text = '  ' + job + ':\n    name: ' + name + '\n    runs-on: ' + runner + '\n'
    if role == 'consume':
        text = text.replace('    runs-on:', '    needs: ' + BUILD_JOB + '\n    runs-on:', 1)
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
    if role == 'consume':
        text += import_step(deadline, pin)
    else:
        text += ('      - name: Build\n        timeout-minutes: ' + str(deadline['build']) + '\n'
                 '        uses: nicodes/cookbook/build' + pin + '\n')
    if role == 'build':
        text += ('      - name: Pack build outputs\n        id: pack\n        timeout-minutes: ' + str(PACKET_CAP) + '\n'
                 '        uses: nicodes/cookbook/export-build' + pin + '\n')
        text = text.replace('    permissions:', '    outputs:\n      digest: ${{ steps.pack.outputs.digest }}\n    permissions:', 1)
    else:
        text += '      - name: Test\n        timeout-minutes: ' + str(deadline['test']) + '\n        uses: nicodes/cookbook/test' + pin + '\n'
    text += ('      - name: Finish\n        if: always()\n        timeout-minutes: 5\n'
             '        uses: nicodes/cookbook/finish' + pin + '\n        with:\n'
             '          status: ${{ job.status }}\n')
    return text


def build_job_text(config, ci, revision, deadline, *, cookbook, source, candidate, condition=None):
    """The job that builds once for a split run, on the recipe's own runner; empty when no gate needs it."""
    if not split(config):
        return ''
    text = job_text(ci, revision, build_deadline(deadline), job=BUILD_JOB, name=BUILD_CHECK, runner=ci['runner'],
                    cookbook=cookbook, source=source, candidate=candidate, role='build')
    return text.replace('    runs-on:', '    if: ' + condition + '\n    runs-on:', 1) if condition else text


def gate_job_texts(config, ci, revision, deadline, *, cookbook, source, candidate, condition=None):
    """The extra jobs, each named Build and test (<gate or runner>); each imports the build job's outputs."""
    result = []
    for job in gate_jobs(config):
        text = job_text(ci, revision, gate_deadline(config, job, deadline), job=job['id'], name=job['check'],
                        runner=job['runner'], cookbook=cookbook, source=source, candidate=candidate, gates=job['runner'],
                        role='consume')
        if condition:
            text = text.replace('    runs-on:', '    if: ' + condition + '\n    runs-on:', 1)
        result.append((job, text))
    return result


def export_step(entry, revision):
    return ('      - name: Export native packet\n        timeout-minutes: ' + str(PACKET_CAP) + '\n'
            '        uses: nicodes/cookbook/export-native@' + revision + '\n        with:\n          entry: ' + entry + '\n')


def native_wave_job(config, ci, revision, *, job, name, rows, needs, role, cookbook, source, candidate, release=None):
    """A job of a second-wave Rust CLI recipe: the second-wave matrix or the integration job.

    Both prepare, import the packets of the jobs they need, build and test. The second wave also exports its own
    packet for the integration job; the integration job, which hosts the groups, exports nothing.

    release (single_job_release) adds the release identity and, for the integration job, the lockfile audit and the
    upload of the complete accepted outputs; without it the text is the Build and test job.
    """
    pin = '@' + revision
    matrix = role == '2'
    text = '  ' + job + ':\n    name: ' + name + '\n    needs: ' + needs + '\n'
    if matrix:
        text += '    strategy:\n      fail-fast: false\n      matrix:\n        include:\n'
        for row in rows:
            first = True
            for key, value in row.items():
                text += ('          - ' if first else '            ') + key + ': ' + str(value) + '\n'
                first = False
        text += '    runs-on: ${{ matrix.runner }}\n    timeout-minutes: ${{ matrix.timeout_minutes }}\n'
        limit = '${{ matrix.timeout_minutes }}'
    else:
        text += '    runs-on: ' + rows['runner'] + '\n    timeout-minutes: ' + str(rows['timeout_minutes']) + '\n'
        limit = str(rows['timeout_minutes'])
    text += '    permissions:\n      contents: read\n      actions: read\n      pull-requests: read\n'
    text += ('    env:\n      COOKBOOK_VALIDATION_TIMEOUT: \'' + str(ci['timeout_minutes']) + '\'\n'
             '      COOKBOOK_CANDIDATE_COMMIT: ${{ ' + candidate + ' }}\n      COOKBOOK_NATIVE_WAVE: ' + ("'2'" if matrix else 'host') + '\n')
    if release:
        text += release['env']
    target = '${{ matrix.target }}' if matrix else rows['target']
    text += ('    steps:\n      - name: Prepare\n        timeout-minutes: ' + str(PREPARE_CAP) + '\n'
             '        uses: nicodes/cookbook/prepare' + pin + '\n        with:\n'
             '          cookbook-ref: ' + cookbook + '\n          source: ' + source + '\n          target: ' + target + '\n'
             + (release['inputs'] + ('' if matrix else release['host_inputs']) if release else '') +
             '      - name: Import native packets\n        timeout-minutes: ' + str(PACKET_CAP) + '\n'
             '        uses: nicodes/cookbook/import-native' + pin + '\n'
             '      - name: Build\n        timeout-minutes: ' + limit + '\n        uses: nicodes/cookbook/build' + pin + '\n'
             '      - name: Test\n        ' + ('id: test\n        ' if release and not matrix else '') + 'timeout-minutes: ' + limit
             + '\n        uses: nicodes/cookbook/test' + pin + '\n')
    if release and not matrix:
        text += release['host_steps']
    if matrix:
        text += export_step('${{ matrix.id }}', revision)
    text += ('      - name: Finish\n        if: always()\n        timeout-minutes: 5\n'
             '        uses: nicodes/cookbook/finish' + pin + '\n        with:\n'
             '          status: ${{ job.status }}\n' + ('          entry: ${{ matrix.id }}\n' if matrix else ''))
    return text


def native_wave_jobs(config, ci, revision, *, cookbook, source, candidate):
    """The second-wave and integration jobs of a Rust CLI recipe; empty when the platform jobs are complete alone."""
    text = ''
    second = second_wave(config)
    if second:
        text += native_wave_job(config, ci, revision, job='build-test-wave2',
                                name=CHECK + ' (${{ matrix.platform }}-${{ matrix.arch }}' + WAVE2 + ')',
                                rows=wave2_platforms(config), needs='build-test', role='2', cookbook=cookbook,
                                source=source, candidate=candidate)
    if integration_needed(config):
        text += native_wave_job(config, ci, revision, job='build-test-host', name=INTEGRATION, rows=integration(config),
                                needs='[build-test, build-test-wave2]' if second else 'build-test', role='host',
                                cookbook=cookbook, source=source, candidate=candidate)
    return text


def build_and_test(config, revision, dispatch=False):
    """Render the caller. dispatch keeps the manual entry that post-merge dependency dispatch relies on."""
    if not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('cookbook workflows require a full source pin')
    unsupported(config)
    ci = ci_settings(config)
    native = orchestration.model(config)['route'] == 'native'
    deadline = budgets(config)
    built = deadline
    consume = split(config)
    if consume:
        deadline = main_deadline(deadline)
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
    text += 'jobs:\n'
    candidate = 'github.event.pull_request.head.sha || ' + ('inputs.source || ' if dispatch else '') + 'github.sha'
    text += build_job_text(config, ci, revision, built, cookbook=cookbook, source=source, candidate=candidate)
    text += '  build-test:\n'
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
        text += '    name: ' + CHECK + '\n' + ('    needs: ' + BUILD_JOB + '\n' if consume else '')
        text += '    runs-on: ' + ci['runner'] + '\n'
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
    if consume:
        text += import_step(deadline, pin)
    else:
        text += '      - name: Build\n        timeout-minutes: ' + str(deadline['build']) + '\n        uses: nicodes/cookbook/build' + pin + '\n'
    text += ('      - name: Test\n        timeout-minutes: ' + str(deadline['test']) + '\n'
             '        uses: nicodes/cookbook/test' + pin + '\n')
    if native and integration_needed(config):
        # Later jobs consume this wave's work.
        text += export_step('${{ matrix.id }}', revision)
    text += ('      - name: Finish\n        if: always()\n        timeout-minutes: 5\n'
             '        uses: nicodes/cookbook/finish' + pin + '\n        with:\n'
             '          status: ${{ job.status }}\n' + ('          entry: ${{ matrix.id }}\n' if native else ''))
    for _, extra in gate_job_texts(config, ci, revision, deadline, cookbook=cookbook, source=source, candidate=candidate):
        text += extra
    if native:
        text += native_wave_jobs(config, ci, revision, cookbook=cookbook, source=source, candidate=candidate)
    return text
