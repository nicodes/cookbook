"""Generate the one-job Rust CLI Release: the plan, one validating job per platform, the integration job.

The jobs from the release plan through the complete accepted outputs of a ci.single_job consumer. The jobs that
sign and publish stay in cli_publication_workflows, so that the authority of every job is read in one place.
Every job here has contents read and no secret: they run the consumer's own build and test programs.
"""
import single_job_workflows as single

PLAN = 'plan'
FIRST = 'build-test'
SECOND = 'build-test-wave2'
HOST = 'build-test-host'
AUDIT_CAP = 15  # minutes for the pinned audit tools, and again for the lockfile audit, in the plan job
PLAN_NAME = 'Plan release'
PERMISSIONS = '    permissions:\n      contents: read\n      actions: read\n      pull-requests: read\n'


def plan_job(condition):
    """Select the unused release identity without mutation, in the workflow that releases it.

    This is the Plan of the select-cli-release workflow: the same accepted-Cookbook and dispatch checks, the same
    identity operation, the same outputs, with contents read alone.
    """
    return ('  ' + PLAN + ':\n    name: ' + PLAN_NAME + '\n' + ('    if: ' + condition + '\n' if condition else '') + '''    runs-on: ubuntu-24.04
    timeout-minutes: PLAN_TIMEOUT
    permissions:
      contents: read
    outputs:
      version: ${{ steps.identity.outputs.version }}
      tag: ${{ steps.identity.outputs.tag }}
      date: ${{ steps.identity.outputs.date }}
    env:
      SOURCE_SHA: ${{ github.sha }}
      COOKBOOK_SHA: PIN
      RELEASE_BUMP: ${{ inputs.bump }}
      GH_TOKEN: ${{ github.token }}
      MISE_GITHUB_TOKEN: ${{ github.token }}
    steps:
PREPARATION      - name: Prepare tools
        uses: ./.artifacts/recipe-sdks/_cookbook/sdk-setup
        with:
          tools: python
      - name: Prepare pinned Rust audit tools
        timeout-minutes: AUDIT_CAP
        uses: ./.artifacts/recipe-sdks/_cookbook/sdk-setup
        with:
          tools: python rust cargo:cargo-audit
      - name: Audit Rust lockfiles
        timeout-minutes: AUDIT_CAP
        run: AUDIT
      - name: Select unused native release identity without mutation
        id: identity
        run: IDENTITY
''').replace('PLAN_TIMEOUT', str(5 + 2 * AUDIT_CAP)).replace('AUDIT_CAP', str(AUDIT_CAP))


def release_inputs():
    return '          version: ${{ needs.' + PLAN + '.outputs.version }}\n          date: ${{ needs.' + PLAN + '.outputs.date }}\n'


def release_context(config, ci, validation):
    """The environment, Prepare inputs and extra integration steps that make a job part of the release run.

    The integration job runs the whole test phase with every advisory scan on (the one-job default is to skip them),
    and uploads the accepted outputs. The lockfile audit is the plan job's: it runs before any consumer program.
    """
    upload = ('      - name: Upload accepted outputs and fresh evidence\n        timeout-minutes: ' + str(single.PACKET_CAP) + '\n'
              '        uses: UPLOAD\n        with:\n'
              '          name: cookbook-full-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}\n'
              '          path: ${{ steps.test.outputs.artifact-paths }}\n'
              '          if-no-files-found: error\n          include-hidden-files: true\n          retention-days: 7\n')
    return {'env': "      COOKBOOK_NATIVE_RELEASE: 'true'\n", 'inputs': release_inputs(),
            'host_inputs': '          advisories: run\n', 'host_steps': upload}


def first_wave_job(config, ci, revision):
    pin = '@' + revision
    text = '  ' + FIRST + ':\n    name: ' + single.CHECK + ' (${{ matrix.platform }}-${{ matrix.arch }})\n    needs: ' + PLAN + '\n'
    text += '    strategy:\n      fail-fast: false\n      matrix:\n        include:\n'
    for row in single.platforms(config, release=True):
        first = True
        for key, value in row.items():
            text += ('          - ' if first else '            ') + key + ': ' + str(value) + '\n'
            first = False
    text += '    runs-on: ${{ matrix.runner }}\n    timeout-minutes: ${{ matrix.timeout_minutes }}\n' + PERMISSIONS
    text += ('    env:\n      COOKBOOK_VALIDATION_TIMEOUT: \'' + str(ci['timeout_minutes']) + '\'\n'
             '      COOKBOOK_CANDIDATE_COMMIT: ${{ github.sha }}\n      COOKBOOK_NATIVE_RELEASE: \'true\'\n')
    text += ('    steps:\n      - name: Prepare\n        timeout-minutes: ' + str(single.PREPARE_CAP) + '\n'
             '        uses: nicodes/cookbook/prepare' + pin + '\n        with:\n'
             '          cookbook-ref: ' + revision + '\n          source: ${{ github.sha }}\n'
             '          target: ${{ matrix.target }}\n' + release_inputs())
    text += ('      - name: Build\n        timeout-minutes: ${{ matrix.timeout_minutes }}\n        uses: nicodes/cookbook/build' + pin + '\n'
             '      - name: Test\n        timeout-minutes: ${{ matrix.timeout_minutes }}\n        uses: nicodes/cookbook/test' + pin + '\n')
    text += single.export_step('${{ matrix.id }}', revision)
    text += ('      - name: Finish\n        if: always()\n        timeout-minutes: 5\n'
             '        uses: nicodes/cookbook/finish' + pin + '\n        with:\n          status: ${{ job.status }}\n'
             '          entry: ${{ matrix.id }}\n')
    return text


def jobs(config, revision, validation, condition=None, fill=str):
    """The release plan, the first wave, the second wave when declared and the integration job, in run order.

    validation is the deadline the release allows the complete validation. The text keeps the tokens PREPARATION,
    PIN, IDENTITY, AUDIT and UPLOAD, which fill resolves before the text joins the workflow.
    """
    from configuration import ci_settings
    ci = {**ci_settings(config), 'timeout_minutes': validation}
    release = release_context(config, ci, validation)
    text = plan_job(condition) + first_wave_job(config, ci, revision)
    second = single.second_wave(config)
    if second:
        text += single.native_wave_job(config, ci, revision, job=SECOND,
                                       name=single.CHECK + ' (${{ matrix.platform }}-${{ matrix.arch }}' + single.WAVE2 + ')',
                                       rows=single.wave2_platforms(config), needs='[' + PLAN + ', ' + FIRST + ']', role='2',
                                       cookbook=revision, source='${{ github.sha }}', candidate='github.sha', release=release)
    text += single.native_wave_job(config, ci, revision, job=HOST, name=single.INTEGRATION,
                                   rows=single.integration(config, validation),
                                   needs='[' + PLAN + ', ' + FIRST + (', ' + SECOND if second else '') + ']', role='host',
                                   cookbook=revision, source='${{ github.sha }}', candidate='github.sha', release=release)
    return fill(text)


def publish_job(final, fill=str):
    """Publish from the accepted payload with the one job that has contents write; it runs only Cookbook's own code."""
    return fill('''  publish:
    name: Publish
    needs: [PLAN_JOB, FINAL_JOB]
    runs-on: ubuntu-24.04
    timeout-minutes: 15
    permissions:
      contents: write
      actions: read
      attestations: read
    env:
      SOURCE_SHA: ${{ github.sha }}
      COOKBOOK_SHA: PIN
      RELEASE_TAG: ${{ needs.PLAN_JOB.outputs.tag }}
      GH_TOKEN: ${{ github.token }}
    steps:
PREPARATION      - name: Download exactly the accepted caller artifact
        uses: DOWNLOAD
        with:
          artifact-ids: ${{ needs.FINAL_JOB.outputs.artifact }}
          path: .artifacts/cli-input/publication
          merge-multiple: true
      - name: Stage only admitted release data without overlaying source or providers
        run: STAGE_PUBLICATION
      - name: Recheck exact source proof and publish immutable native bytes
        run: PUBLISH_RUN
'''.replace('PLAN_JOB', PLAN).replace('FINAL_JOB', final))
