"""Compose thin Komizo delivery callers after the same-run recipe acceptance."""
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


def historical_deadlines(data):
    values=deadlines(data)
    return {name:values[name] for name in ['checkout','setup','plan','registry','inspect','deploy','diagnostics']}


def declaration(root, config):
    value = config.get('delivery')
    if not isinstance(value, dict) or set(value) != {'definition'}:
        raise ValueError('delivery requires its explicit native declaration')
    path = relative(Path(root), value['definition'], exists=True)
    if path.stat().st_size > 32768:
        raise ValueError('delivery declaration exceeds its bound')
    data = read_source_data(path, maximum=32768)
    keys = {'version', 'project', 'komizo_ref', 'publish_runner', 'publish_timeout',
            'deploy_runner', 'deploy_timeout', 'health_urls', 'secrets', 'variables',
            'backup', 'upload_backup', 'readiness', 'diagnostics', 'notification_url', 'combined', 'mode', 'static',
            'environment','step_timeouts','readiness_operation','notify','report','notification','optional_secrets',
            'service_env_profile','expected_generation','validation_timeout_minutes','historical_deploy'}
    if not isinstance(data, dict) or set(data) - keys or data.get('version') != 1:
        raise ValueError('unsupported delivery declaration')
    mode = data.get('mode', 'application-images')
    if mode not in {'application-images', 'godot-static'} or (mode == 'application-images' and 'static' in data):
        raise ValueError('delivery mode must select its implemented publication contract')
    if mode == 'godot-static':
        import godot_static
        godot_static.declaration(Path(root), config)
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
    if type(data.get('historical_deploy',False)) is not bool:raise ValueError('historical deployment requires its explicit native capability')
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
    for key in ['secrets', 'variables','optional_secrets']:
        mappings = data.get(key, {})
        if not isinstance(mappings, dict) or any(not re.fullmatch('KOMIZO_SECRET_[A-Z][A-Z0-9_]*', name)
                or not isinstance(source, str) or not re.fullmatch('[A-Z][A-Z0-9_]*', source)
                for name, source in mappings.items()):
            raise ValueError('delivery maps explicit host secrets to named caller inputs')
    if set(data.get('secrets', {})) & set(data.get('variables', {})):
        raise ValueError('host inputs cannot have two secret authorities')
    if set(data.get('optional_secrets',{}))&(set(data.get('secrets',{}))|set(data.get('variables',{}))):
        raise ValueError('optional host tokens cannot replace required secret authority')
    profile=data.get('service_env_profile','');generation=data.get('expected_generation','')
    if profile:
        if (not isinstance(profile,str) or not re.fullmatch('[a-z][a-z0-9-]{0,62}',profile)
                or not isinstance(generation,str) or not re.fullmatch('[a-f0-9]{32}',generation)
                or any(data.get(key) for key in ['secrets','variables','optional_secrets'])):
            raise ValueError('scoped delivery retains one native profile generation without transporting profile values')
    elif generation or 'service_env_profile' in data or 'expected_generation' in data:
        raise ValueError('a scoped delivery generation requires its explicit native profile')
    if data.get('historical_deploy') and (mode!='application-images' or not profile):
        raise ValueError('manual historical application deployment requires its original fixed scoped profile')
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
    validation_needs = '[validate, dependency-security]' if dependency_security.declaration(config) else 'validate'
    if data.get('mode') == 'godot-static':
        import godot_static_workflows
        return godot_static_workflows.jobs(root, config, tools).replace('    needs: validate\n', '    needs: ' + validation_needs + '\n', 1)
    ref=data['komizo_ref'];limits=deadlines(data);combined=data.get('combined',False)
    source_condition="github.ref == 'refs/heads/main' && github.event_name != 'pull_request' && (inputs.source == '' || inputs.source == github.sha)"
    condition=source_condition
    historical=data.get('historical_deploy',False)
    if historical:condition+=" && (inputs.version == '' || inputs.version == github.sha)"
    lines=[]
    if not combined:
        lines+=['  publish:','    name: Publish accepted images','    needs: '+validation_needs,'    if: '+condition,
            '    uses: nicodes/komizo-actions/.github/workflows/cookbook-publish.yml@'+ref,
            '    permissions:','      contents: read','      actions: read','      packages: write','      attestations: read',
            '    with:','      cookbook-ref: '+tools,'      komizo-ref: '+ref,'      source: ${{ github.sha }}',
            '      project: '+data['project'],'      runner: '+data['publish_runner'],
            '      timeout-minutes: '+str(data['publish_timeout'])]
    lines+=['  deploy:','    name: Deploy accepted images','    needs: '+(validation_needs if combined else 'publish'),
        '    if: '+condition,'    runs-on: '+data['deploy_runner'],'    timeout-minutes: '+str(data['deploy_timeout']),
        '    environment: '+data.get('environment','production'),'    concurrency:','      group: deploy-production',
        '      cancel-in-progress: false','    permissions:','      contents: read','      actions: read',
        '      packages: '+('write' if combined else 'read'),'      attestations: read','      id-token: write',
        '    steps:','      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1',
        '        timeout-minutes: '+str(limits['checkout']),'        with:','          persist-credentials: false','          fetch-depth: 0']
    if combined:
        lines+=['      - uses: nicodes/komizo-actions/application-publication@'+ref,
            '        with:','          cookbook-ref: '+tools,'          komizo-ref: '+ref,'          source: ${{ github.sha }}',
            '          project: '+data['project'],'        env:','          GH_TOKEN: ${{ github.token }}']
    lines+=['      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1',
        '        timeout-minutes: '+str(limits['setup']),'        with:','          repository: nicodes/cookbook',
        '          ref: '+tools,'          path: .artifacts/recipe-sdks/_cookbook','          persist-credentials: false',
        '      - uses: ./.artifacts/recipe-sdks/_cookbook/sdk-dependencies',
        '        timeout-minutes: '+str(limits['setup']),'        with:','          cookbook-ref: '+tools,
        '      - uses: ./.artifacts/recipe-sdks/_cookbook/sdk-setup',
        '        timeout-minutes: '+str(limits['setup']),
        '        with:','          tools: python','        env:','          GH_TOKEN: ${{ github.token }}',
        '      - uses: nicodes/komizo-actions/application-delivery@'+ref,'        id: delivery',
        '        timeout-minutes: '+str(limits['plan']),'        with:','          cookbook-ref: '+tools,
        '          project: '+data['project'],'          declaration: '+config['delivery']['definition'],
        '          plan-only: true','          sdk-prepared: true','        env:','          GH_TOKEN: ${{ github.token }}',
        "          PYTHONDONTWRITEBYTECODE: '1'",
        '      - uses: nicodes/komizo-actions/connect@'+ref,
        '        timeout-minutes: '+str(limits['connect']),'        with:','          user: komizo-'+data['project'],
        '        env:',
        '          KOMIZO_SERVER_URL: ${{ vars.KOMIZO_SERVER_URL }}',
        '          KOMIZO_DEPLOY_KEY: ${{ secrets.KOMIZO_DEPLOY_KEY }}','          KOMIZO_KNOWN_HOSTS: ${{ vars.KOMIZO_KNOWN_HOSTS }}']
    if data.get('backup'):
        lines+=['      - uses: nicodes/komizo-actions/application-backup-export@'+ref,'        id: backup',
            '        timeout-minutes: '+str(limits['capture']),'        with:',
            '          project: '+data['project'],'          config: '+data['backup']]
    if data.get('upload_backup'):
        lines+=['      - uses: nicodes/komizo-actions/application-backup-upload@'+ref,
            "        if: steps.backup.outputs.has-export == 'true'",'        timeout-minutes: '+str(limits['upload']),
            '        with:','          project: '+data['project'],'        env:',
            '          BACKUP_ALLOWED_SLUGS: '+json.dumps(json.dumps([data['project']]))]
        for key in ['ACCESS_KEY','SECRET_KEY']:lines+=['          BACKUP_S3_'+key+': ${{ secrets.BACKUP_S3_'+key+' }}']
        for key in ['HOSTNAME','BUCKET']:lines+=['          BACKUP_S3_'+key+': ${{ vars.BACKUP_S3_'+key+' }}']
    if data.get('backup'):
        lines+=['      - uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9',
            '        timeout-minutes: '+str(limits['artifact']),'        with:',
            '          name: backup-${{ github.run_id }}-${{ github.run_attempt }}','          path: .artifacts/backup/',
            '          if-no-files-found: error','          include-hidden-files: true','          retention-days: 30']
    if data.get('optional_secrets'):
        lines+=['      - uses: nicodes/komizo-actions/optional-token-secrets@'+ref,
            '        timeout-minutes: '+str(limits['prepare']),'        env:']
        for name,source in sorted(data['optional_secrets'].items()):
            lines+=['          '+name.replace('KOMIZO_SECRET_','KOMIZO_OPTIONAL_SECRET_',1)+': ${{ secrets.'+source+' }}']
    lines+=['      - uses: actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333',
        '        timeout-minutes: '+str(limits['download']),'        with:',
        '          name: host-release-${{ github.sha }}','          path: .artifacts/komizo-release']
    lines+=['      - uses: nicodes/komizo-actions/deploy@'+ref,'        timeout-minutes: '+str(limits['deploy']),
        '        with:','          release-manifest: .artifacts/komizo-release/manifest.json','          version: ${{ github.sha }}',
        '          registry-user: ${{ github.repository_owner }}','          registry-token: ${{ github.token }}',
        "          health-urls: ${{ steps.delivery.outputs['health-urls'] }}"]
    if data.get('service_env_profile'):
        lines+=['          service-env-profile: '+data['service_env_profile'],
            '          expected-generation: '+data['expected_generation']]
    lines+=['        env:',
        '          GH_TOKEN: ${{ github.token }}','          KOMIZO_APP_NAME: ${{ vars.KOMIZO_APP_NAME }}',
        '          KOMIZO_SERVER_URL: ${{ vars.KOMIZO_SERVER_URL }}','          KOMIZO_DEPLOY_KEY: ${{ secrets.KOMIZO_DEPLOY_KEY }}',
        '          KOMIZO_KNOWN_HOSTS: ${{ vars.KOMIZO_KNOWN_HOSTS }}']
    for key,context in [('secrets','secrets'),('variables','vars')]:
        for name,source in sorted(data.get(key,{}).items()):lines+=['          '+name+': ${{ '+context+'.'+source+' }}']
    if data.get('readiness') or data.get('readiness_operation'):
        lines+=['      - uses: nicodes/komizo-actions/application-readiness@'+ref,
            '        timeout-minutes: '+str(limits['readiness']),'        with:',
            '          declaration: '+config['delivery']['definition'],'        env:',
            '          GH_TOKEN: ${{ github.token }}','          EXPECTED_REVISION: ${{ github.sha }}',
            "          PYTHONDONTWRITEBYTECODE: '1'"]
    if data.get('diagnostics'):
        lines+=['      - name: Bounded deployment diagnostics','        if: failure()','        continue-on-error: true',
            '        timeout-minutes: '+str(limits['diagnostics']),'        shell: bash',
            '        env:','          PROJECT: '+data['project'],
            '        run: ssh -o BatchMode=yes -o ConnectTimeout=15 deploy-target doas "/usr/local/bin/diagnose-$PROJECT" 200']
    if historical:
        lines+=['  rollback:',
            "    if: github.event_name == 'workflow_dispatch' && ("+source_condition+") && inputs.version != '' && inputs.version != github.sha",
            '    uses: nicodes/komizo-actions/.github/workflows/cookbook-historical-deploy.yml@'+ref,
            '    permissions:','      contents: read','      actions: read','      packages: read','      attestations: read',
            '    with:','      cookbook-ref: '+tools,'      komizo-ref: '+ref,
            '      declaration: '+json.dumps(config['delivery']['definition']),'      version: ${{ inputs.version }}',
            '      runner: '+data['deploy_runner'],'      timeout-minutes: '+str(data['deploy_timeout']),
            '      environment: '+data.get('environment','production'),
            '      step-timeouts: '+json.dumps(json.dumps(historical_deadlines(data),sort_keys=True)),
            '    secrets:','      KOMIZO_DEPLOY_KEY: ${{ secrets.KOMIZO_DEPLOY_KEY }}']
    reporting=data.get('report',{}) is not False
    notifying=data.get('notify',True)
    if not reporting and not notifying:return '\n'.join(lines)+'\n'
    report=report_settings(data,'report');notification=report_settings(data,'notification')
    needs=['validate',*([] if combined else ['publish']),'deploy',*(['rollback'] if historical else [])]
    deployment="needs.rollback.result == 'success' && 'success' || needs.rollback.result == 'failure' && 'failure' || needs.deploy.result" if historical else 'needs.deploy.result'
    lines+=['  report:','    needs: ['+', '.join(needs)+']',
        '    if: always() && !cancelled() && ('+source_condition+')',
        '    uses: nicodes/komizo-actions/.github/workflows/cookbook-'+('report' if reporting else 'notify')+'.yml@'+ref,
        '    permissions:','      contents: read']
    if reporting:lines+=['      issues: write']
    lines+=['    with:','      cookbook-ref: '+tools,
        '      komizo-ref: '+ref,'      validation: ${{ needs.validate.result }}',
        '      publication: ${{ needs.'+('deploy' if combined else 'publish')+'.result }}',
        '      deployment: ${{ '+deployment+' }}','      deployment-url: '+json.dumps(data['notification_url']),
        '      notify: '+str(notifying).lower(),'      environment: '+notification.get('environment',data.get('environment','production')),
        '      report-runner: '+report['runner'],'      report-timeout: '+str(report['timeout_minutes']),
        '      report-step-timeouts: '+json.dumps(json.dumps(report['step_timeouts'],sort_keys=True)),
        '      notify-runner: '+notification['runner'],'      notify-timeout: '+str(notification['timeout_minutes']),
        '      notify-step-timeouts: '+json.dumps(json.dumps(notification['step_timeouts'],sort_keys=True))]
    if historical:lines+=['      revision: ${{ needs.rollback.outputs.version || github.sha }}']
    if notifying:lines+=['    secrets:','      DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}']
    return '\n'.join(lines)+'\n'
