"""Generate thin source-owned callers for the trusted addon publication workflows."""
from pathlib import Path
import re

from configuration import load
from publication import policy, plugin_version
from addon_release_workflows import workflow_path


def workflow_files(root, revision):
    root = Path(root).resolve()
    if not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('publication callers require a full cookbook source pin')
    config = load(root)
    if not config.get('publication'):
        return {}
    values = policy(root, config)
    inputs = []
    arguments = ['      mode: release']
    republish = values['registry_auth'] == 'oidc' and values['mode'] == 'tag'
    if republish:
        inputs += ['      mode:', '        description: Publish a new version or republish an existing version',
                   '        type: choice', '        default: publish', '        options: [publish, republish]']
    if values['mode'] == 'bump':
        inputs += ['      bump:', '        description: Version bump; plugin.cfg must already match',
                   '        type: choice', '        default: patch', '        options: [patch, minor, major]']
        arguments.append('      bump: ${{ inputs.bump }}')
    elif values['mode'] == 'tag':
        inputs += ['      tag:', '        description: Exact tag matching the package version',
                   '        type: string', '        required: true',
                   '        default: ' + values['tag_prefix'] + plugin_version(root, config)]
        arguments.append('      tag: ${{ inputs.tag }}')
    if values['retry']:
        for name, description in [('retry-tag', 'Existing immutable tag; requires retry-sha256'),
                                  ('retry-sha256', 'Exact existing release ZIP SHA256; requires retry-tag')]:
            inputs += ['      ' + name + ':', '        description: ' + description,
                       '        type: string', "        default: ''"]
            arguments.append('      ' + name + ": ${{ inputs['" + name + "'] }}")
    trigger = "'on':\n  workflow_dispatch:" + ('\n    inputs:\n' + '\n'.join(inputs) if inputs else '')
    permissions = ['      contents: write', '      actions: read', '      pull-requests: read'] + (['      id-token: write'] if values['registry_auth'] == 'oidc' else [])
    header = '# Generated cookbook caller. Change recipe data and regenerate.\n'
    target = 'inputs.tag' if values['mode'] == 'tag' else 'inputs.bump' if values['mode'] == 'bump' else 'github.ref_name'
    if values['retry']:
        target = "inputs['retry-tag'] || " + target
    if republish:
        target = 'inputs.mode }} · ${{ ' + target
    release = header + '\n'.join(['name: Release', 'run-name: "Release · ${{ '+target+' }}"', trigger, 'permissions: {}', 'concurrency:',
        '  group: release-${{ github.repository }}', '  cancel-in-progress: false', 'jobs:', '  release:', '    name: Release',
        *(["    if: inputs.mode == 'publish'"] if republish else []),
        '    uses: nicodes/cookbook/' + workflow_path(values['registry_auth']) + '@' + revision,
        '    permissions:', *permissions, '    with:', '      cookbook-ref: ' + revision, *arguments]) + '\n'
    if values['registry_auth'] == 'api-key':
        release += '    secrets:\n      gdam-key: ${{ secrets.GDAM_API_KEY }}\n'
    if republish:
        release += '''  republish:
    name: Republish existing version
    if: inputs.mode == 'republish'
    uses: nicodes/cookbook/.github/workflows/recover-addon.yml@PIN
    permissions:
      contents: read
      id-token: write
    with:
      cookbook-ref: PIN
      mode: registry-recovery
      tag: ${{ inputs.tag }}
'''.replace('PIN', revision)
    return {'.github/workflows/release.yml': release}
