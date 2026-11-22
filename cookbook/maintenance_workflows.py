"""Render independent bounded dependency checks in application Maintenance."""
import json
import re

import dependencies

STAGES = {'checkout', 'setup', 'check', 'artifact', 'report'}
CRON = re.compile('[0-9*/,-]+(?: [0-9*/,-]+){4}')


def declaration(value):
    if not isinstance(value, dict) or set(value) - {'tools', 'packages', 'backup-storage'} or not value:
        raise ValueError('maintenance requires a declared native check')
    for role, record in value.items():
        common = {'runner', 'timeout_minutes', 'step_timeouts', 'concurrency'}
        allowed = common | ({'environment'} if role == 'backup-storage' else {'schedule'})
        if not isinstance(record, dict) or set(record) - allowed:
            raise ValueError('maintenance requires bounded native check settings')
        if role == 'backup-storage' and record.get('environment') not in {'production', 'Production'}:
            raise ValueError('backup storage checks require the protected production environment')
        if not isinstance(record.get('runner'), str) or not re.fullmatch('[A-Za-z0-9_.-]{1,80}', record['runner']):
            raise ValueError('maintenance requires its native runner')
        budget = record.get('timeout_minutes')
        if type(budget) is not int or not 1 <= budget <= 180:
            raise ValueError('maintenance requires its bounded job deadline')
        schedule = record.get('schedule', [])
        if (not isinstance(schedule, list) or len(schedule) > 4
                or any(not isinstance(item, str) or not CRON.fullmatch(item) for item in schedule)
                or len(set(schedule)) != len(schedule)):
            raise ValueError('maintenance schedules must be distinct literal cron entries')
        stages = record.get('step_timeouts', {})
        available_stages = {'checkout', 'setup', 'check'} if role == 'backup-storage' else STAGES
        if (not isinstance(stages, dict) or set(stages) - available_stages
                or any(type(n) is not int or not 1 <= n <= budget for n in stages.values())):
            raise ValueError('maintenance step limits stay within the original job deadline')
        group = record.get('concurrency')
        if group is not None and (not isinstance(group, str) or not re.fullmatch('[A-Za-z0-9_.:/-]{1,128}', group)):
            raise ValueError('maintenance serialization requires a literal native group')
    return value


def dispatch(value, security=False):
    choices = (['dependencies'] if {'tools', 'packages'} & value.keys() else []) + (['security'] if security else [])
    if 'backup-storage' in value:
        choices.append('backup-storage')
    assert choices
    lines = ['  workflow_dispatch:', '    inputs:', '      task:',
             '        description: Maintenance task', '        type: choice',
             '        options: ' + json.dumps(choices), '        default: ' + choices[0]]
    if 'backup-storage' in value:
        lines += ['      storage-capability:',
                  '        description: Retain one synthetic object to verify immutable backup storage',
                  '        type: boolean', '        default: false']
    return lines


def condition(schedule, task):
    assert task in {'security', 'dependencies'}
    selected = "'" + json.dumps(schedule, separators=(',', ':')) + "'"
    return ("github.ref == 'refs/heads/main' && ((github.event_name == 'workflow_dispatch' && inputs.task == '"
        + task + "') || (github.event_name == 'schedule' && contains(fromJSON(" + selected + "), github.event.schedule)))")


def jobs(value):
    pin = dependencies.tools_identity()['revision']
    lines = []
    for role in ['tools', 'packages']:
        if role not in value:
            continue
        record = value[role]
        helper = 'watch-tools' if role == 'tools' else 'update-bun'
        label = 'Check tool versions' if role == 'tools' else 'Check package versions'
        artifact = 'tool-updates' if role == 'tools' else 'bun-updates'
        lines += ['  ' + role + ':', '    name: ' + label,
            '    if: ${{ ' + condition(record.get('schedule', []), 'dependencies') + ' }}',
            '    runs-on: ' + record['runner'], '    timeout-minutes: ' + str(record['timeout_minutes']),
            '    permissions:', '      contents: read', '      issues: write', '      attestations: read']
        if record.get('concurrency'):
            lines += ['    concurrency:', '      group: ' + record['concurrency'], '      cancel-in-progress: false']
        lines += ['    env:', '      MISE_GITHUB_TOKEN: ${{ github.token }}', '    steps:']
        stages = record.get('step_timeouts', {})
        def step(name, stage, body):
            lines.append('      - name: ' + name)
            if stage in stages:
                lines.append('        timeout-minutes: ' + str(stages[stage]))
            lines.extend('        ' + part for part in body)
        step('Check out source', 'checkout', [
            'uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1',
            'with:', '  persist-credentials: false'])
        step('Set up development tools', 'setup', ['uses: nicodes/tools/setup@' + pin,
            'with:', '  tools: ' + ('python' if role == 'tools' else 'python bun'),
            'env:', '  GH_TOKEN: ${{ github.token }}'])
        step(label, 'check', ['run: python3 "$TOOLS_ROOT/helpers/' + helper + '.py"',
            'env:', '  GH_TOKEN: ${{ github.token }}'])
        step('Upload artifacts', 'artifact', ['if: always()',
            'uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9',
            'with:', '  name: ' + artifact, '  path: .artifacts/' + artifact + '.json',
            '  if-no-files-found: ignore', '  retention-days: 30', '  include-hidden-files: true'])
        step('Report failure', 'report', ['if: failure()',
            'run: python3 "$TOOLS_ROOT/helpers/report-failure.py"', 'env:', '  GH_TOKEN: ${{ github.token }}'])
    if 'backup-storage' in value:
        lines += storage_job(value['backup-storage'], pin)
    return lines


def storage_job(record, pin):
    stages = record.get('step_timeouts', {})
    budget = record['timeout_minutes']
    return [
        '  backup-storage:', '    name: Check backup storage',
        "    if: github.ref == 'refs/heads/main' && github.event_name == 'workflow_dispatch' && inputs.task == 'backup-storage' && inputs.storage-capability",
        '    runs-on: '+record['runner'], '    timeout-minutes: '+str(budget),
        '    environment: '+record['environment'], '    permissions:', '      contents: read',
        '    concurrency:', '      group: '+record.get('concurrency', 'backup-storage-capability'),
        '      cancel-in-progress: false', '    steps:',
        '      - name: Check out the pinned Tools source',
        '        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1',
        '        timeout-minutes: '+str(stages.get('checkout', budget)),
        '        with:', '          repository: nicodes/tools', '          ref: '+pin, '          persist-credentials: false',
        '      - name: Prepare the pinned Python runtime', '        uses: nicodes/tools/setup@'+pin,
        '        timeout-minutes: '+str(stages.get('setup', budget)), '        with:', '          tools: python',
        '        env:', '          GH_TOKEN: ${{ github.token }}',
        '      - name: Check immutable synthetic creation and preserved original bytes',
        '        timeout-minutes: '+str(stages.get('check', budget)), '        shell: bash',
        '        env:', '          BACKUP_S3_ACCESS_KEY: ${{ secrets.BACKUP_S3_ACCESS_KEY }}',
        '          BACKUP_S3_SECRET_KEY: ${{ secrets.BACKUP_S3_SECRET_KEY }}',
        '          BACKUP_S3_HOSTNAME: ${{ vars.BACKUP_S3_HOSTNAME }}',
        '          BACKUP_S3_BUCKET: ${{ vars.BACKUP_S3_BUCKET }}',
        '        run: python3 -I -B helpers/probe-backup-store.py',
    ]
