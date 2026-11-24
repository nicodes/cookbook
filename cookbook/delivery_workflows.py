"""Generate thin Komizo delivery callers after the same-run recipe acceptance."""
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from configuration import relative, read_source_data, ci_settings


DEPLOYMENT_EVENT = "(github.event_name == 'push' || (github.event_name == 'workflow_dispatch' && inputs.mode == 'deploy'))"


def production_gates(root, config):
    if config.get('recipe') != 'godot-game' or not config.get('ci',{}).get('godot',{}).get('gates'):
        return []
    import godot_ci
    records=godot_ci.plan(root,config).get('production',{}).get('include',[])
    if len(records)>1:
        raise ValueError('delivery requires one source-owned public production policy')
    return records


def deadlines(data):
    values=data.get('step_timeouts',{})
    names={'checkout','setup','plan','registry','inspect','prepare','connect','capture','download','deploy','readiness','diagnostics','artifact','upload'}
    if not isinstance(values,dict) or set(values)-names:raise ValueError('delivery step deadlines require implemented native phases')
    result={name:values.get(name,min(3,data['deploy_timeout']) if name in {'readiness','diagnostics','registry','connect'} else min(5,data['deploy_timeout']) if name in {'setup','plan','download'} else min(10,data['deploy_timeout']) if name in {'artifact','upload'} else min(15,data['deploy_timeout']) if name=='capture' else data['deploy_timeout']) for name in names}
    if any(type(value) is not int or not 1<=value<=data['deploy_timeout'] for value in result.values()):
        raise ValueError('delivery step deadlines must stay within the original job budget')
    return result


def report_settings(data,key,runner):
    defaults={'report':dict(runner=runner,timeout_minutes=10,step_timeouts={'checkout':10,'setup':5,'report':10}),
        'notification':dict(runner=runner,timeout_minutes=2,step_timeouts={'checkout':2,'notify':1})}
    value=data.get(key,defaults[key])
    if key=='report' and value is False:value=defaults[key]
    if (not isinstance(value,dict) or set(value)-({'runner','timeout_minutes','step_timeouts'}|({'environment'} if key=='notification' else set()))
            or not isinstance(value.get('runner'),str) or not re.fullmatch('[A-Za-z0-9_.-]{1,80}',value['runner'])
            or type(value.get('timeout_minutes')) is not int or not 1<=value['timeout_minutes']<=45):
        raise ValueError('delivery reporting requires its native runner and job deadline')
    if key=='notification' and value.get('environment',data.get('environment','production')) not in ('production','Production'):
        raise ValueError('notification retains its original protected environment')
    stages=value.get('step_timeouts',{})
    names=set(defaults[key]['step_timeouts'])
    if (not isinstance(stages,dict) or set(stages)-names
            or any(type(v) is not int or not 1<=v<=value['timeout_minutes'] for v in stages.values())):
        raise ValueError('delivery reporting preserves bounded native step deadlines')
    return dict(value,step_timeouts={name:stages.get(name,value['timeout_minutes']) for name in names})


def declaration(root, config):
    value = config.get('delivery')
    if not isinstance(value, dict) or set(value) != {'definition'}:
        raise ValueError('delivery requires its explicit native declaration')
    path = relative(Path(root), value['definition'], exists=True)
    if path.stat().st_size > 32768:
        raise ValueError('delivery declaration exceeds its bound')
    data = read_source_data(path, maximum=32768)
    keys = {'version', 'project', 'komizo_ref', 'publish_runner', 'publish_timeout',
            'deploy_runner', 'deploy_timeout', 'health_urls',
            'backup', 'upload_backup', 'readiness', 'diagnostics', 'notification_url', 'combined',
            'environment','step_timeouts','readiness_operation','notify','report','notification',
            'expected_secret_generations','validation_timeout_minutes'}
    if not isinstance(data, dict) or set(data) - keys or data.get('version') != 1:
        raise ValueError('unsupported delivery declaration')
    if not re.fullmatch('[a-z][a-z0-9-]{0,62}', data.get('project', '')):
        raise ValueError('invalid delivery project')
    if not re.fullmatch('[a-f0-9]{40}', data.get('komizo_ref', '')):
        raise ValueError('Komizo delivery must use an exact accepted source pin')
    for key in ['publish_runner', 'deploy_runner']:
        if not re.fullmatch('[A-Za-z0-9_.-]{1,80}', data.get(key, '')):
            raise ValueError('delivery requires its existing runner labels')
    for key in ['publish_timeout', 'deploy_timeout']:
        if type(data.get(key)) is not int or not 1 <= data[key] <= 180:
            raise ValueError('delivery requires bounded existing job budgets')
    if 'validation_timeout_minutes' in data and (type(data['validation_timeout_minutes']) is not int or not 1<=data['validation_timeout_minutes']<=180):
        raise ValueError('delivery verification retains its own bounded native deadline')
    if data.get('environment','production') not in ('production','Production'):raise ValueError('delivery retains its native protected environment')
    deadlines(data)
    for key in ['report','notification']:report_settings(data,key,ci_settings(config)['runner'])
    urls = data.get('health_urls')
    if not isinstance(urls, list) or not 1 <= len(urls) <= 20:
        raise ValueError('delivery requires explicit health URLs')
    for entry in urls:
        if isinstance(entry, dict):
            if (set(entry) != {'url', 'contains'} or not isinstance(entry['contains'], str)
                    or not 1 <= len(entry['contains']) <= 512 or any(ord(c) < 32 for c in entry['contains'])):
                raise ValueError('health body assertions require an explicit bounded expected value')
        elif not isinstance(entry, str):
            raise ValueError('health entries must be URLs or explicit body assertions')
    for url in [*[entry['url'] if isinstance(entry, dict) else entry for entry in urls], data.get('notification_url', '')]:
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or any(ord(c) < 33 for c in url):
            raise ValueError('delivery URLs must be explicit public HTTPS URLs')
    generations=data.get('expected_secret_generations',{})
    if not isinstance(generations,dict) or len(generations)>32 or any(not re.fullmatch('[a-z][a-z0-9-]{0,63}',alias) or not isinstance(generation,str) or not re.fullmatch('[a-f0-9]{32}',generation) for alias,generation in generations.items()):
        raise ValueError('delivery requires explicit logical secret generation identities')
    for key in ['backup', 'readiness']:
        if data.get(key):
            relative(Path(root), data[key], exists=True)
    for key in ['upload_backup', 'diagnostics', 'combined', 'readiness_operation','notify']:
        if type(data.get(key, False)) is not bool:
            raise ValueError('delivery options must be literal booleans')
    if data.get('readiness_operation') and (data.get('readiness') or not config.get('operations')):
        raise ValueError('operational readiness requires its one native production policy')
    if data.get('combined') and (data['publish_runner'] != data['deploy_runner'] or data['publish_timeout'] != data['deploy_timeout']):
        raise ValueError('combined publication and delivery retain one existing runner and total job deadline')
    public=production_gates(root,config)
    if public and not data.get('combined'):
        raise ValueError('public release verification requires the accepted packet in the combined delivery job')
    return {**data,'godot_production':bool(public)}


def public_verification(data, record, tools, *, manual=False):
    ref = data['komizo_ref']
    lines = ['  manual_public_verification:' if manual else '  public_verification:',
             '    name: Verify production']
    if manual:
        lines += ["    if: github.ref == 'refs/heads/main' && github.event_name == 'workflow_dispatch' && inputs.mode == 'verify'"]
    else:
        lines += ['    needs: deploy', "    if: needs.deploy.result == 'success' && needs.deploy.outputs.phase == 'ready'"]
    lines += ['    uses: nicodes/komizo-actions/.github/workflows/verify-godot-production.yml@'+ref,
              '    permissions:', '      contents: read', '    with:', '      cookbook-ref: '+tools,
              '      komizo-ref: '+ref, '      source: ${{ github.sha }}',
              '      runner: '+record['runner'], '      timeout-minutes: '+str(record['timeout_minutes'])]
    if manual:
        lines += ['      expected-source: ${{ inputs.source_commit }}', '      release-id: ${{ inputs.release_id }}']
    else:
        lines += ['      release-id: ${{ needs.deploy.outputs.public-release-id }}']
    return lines


def verification_entry(root, config, tools, workflow):
    from operational_workflows import production_verification

    native = production_verification(root, tools, config)
    public = production_gates(root, config)
    if native is None and not public:
        return workflow, False
    options = ['      mode:', '        description: Deploy or verify the live application', '        type: choice',
               '        options: [deploy, verify]', '        default: deploy']
    if native:
        options.extend(native['inputs'])
    if public:
        options += ['      source_commit:', '        description: Expected deployed source for verify mode', '        type: string',
                    '      release_id:', '        description: Expected ordinary payload identity for verify mode', '        type: string']
    workflow = workflow.replace('    inputs:\n', '    inputs:\n'+'\n'.join(options)+'\n', 1)
    if native and native['schedule']:
        schedule = ['  schedule:', *['    - cron: '+json.dumps(cron) for cron in native['schedule']]]
        workflow = workflow.replace('  workflow_dispatch:\n', '\n'.join(schedule)+'\n  workflow_dispatch:\n', 1)
    condition = ' && '.join(("github.ref == 'refs/heads/main'", DEPLOYMENT_EVENT))
    workflow = workflow.replace('    name: Test\n', '    name: Test\n    if: '+condition+'\n', 1)
    group = native['group'] if native else 'production-browser-verification'
    queue = '\n  queue: '+native['queue'] if native and native['queue'] else ''
    group_expression = ("  group: ${{ (inputs.mode == 'verify' || github.event_name == 'schedule') && '"+group+"' || "
                        "format('deploy-source-{0}-{1}', github.sha, inputs.source || 'current') }}")
    workflow = workflow.replace("  group: deploy-source-${{ github.sha }}-${{ inputs.source || 'current' }}", group_expression, 1)
    workflow = workflow.replace('  cancel-in-progress: false', '  cancel-in-progress: false'+queue, 1)
    workflow = workflow.replace('Deploy · ${{ inputs.source || github.ref_name }}',
        "Deploy · ${{ (inputs.mode == 'verify' || github.event_name == 'schedule') && 'Verify production' || inputs.source || github.ref_name }}", 1)
    lines = list(native['jobs']) if native else []
    if public:
        lines += public_verification(declaration(root, config), public[0], tools, manual=True)
    return workflow + '\n'.join(lines)+'\n', True


def jobs(root, config, tools, *, verification=False):
    data = declaration(root, config)
    validation = 'validate'
    ref = data['komizo_ref']
    combined = data.get('combined', False)
    condition = "github.ref == 'refs/heads/main' && github.event_name != 'pull_request' && (inputs.source == '' || inputs.source == github.sha)"
    if verification:
        condition += ' && '+DEPLOYMENT_EVENT
    lines = []
    if not combined:
        lines += ['  publish:', '    name: Publish', '    needs: ' + validation,
            '    if: ' + condition,
            '    uses: nicodes/komizo-actions/.github/workflows/publish.yml@' + ref,
            '    permissions:', '      contents: read', '      actions: read', '      packages: write',
            '      attestations: read', '    with:', '      cookbook-ref: ' + tools,
            '      komizo-ref: ' + ref, '      source: ${{ github.sha }}',
            '      project: ' + data['project'], '      runner: ' + data['publish_runner'],
            '      timeout-minutes: ' + str(data['publish_timeout'])]
    lines += ['  deploy:', '    name: Deploy',
        '    needs: ' + (validation if combined else 'publish'), '    if: ' + condition,
        '    uses: nicodes/komizo-actions/.github/workflows/deploy.yml@' + ref,
        '    permissions:', '      contents: read', '      actions: read',
        '      packages: ' + ('write' if combined else 'read'), '      attestations: read',
        '      id-token: write', '    with:', '      cookbook-ref: ' + tools,
        '      komizo-ref: ' + ref, '      project: ' + data['project'],
        '      declaration: ' + config['delivery']['definition'], '      source: ${{ github.sha }}',
        '      runner: ' + data['deploy_runner'], '      timeout-minutes: ' + str(data['deploy_timeout']),
        '      environment: ' + data.get('environment', 'production'),
        '      step-timeouts: ' + json.dumps(json.dumps(deadlines(data), sort_keys=True)),
        '      combined: ' + str(combined).lower(), '    secrets:',
        '      KOMIZO_DEPLOY_KEY: ${{ secrets.KOMIZO_DEPLOY_KEY }}']
    if data.get('upload_backup'):
        for key in ['ACCESS_KEY', 'SECRET_KEY']:
            lines += ['      BACKUP_S3_' + key + ': ${{ secrets.BACKUP_S3_' + key + ' }}']
    public=production_gates(root,config)
    if public:
        lines += public_verification(data, public[0], tools)
    reporting = data.get('report', {}) is not False
    notifying = data.get('notify', True)
    if not reporting and not notifying:
        return '\n'.join(lines) + '\n'
    report = report_settings(data, 'report', ci_settings(config)['runner'])
    notification = report_settings(data, 'notification', ci_settings(config)['runner'])
    needs = ['validate', *([] if combined else ['publish']), 'deploy', *(['public_verification'] if public else [])]
    readiness = "needs.deploy.outputs.phase == 'ready'"
    if public:
        readiness += " && needs.public_verification.result == 'success'"
    outcome = ("needs.deploy.result == 'success' && (needs.deploy.outputs.phase == 'prepared_stopped' && 'prepared_stopped' || "
        + readiness + " && 'success' || 'failure') || needs.deploy.result")
    common_condition = 'always() && !cancelled() && (' + condition + ')'
    if reporting:
        failed = ("needs.validate.result != 'success' || needs." + ('deploy' if combined else 'publish')
            + ".result != 'success' || !contains(fromJSON('[\"success\",\"prepared_stopped\"]'), (" + outcome + '))')
        lines += ['  report:', '    name: Report failure', '    needs: [' + ', '.join(needs) + ']',
            '    if: ' + common_condition + ' && (' + failed + ')',
            '    uses: nicodes/komizo-actions/.github/workflows/report-failure.yml@' + ref,
            '    permissions:', '      contents: read', '      issues: write',
            '    with:', '      cookbook-ref: ' + tools, '      komizo-ref: ' + ref,
            '      runner: ' + report['runner'], '      timeout-minutes: ' + str(report['timeout_minutes']),
            '      step-timeouts: ' + json.dumps(json.dumps(report['step_timeouts'], sort_keys=True))]
    if notifying:
        lines += ['  notify:', '    name: Notify', '    needs: [' + ', '.join(needs) + ']',
            '    if: ' + common_condition,
            '    uses: nicodes/komizo-actions/.github/workflows/notify.yml@' + ref,
            '    permissions:', '      contents: read', '    with:',
            '      cookbook-ref: ' + tools, '      komizo-ref: ' + ref,
            '      validation: ${{ needs.validate.result }}',
            '      publication: ${{ needs.' + ('deploy' if combined else 'publish') + '.result }}',
            '      deployment: ${{ ' + outcome + ' }}',
            '      deployment-url: ' + json.dumps(data['notification_url']),
            '      environment: ' + notification.get('environment', data.get('environment', 'production')),
            '      notify-runner: ' + notification['runner'], '      notify-timeout: ' + str(notification['timeout_minutes']),
            '      notify-step-timeouts: ' + json.dumps(json.dumps(notification['step_timeouts'], sort_keys=True)),
            '    secrets:', '      DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}']
    return '\n'.join(lines) + '\n'


JOB_CAP = 360
SINGLE_STEPS = ['artifact', 'build', 'capture', 'checkout', 'connect', 'deploy', 'download', 'plan',
                'prepare', 'publish', 'readiness', 'setup', 'test', 'upload']


def single_deadlines(data, source):
    """Step deadlines of the one deliver job; source holds the Build and test budgets (job, prepare, build, test)."""
    stages = deadlines(data)
    declared = data.get('step_timeouts', {})
    result = {name: stages[name] for name in SINGLE_STEPS if name in stages}
    result['prepare'] = declared.get('prepare', source['prepare'])
    result['checkout'] = declared.get('checkout', min(5, data['deploy_timeout']))
    result['build'], result['test'] = source['build'], source['test']
    result['publish'] = declared.get('publish', data['deploy_timeout'] if data.get('combined') else data['publish_timeout'])
    # Report and notify are steps of the one job: only their step deadlines carry over (runner and job budget do not).
    for key, step in [('report', 'report'), ('notification', 'notify')]:
        value = data.get(key)
        if isinstance(value, dict) and step in value.get('step_timeouts', {}):
            result[step] = value['step_timeouts'][step]
    return {name: result[name] for name in sorted(result)}


def single_job(root, config, tools):
    """One Deploy caller: the reusable workflow builds, tests and delivers in a single job."""
    import single_job_workflows
    data = declaration(root, config)
    if production_gates(root, config):
        raise ValueError('production gates (ci.godot gates of kind production) are not yet supported in single-job mode')
    source = single_job_workflows.budgets(config)
    validation = data.get('validation_timeout_minutes')
    if validation:
        source = {**source, 'job': validation}
    combined = data.get('combined', False)
    total = min(JOB_CAP, source['job'] + data['deploy_timeout'] + (0 if combined else data['publish_timeout']))
    ref = data['komizo_ref']
    lines = ['# Generated cookbook caller. Change recipe data and regenerate.', 'name: Deploy',
        'run-name: "Deploy · ${{ inputs.source || github.ref_name }}"', "'on':",
        '  push:', '    branches: [main]', '  workflow_dispatch:', '    inputs:', '      source:',
        '        description: Exact source commit; defaults to this main revision', '        type: string',
        "        default: ''", '      cookbook:', '        description: Accepted cookbook commit; defaults to the pinned recipe',
        '        type: string', "        default: ''",
        'permissions: {}', 'concurrency:', '  group: deploy-source-${{ github.sha }}', '  cancel-in-progress: false',
        'jobs:']
    condition = "github.ref == 'refs/heads/main' && (inputs.source == '' || inputs.source == github.sha)"
    # A gate that needs another runner image cannot run inside the one deliver job. Legacy Deploy re-ran every gate
    # on main before publishing, so each such gate keeps one job here that must pass before the deliver job starts.
    gates = single_job_workflows.gate_job_texts(config, ci_settings(config), tools, source,
        cookbook="${{ inputs.cookbook || '" + tools + "' }}", source='${{ inputs.source || github.sha }}',
        candidate='inputs.source || github.sha', condition=condition)
    for _, text in gates:
        lines += text.rstrip('\n').split('\n')
    if gates and data.get('report', {}) is not False:
        report = report_settings(data, 'report', ci_settings(config)['runner'])
        lines += ['  report:', '    name: Report failure', '    needs: [' + ', '.join(job['id'] for job, _ in gates) + ']',
            '    if: always() && !cancelled() && (' + condition + ") && contains(needs.*.result, 'failure')",
            '    uses: nicodes/komizo-actions/.github/workflows/report-failure.yml@' + ref,
            '    permissions:', '      contents: read', '      issues: write',
            '    with:', '      cookbook-ref: ' + tools, '      komizo-ref: ' + ref,
            '      runner: ' + report['runner'], '      timeout-minutes: ' + str(report['timeout_minutes']),
            '      step-timeouts: ' + json.dumps(json.dumps(report['step_timeouts'], sort_keys=True))]
    lines += ['  deploy:', '    name: Deploy']
    if gates:
        lines.append('    needs: [' + ', '.join(job['id'] for job, _ in gates) + ']')
    lines += ['    if: ' + condition,
        '    uses: nicodes/komizo-actions/.github/workflows/deliver.yml@' + ref,
        '    permissions:', '      contents: read', '      actions: read', '      pull-requests: read',
        '      packages: write', '      attestations: read', '      id-token: write', '      issues: write',
        '    with:', "      cookbook-ref: ${{ inputs.cookbook || '" + tools + "' }}", '      komizo-ref: ' + ref,
        '      project: ' + data['project'], '      declaration: ' + config['delivery']['definition'],
        '      runner: ' + data['deploy_runner'], '      timeout-minutes: ' + str(total),
        # One budget covers backup, publish and deploy together; combined delivery already includes publication.
        '      deploy-timeout-minutes: ' + str(min(JOB_CAP, data['deploy_timeout'] + (0 if combined else data['publish_timeout']))),
        '      environment: ' + data.get('environment', 'production'),
        '      deployment-url: ' + json.dumps(data['notification_url']),
        '      step-timeouts: ' + json.dumps(json.dumps(single_deadlines(data, source), sort_keys=True))]
    notifying = data.get('notify', True)
    if not notifying:
        lines.append('      notify: false')
    if data.get('report', {}) is False:
        lines.append('      report: false')
    lines += ['    secrets:', '      KOMIZO_DEPLOY_KEY: ${{ secrets.KOMIZO_DEPLOY_KEY }}']
    if data.get('upload_backup'):
        for key in ['ACCESS_KEY', 'SECRET_KEY']:
            lines.append('      BACKUP_S3_' + key + ': ${{ secrets.BACKUP_S3_' + key + ' }}')
    if notifying:
        lines.append('      DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}')
    return '\n'.join(lines) + '\n'
