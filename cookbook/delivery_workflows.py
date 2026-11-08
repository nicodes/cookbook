"""Generate thin Komizo delivery callers after the same-run recipe acceptance."""
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from configuration import relative, read_source_data


def deadlines(data):
    values=data.get('step_timeouts',{})
    names={'checkout','setup','plan','registry','inspect','prepare','connect','capture','download','deploy','readiness','diagnostics','artifact','upload'}
    if not isinstance(values,dict) or set(values)-names:raise ValueError('delivery step deadlines require implemented native phases')
    result={name:values.get(name,min(3,data['deploy_timeout']) if name in {'readiness','diagnostics','registry','connect'} else min(5,data['deploy_timeout']) if name in {'setup','plan','download'} else min(10,data['deploy_timeout']) if name in {'artifact','upload'} else min(15,data['deploy_timeout']) if name=='capture' else data['deploy_timeout']) for name in names}
    if any(type(value) is not int or not 1<=value<=data['deploy_timeout'] for value in result.values()):
        raise ValueError('delivery step deadlines must stay within the original job budget')
    return result


def report_settings(data,key):
    defaults={'report':dict(runner='ubuntu-slim',timeout_minutes=10,step_timeouts={'checkout':10,'setup':5,'report':10}),
        'notification':dict(runner='ubuntu-slim',timeout_minutes=2,step_timeouts={'checkout':2,'notify':1})}
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
    for key in ['report','notification']:report_settings(data,key)
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
    return data


def jobs(root, config, tools):
    data = declaration(root, config)
    import dependency_security
    validation = '[validate, dependency-security]' if dependency_security.declaration(config) else 'validate'
    ref = data['komizo_ref']
    combined = data.get('combined', False)
    condition = "github.ref == 'refs/heads/main' && github.event_name != 'pull_request' && (inputs.source == '' || inputs.source == github.sha)"
    lines = []
    if not combined:
        lines += ['  publish:', '    name: Publish accepted release', '    needs: ' + validation,
            '    if: ' + condition,
            '    uses: nicodes/komizo-actions/.github/workflows/cookbook-publish.yml@' + ref,
            '    permissions:', '      contents: read', '      actions: read', '      packages: write',
            '      attestations: read', '    with:', '      cookbook-ref: ' + tools,
            '      komizo-ref: ' + ref, '      source: ${{ github.sha }}',
            '      project: ' + data['project'], '      runner: ' + data['publish_runner'],
            '      timeout-minutes: ' + str(data['publish_timeout'])]
    lines += ['  deploy:', '    name: Deploy accepted release',
        '    needs: ' + (validation if combined else 'publish'), '    if: ' + condition,
        '    uses: nicodes/komizo-actions/.github/workflows/cookbook-deliver.yml@' + ref,
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
    reporting = data.get('report', {}) is not False
    notifying = data.get('notify', True)
    if not reporting and not notifying:
        return '\n'.join(lines) + '\n'
    report = report_settings(data, 'report')
    notification = report_settings(data, 'notification')
    needs = ['validate', *([] if combined else ['publish']), 'deploy']
    lines += ['  report:', '    needs: [' + ', '.join(needs) + ']',
        '    if: always() && !cancelled() && (' + condition + ')',
        '    uses: nicodes/komizo-actions/.github/workflows/cookbook-' + ('report' if reporting else 'notify') + '.yml@' + ref,
        '    permissions:', '      contents: read']
    if reporting:
        lines += ['      issues: write']
    lines += ['    with:', '      cookbook-ref: ' + tools, '      komizo-ref: ' + ref,
        '      validation: ${{ needs.validate.result }}',
        '      publication: ${{ needs.' + ('deploy' if combined else 'publish') + '.result }}',
        '      deployment: ${{ needs.deploy.result }}',
        '      deployment-url: ' + json.dumps(data['notification_url']),
        '      notify: ' + str(notifying).lower(),
        '      environment: ' + notification.get('environment', data.get('environment', 'production')),
        '      report-runner: ' + report['runner'], '      report-timeout: ' + str(report['timeout_minutes']),
        '      report-step-timeouts: ' + json.dumps(json.dumps(report['step_timeouts'], sort_keys=True)),
        '      notify-runner: ' + notification['runner'], '      notify-timeout: ' + str(notification['timeout_minutes']),
        '      notify-step-timeouts: ' + json.dumps(json.dumps(notification['step_timeouts'], sort_keys=True))]
    if notifying:
        lines += ['    secrets:', '      DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}']
    return '\n'.join(lines) + '\n'
