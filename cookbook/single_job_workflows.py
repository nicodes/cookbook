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
        raise ValueError('single-job CI is not yet supported for Rust CLI recipes')
    if model['route'] == 'host':
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
        job = min(JOB_CAP, build + unit + host)
        build, test = min(build, job), min(unit + host, job)
    return {'job': job, 'prepare': min(PREPARE_CAP, job), 'build': build, 'test': test}


def unsupported(config):
    """Refuse recipes and gates that one process cannot yet cover."""
    import ci as native_ci
    native_ci.single_job_support(config, orchestration.model(config))


def build_and_test(config, revision, dispatch=False):
    """Render the caller. dispatch keeps the manual entry that post-merge dependency dispatch relies on."""
    if not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('cookbook workflows require a full source pin')
    unsupported(config)
    ci = ci_settings(config)
    deadline = budgets(config)
    source = '${{ inputs.source || github.sha }}' if dispatch else '${{ github.sha }}'
    cookbook = "${{ inputs.cookbook || '" + revision + "' }}" if dispatch else revision
    run_name = "Build and test · ${{ github.event.pull_request.number && format('PR #{0}', github.event.pull_request.number) || "
    run_name += ('inputs.source || ' if dispatch else '') + "github.ref_name }}"
    text = '# Generated cookbook caller. Change recipe data and regenerate.\nname: Build and test\n'
    text += 'run-name: "' + run_name + '"\n'
    text += "'on':\n  pull_request:\n    types: [opened, synchronize, reopened, ready_for_review]\n"
    text += '  merge_group:\n    types: [checks_requested]\n'
    if dispatch:
        text += ('  workflow_dispatch:\n    inputs:\n      source:\n'
                 '        description: Exact source commit; defaults to this main revision\n'
                 '        type: string\n        default: \'\'\n      cookbook:\n'
                 '        description: Accepted cookbook commit; defaults to the pinned recipe\n'
                 '        type: string\n        default: \'\'\n')
    group = 'ci-${{ github.ref }}' + ("-${{ inputs.source || 'current' }}" if dispatch else '')
    text += 'permissions: {}\nconcurrency:\n  group: ' + group + '\n  cancel-in-progress: true\n'
    text += 'jobs:\n  build-test:\n    name: ' + CHECK + '\n    runs-on: ' + ci['runner'] + '\n'
    text += '    timeout-minutes: ' + str(deadline['job']) + '\n'
    preview = ci.get('preview_environment')
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
    if preview:
        text += ("          preview-clerk-key: ${{ github.event_name == 'pull_request' && vars.CLERK_PUBLISHABLE_KEY || '' }}\n")
    text += ('      - name: Build\n        timeout-minutes: ' + str(deadline['build']) + '\n'
             '        uses: nicodes/cookbook/build' + pin + '\n'
             '      - name: Test\n        timeout-minutes: ' + str(deadline['test']) + '\n'
             '        uses: nicodes/cookbook/test' + pin + '\n'
             '      - name: Finish\n        if: always()\n        timeout-minutes: 5\n'
             '        uses: nicodes/cookbook/finish' + pin + '\n        with:\n'
             '          status: ${{ job.status }}\n')
    return text
