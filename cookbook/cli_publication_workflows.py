"""Generate thin CLI release callers while retaining their published signer identity."""
from configuration import load, ci_settings
from cli_release import policy

CHECKOUT = 'actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1'
DOWNLOAD = 'actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333'
UPLOAD = 'actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9'
ATTEST = 'actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8'


PYTHON = 'actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405'
ENTRY = "python3 -I -B -c 'import pathlib,runpy,sys;p=pathlib.Path(sys.argv[1]);sys.argv=sys.argv[1:];sys.path.insert(0,str(p.parent));runpy.run_path(str(p),run_name=\"__main__\")' .artifacts/recipe-sdks/_cookbook/cookbook/cli_release.py"


def preparation():
    return '''      - name: Require an accepted provider and explicit source dispatch
        run: |
          set -euo pipefail
          [[ "$COOKBOOK_SHA" =~ ^[a-f0-9]{40}$ && "$GITHUB_REF" == refs/heads/main && "$GITHUB_EVENT_NAME" == workflow_dispatch ]]
          relation="$(gh api "repos/nicodes/cookbook/compare/$COOKBOOK_SHA...main" --jq .status)"
          [[ "$relation" == ahead || "$relation" == identical ]]
      - name: Check out exact source data
        uses: CHECKOUT
        with:
          ref: ${{ github.sha }}
          fetch-depth: 0
          persist-credentials: false
      - name: Prepare the provider interpreter without caller configuration
        uses: PYTHON
        with:
          python-version: '3.13.11'
          check-latest: false
      - name: Check out accepted Cookbook
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
'''


def invocation(operation):
    return ENTRY + ' ' + operation + ' --source "$SOURCE_SHA" --cookbook-ref "$COOKBOOK_SHA" --tag "$RELEASE_TAG"'


def stage_invocation(role, product=False):
    return ENTRY.replace('/cli_release.py', '/cli_inputs.py') + ' --input ' + role + (' --product' if product else '')


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
PREPARATION      - name: Download this execution's complete source acceptance
        uses: DOWNLOAD
        with:
          name: cookbook-full-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}
          path: .artifacts/cli-input/full
      - name: Stage only closed original archive and proof data
        run: STAGE_FULL
      - name: Recheck complete native and host proof and exact publication bytes
        run: ACCEPT_BEFORE
      - name: Attest build provenance under the published source workflow identity
        uses: ATTEST
        with:
          subject-path: dist/*
      - name: Preserve original accepted archives and source proof
        id: accepted
        uses: UPLOAD
        with:
          name: accepted-cli-archives-${{ github.sha }}-${{ github.run_attempt }}
          path: |
            dist/
            .artifacts/cookbook-validation.json
          include-hidden-files: true
          if-no-files-found: error
          retention-days: 7
ADDITIONAL  publish:
    name: Publish immutable release
    needs: [identity, FINAL_JOB]
    permissions:
      contents: write
      actions: read
      attestations: read
    uses: nicodes/cookbook/.github/workflows/cookbook-cli-publish.yml@PIN
    with:
      cookbook-ref: PIN
      tag: ${{ needs.identity.outputs.tag }}
      artifact-id: ${{ needs.FINAL_JOB.outputs.artifact }}
'''
    additional = ''
    if values.get('acceptance'):
        files=values['acceptance']['files']
        evidence=['receipt.json',*files]
        additional = '''  product-acceptance:
    name: Verify product release
    needs: [identity, accept]
    runs-on: ubuntu-24.04
    timeout-minutes: ACCEPT_TIMEOUT
    permissions:
      contents: read
      actions: read
      attestations: read
    outputs:
      artifact: ${{ steps.evidence.outputs.artifact-id }}
    env:
      SOURCE_SHA: ${{ github.sha }}
      COOKBOOK_SHA: PIN
      RELEASE_TAG: ${{ needs.identity.outputs.tag }}
      GH_TOKEN: ${{ github.token }}
    steps:
PREPARATION      - name: Download original authenticated native archives and source proof
        uses: DOWNLOAD
        with:
          artifact-ids: ${{ needs.accept.outputs.artifact }}
          path: .artifacts/cli-input/archives
      - name: Stage only authenticated native archive and proof data
        run: STAGE_ARCHIVES
      - name: Run owned lifecycle assertions and verification without signing or publication authority
        id: domain
        run: DOMAIN
DIAGNOSTICS      - name: Preserve only declared product evidence and its bound receipt
        id: evidence
        uses: UPLOAD
        with:
          name: accepted-cli-product-${{ github.sha }}-${{ github.run_attempt }}
          path: |
EVIDENCE_PATHS          include-hidden-files: true
          if-no-files-found: error
          retention-days: 7
  attest-evidence:
    name: Attest accepted product evidence
    needs: [identity, accept, product-acceptance]
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
PREPARATION      - name: Download original accepted archives independently of product execution
        uses: DOWNLOAD
        with:
          artifact-ids: ${{ needs.accept.outputs.artifact }}
          path: .artifacts/cli-input/archives
      - name: Download only the product evidence handoff
        uses: DOWNLOAD
        with:
          artifact-ids: ${{ needs.product-acceptance.outputs.artifact }}
          path: .artifacts/cli-input/product
      - name: Stage original archives and closed product evidence without source overlays
        run: STAGE_PRODUCT
      - name: Admit source-bound evidence and actual read-only execution without product execution
        run: ACCEPT
      - name: Attest accepted product release evidence under the published source identity
        uses: ATTEST
        with:
          subject-path: |
SUBJECT_PATHS      - name: Preserve the accepted immutable publication payload
        id: accepted
        uses: UPLOAD
        with:
          name: accepted-cli-${{ github.sha }}-${{ github.run_attempt }}
          path: |
            dist/
            .artifacts/cookbook-validation.json
EVIDENCE_PATHS          include-hidden-files: true
          if-no-files-found: error
          retention-days: 7
'''
        diagnostic=''
        if values['acceptance'].get('diagnostics'):
            diagnostic = '''      - name: Preserve only declared nonsecret assertion diagnostics on failure
        if: failure() && steps.domain.outcome == 'failure'
        uses: UPLOAD
        with:
          name: cli-acceptance-failure-${{ github.sha }}-${{ github.run_attempt }}
          path: |
'''+''.join('            .artifacts/cli-publication-evidence/'+name+'\n' for name in values['acceptance']['diagnostics'])+'''          if-no-files-found: error
          include-hidden-files: true
          retention-days: 30
'''
        additional=additional.replace('DIAGNOSTICS',diagnostic).replace('EVIDENCE_PATHS',
            ''.join('            .artifacts/cli-publication-evidence/'+name+'\n' for name in evidence)).replace(
                'SUBJECT_PATHS',''.join('            dist/'+name+'\n' for name in files))
    text=text.replace('ADDITIONAL',additional).replace('FINAL_JOB','attest-evidence' if additional else 'accept')
    for old,new in [('PREPARATION',preparation()),('STAGE_FULL',stage_invocation('full')),
            ('STAGE_ARCHIVES',stage_invocation('archives')),('STAGE_PRODUCT',stage_invocation('archives', True)),
            ('ACCEPT_BEFORE',invocation('accept')+' --before-acceptance'),
            ('DOMAIN',invocation('acceptance')),('PIN',revision),
            ('RUNNER',settings['runner']),('HOST_TIMEOUT',str(values.get('validation_timeout',settings['timeout_minutes']))),
            ('ACCEPT_TIMEOUT',str(values['acceptance_timeout'])),('ACCEPT',invocation('accept')),('CHECKOUT',CHECKOUT),('DOWNLOAD',DOWNLOAD),
            ('UPLOAD',UPLOAD),('ATTEST',ATTEST),('PYTHON',PYTHON)]:
        text=text.replace(old,new)
    return {'.github/workflows/release.yml':text}
