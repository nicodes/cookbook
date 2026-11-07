"""Generate thin CLI release callers while retaining their published signer identity."""
from configuration import load, ci_settings
from cli_release import policy

CHECKOUT = 'actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1'
DOWNLOAD = 'actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333'
UPLOAD = 'actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9'
ATTEST = 'actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8'


def workflow_files(root, revision):
    config = load(root)
    values, settings = policy(config), ci_settings(config)
    text = '''# Generated cookbook caller. Change recipe data and regenerate.
# Attestation stays in this source workflow to preserve published installer trust.
name: Release
'on':
  workflow_dispatch:
    inputs:
      bump:
        description: Version bump
        required: true
        type: choice
        default: patch
        options: [patch, minor, major]
permissions: {}
concurrency:
  group: release-${{ github.repository }}
  cancel-in-progress: false
jobs:
  identity:
    name: Select immutable release identity
    permissions:
      contents: read
    uses: nicodes/cookbook/.github/workflows/cookbook-cli-identity.yml@PIN
    with:
      cookbook-ref: PIN
      bump: ${{ inputs.bump }}
  validate:
    name: Validate release source and native archives
    needs: identity
    permissions:
      contents: read
      actions: read
      pull-requests: read
    uses: nicodes/cookbook/.github/workflows/cookbook-native.yml@PIN
    with:
      cookbook-ref: PIN
      source: ${{ github.sha }}
      version: ${{ needs.identity.outputs.version }}
      date: ${{ needs.identity.outputs.date }}
      runner: RUNNER
      timeout-minutes: HOST_TIMEOUT
  accept:
    name: Attest accepted source archives
    needs: [identity, validate]
    runs-on: ubuntu-24.04
    timeout-minutes: ACCEPT_TIMEOUT
    permissions:
      contents: read
      actions: read
      attestations: write
      id-token: write
    outputs:
      artifact: ${{ steps.accepted.outputs.artifact-id }}
    env:
      SOURCE_SHA: ${{ github.sha }}
      COOKBOOK_SHA: PIN
      RELEASE_TAG: ${{ needs.identity.outputs.tag }}
      GH_TOKEN: ${{ github.token }}
    steps:
      - name: Check out exact source
        uses: CHECKOUT
        with:
          ref: ${{ github.sha }}
          fetch-depth: 0
          persist-credentials: false
      - name: Check out accepted cookbook
        uses: CHECKOUT
        with:
          repository: nicodes/cookbook
          ref: PIN
          path: .artifacts/recipe-sdks/_cookbook
          persist-credentials: false
      - name: Prepare separate immutable Cookbook and Tools sources
        uses: ./.artifacts/recipe-sdks/_cookbook/sdk-dependencies
        with:
          cookbook-ref: PIN
      - name: Bootstrap caller-pinned Python
        uses: ./.artifacts/recipe-sdks/_cookbook/sdk-setup
        with:
          tools: python
      - name: Download this execution's complete source acceptance
        uses: DOWNLOAD
        with:
          name: cookbook-full-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}
          path: .
      - name: Recheck complete native and host proof and exact publication bytes
        run: python3 .artifacts/recipe-sdks/_cookbook/cookbook/cli_release.py accept --source "$SOURCE_SHA" --cookbook-ref "$COOKBOOK_SHA" --tag "$RELEASE_TAG" --before-acceptance
      - name: Attest build provenance under the published source workflow identity
        uses: ATTEST
        with:
          subject-path: dist/*
      - name: Preserve accepted publication payload and source proof
        id: accepted
        uses: UPLOAD
        with:
          name: accepted-cli-${{ github.sha }}-${{ github.run_attempt }}
          path: |
            dist/
            .artifacts/cookbook-validation.json
          include-hidden-files: true
          if-no-files-found: error
          retention-days: 7
  publish:
    name: Publish immutable release
    needs: [identity, accept]
    permissions:
      contents: write
      actions: read
      attestations: read
    uses: nicodes/cookbook/.github/workflows/cookbook-cli-publish.yml@PIN
    with:
      cookbook-ref: PIN
      tag: ${{ needs.identity.outputs.tag }}
      artifact-id: ${{ needs.accept.outputs.artifact }}
'''
    if values.get('acceptance'):
        additional = '''      - name: Run owned lifecycle assertions against authenticated native archives
        id: domain
        run: python3 .artifacts/recipe-sdks/_cookbook/cookbook/cli_release.py acceptance --source "$SOURCE_SHA" --cookbook-ref "$COOKBOOK_SHA" --tag "$RELEASE_TAG"
      - name: Attest accepted product release evidence under the published source identity
        uses: ATTEST
        with:
          subject-path: |
''' + ''.join('            dist/' + name + '\n' for name in values['acceptance']['files']) + '''      - name: Recheck native bytes and independently verified release evidence
        run: python3 .artifacts/recipe-sdks/_cookbook/cookbook/cli_release.py accept --source "$SOURCE_SHA" --cookbook-ref "$COOKBOOK_SHA" --tag "$RELEASE_TAG"
'''
        marker = '      - name: Preserve accepted publication payload and source proof\n'
        text = text.replace(marker, additional + marker)
        if values['acceptance'].get('diagnostics'):
            diagnostic_step = '''      - name: Preserve only declared nonsecret assertion diagnostics on failure
        if: failure() && steps.domain.outcome == 'failure'
        uses: UPLOAD
        with:
          name: cli-acceptance-failure-${{ github.sha }}-${{ github.run_attempt }}
          path: |
''' + ''.join('            .artifacts/cli-publication-evidence/' + name + '\n' for name in values['acceptance']['diagnostics']) + '''          if-no-files-found: error
          include-hidden-files: true
          retention-days: 30
'''
            text = text.replace(marker, diagnostic_step + marker)
        marker = '            .artifacts/cookbook-validation.json\n'
        evidence = ['receipt.json', *values['acceptance']['files']]
        text = text.replace(marker, marker + ''.join('            .artifacts/cli-publication-evidence/' + name + '\n' for name in evidence))
    for old, new in [('PIN', revision), ('RUNNER', settings['runner']), ('HOST_TIMEOUT', str(values.get('validation_timeout', settings['timeout_minutes']))),
                     ('ACCEPT_TIMEOUT', str(values['acceptance_timeout'])), ('CHECKOUT', CHECKOUT),
                     ('DOWNLOAD', DOWNLOAD), ('UPLOAD', UPLOAD), ('ATTEST', ATTEST)]:
        text = text.replace(old, new)
    return {'.github/workflows/release.yml': text}
