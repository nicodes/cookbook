"""Generate one trusted-main Preview contract around readonly native recipe acceptance."""
import json
from pathlib import Path
import re
import sys
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent))
from configuration import load, relative, ci_settings, validate_data, release_definition, read_source_data
import images
import deployment

SHA = re.compile('[a-f0-9]{40}')
NAME = re.compile('[A-Z][A-Z0-9_]{0,79}')
SLUG = re.compile('[a-z][a-z0-9-]{0,62}')
SECRET_NAMES = {'CLERK_SECRET_KEY', 'OPENROUTER_API_KEY'}


def declaration(root, config):
    value = config.get('preview')
    if not isinstance(value, dict) or set(value) != {'definition'}:
        raise ValueError('Preview requires one native source declaration')
    path = relative(Path(root), value['definition'], exists=True)
    if not path.is_file() or path.stat().st_size > 32768:
        raise ValueError('Preview declaration must be a bounded regular source file')
    data = read_source_data(path, maximum=32768)
    keys = {'version', 'project', 'komizo_ref', 'ttl_seconds', 'runner', 'up_timeout_minutes',
            'down_timeout_minutes', 'publish_timeout_minutes', 'origin_variables',
            'environment', 'secrets', 'health_paths', 'origin_layout', 'clerk_urls', 'publish_runner', 'validation_timeout_minutes', 'producer_timeout_minutes'}
    if not isinstance(data, dict) or set(data)-keys or data.get('version') != 1:
        raise ValueError('unsupported Preview declaration')
    if config.get('recipe') not in {'react-native','godot-game'}:
        raise ValueError('this Preview acceptance requires an implemented application recipe')
    if not SLUG.fullmatch(data.get('project', '')) or not SHA.fullmatch(data.get('komizo_ref', '')):
        raise ValueError('Preview requires a source-bound project and reviewed immutable Komizo pin')
    definition = images.definition(root, config)
    if data['project'] != definition['project']:
        raise ValueError('Preview project differs from native release authority')
    workload = deployment.declaration(root)['workload']
    backend = any(route.get('target', {}).get('kind') == 'service' for route in workload['routes'])
    if type(data.get('ttl_seconds')) is not int or not 60 <= data['ttl_seconds'] <= 259200:
        raise ValueError('Preview requires an explicit bounded native retention lifetime')
    runner = data.get('runner', '')
    if not isinstance(runner,str) or not re.fullmatch('[A-Za-z0-9_.-]{1,80}',runner):
        raise ValueError('Preview must preserve its source-declared runner')
    for key, maximum in [('up_timeout_minutes',35),('down_timeout_minutes',35),('publish_timeout_minutes',180)]:
        if type(data.get(key)) is not int or not 1 <= data[key] <= maximum:
            raise ValueError('Preview must preserve bounded native effect/publication deadlines')
    publish_runner=data.get('publish_runner',runner)
    if not isinstance(publish_runner,str) or not re.fullmatch('[A-Za-z0-9_.-]{1,80}',publish_runner):
        raise ValueError('Preview publication preserves its explicit native runner')
    for key in ['validation_timeout_minutes','producer_timeout_minutes']:
        if key in data and (type(data[key]) is not int or not 1<=data[key]<=180):
            raise ValueError('Preview preserves explicit source/publication deadlines')
    clerk_urls = data.get('clerk_urls', {})
    if (not isinstance(clerk_urls,dict) or (clerk_urls and not backend)
            or set(clerk_urls)-{'CLERK_ISSUER','CLERK_JWKS_URL'}
            or any(not isinstance(value,str) or value not in {'issuer','jwks'} for value in clerk_urls.values())):
        raise ValueError('Preview Clerk URLs select only declared development issuer and JWKS carriers')
    if backend and config['recipe']=='react-native':
        app=config.get('components',{}).get('app',{})
        if (config.get('ci',{}).get('preview_environment')!='Preview'
                or app.get('preview_environment',{}).get('EXPO_PUBLIC_API_URL') not in {'', '/api'}
                or app.get('preview_variables')!={'EXPO_PUBLIC_CLERK_PUBLISHABLE_KEY':'COOKBOOK_PREVIEW_CLERK_KEY'}):
            raise ValueError('native Preview builds must retain same-origin API and the bounded development public key')
        if app['preview_environment']['EXPO_PUBLIC_API_URL']=='/api':
            frontends=[route for route in workload['routes'] if route.get('target',{}).get('kind')=='asset']
            if not frontends or not any(route['host']==frontends[0]['host'] and route['path']=='/api/*'
                    and route.get('target',{}).get('kind')=='service' for route in workload['routes']):
                raise ValueError('Preview /api frontend requires an explicit app-host /api/* service route')
    origins = data.get('origin_variables', {})
    if (not isinstance(origins,dict) or len(origins)>16 or (backend and not origins) or (not backend and origins)
            or any(not isinstance(name,str) or not NAME.fullmatch(name)
                or name.startswith(('GITHUB_','MISE_','COOKBOOK_','KOMIZO_','TOOLS_','REQUEST_')) for name in origins)):
        raise ValueError('Preview must declare the native origin carriers')
    for name, sources in origins.items():
        if (not isinstance(sources,list) or not sources or len(set(sources))!=len(sources)
                or not set(sources) <= {'app','api'} or 'app' not in sources):
            raise ValueError('native origin carriers must include the resolved app origin')
    secrets = data.get('secrets', ['CLERK_SECRET_KEY'] if backend else [])
    if (not isinstance(secrets,list) or len(set(secrets))!=len(secrets)
            or (backend and 'CLERK_SECRET_KEY' not in secrets) or (not backend and secrets) or not set(secrets)<=SECRET_NAMES):
        raise ValueError('Preview accepts only explicitly named development credentials')
    literals = data.get('environment', {})
    reserved = {'CLERK_SECRET_KEY','CLERK_PUBLISHABLE_KEY','CLERK_AUTHORIZED_PARTIES', *clerk_urls,
                *origins,'RUNTIME_DATABASE_URL','DATABASE_URL','GITHUB_TOKEN','GH_TOKEN'}
    if not isinstance(literals,dict) or len(literals)>16:
        raise ValueError('Preview native environment must be bounded literal data')
    for name,value in literals.items():
        if (not isinstance(name,str) or not NAME.fullmatch(name) or name in reserved
                or any(word in name for word in ['PASSWORD','SECRET','TOKEN','API_KEY'])
                or name.startswith(('GITHUB_','MISE_','COOKBOOK_'))
                or not isinstance(value,str) or len(value.encode())>512 or any(c in value for c in '\r\n\0')):
            raise ValueError('literal Preview settings cannot replace identity, origins or credentials')
    health = data.get('health_paths')
    if (not isinstance(health,dict) or set(health)!=({'app','api'} if backend else {'app'})
            or any(not isinstance(path,str) or not re.fullmatch('/[A-Za-z0-9/_-]*',path) for path in health.values())):
        raise ValueError('Preview health checks require bounded app/API paths')
    mode=data.get('origin_layout','subdomain' if backend else 'resolved')
    if mode not in ({'subdomain'} if backend else {'resolved','subdomain'}):
        raise ValueError('the Clerk Preview contract currently requires distinct PR origins')
    ci=ci_settings(config)
    return {**data, 'secrets':secrets, 'environment':literals, 'origin_layout':mode,
            'definition':config['preview']['definition'], 'recipe':config['recipe'], 'backend':backend,
            'validation_runner':ci['runner'], 'validation_timeout_minutes':data.get('validation_timeout_minutes',ci['timeout_minutes']), 'publish_runner':publish_runner,
            'image_base':definition['image_base'], 'shipping_components':images.components(definition),
            'test_groups':definition['tests'], 'artifact_definition':release_definition(config),
            'release_metadata':config['artifacts'].get('release_metadata', [])}


def workflow_files(root, revision, config=None):
    if not SHA.fullmatch(revision):
        raise ValueError('Preview caller must select an immutable cookbook source')
    config=load(root) if config is None else config
    if not config.get('preview'):
        return {}
    data=declaration(root,config)
    pin=data['komizo_ref']
    expression=lambda text:chr(36)+'{{ '+text+' }}'
    comment="github.event_name == 'issue_comment' && github.event.action == 'created' && github.event.issue.pull_request && contains(fromJSON('[\"OWNER\",\"MEMBER\",\"COLLABORATOR\"]'),github.event.comment.author_association)"
    comment_up="("+comment+" && contains(github.event.comment.body,'/preview') && !contains(github.event.comment.body,'/preview down'))"
    comment_down="("+comment+" && contains(github.event.comment.body,'/preview down'))"
    same="github.event_name == 'pull_request_target' && github.event.pull_request.head.repo.full_name == github.repository && github.event.pull_request.user.login != 'dependabot[bot]'"
    pr_up="("+same+" && contains(fromJSON('[\"synchronize\",\"reopened\"]'),github.event.action) && contains(github.event.pull_request.labels.*.name,'preview') && contains(fromJSON('[\"OWNER\",\"MEMBER\",\"COLLABORATOR\"]'),github.event.pull_request.author_association) && !github.event.pull_request.draft)"
    pr_down="("+same+" && github.event.action == 'closed')"
    number="github.event.pull_request.number || github.event.issue.number"
    potential="("+comment_up+" || "+comment_down+" || "+pr_up+" || "+pr_down+")"
    lines=['# Generated cookbook caller. Change native Preview data and regenerate.',
           'name: PR Preview',"'on':",'  pull_request_target:',
           '    types: [opened, synchronize, reopened, ready_for_review, closed]',
           '  issue_comment:', '    types: [created]', 'permissions: {}','concurrency:',
           '  group: '+expression(potential+" && format('preview-{0}',"+number+") || format('preview-noop-{0}',github.run_id)"),
           '  cancel-in-progress: '+expression(comment_up+' || '+pr_up),'jobs:','  request:',
           '    if: '+expression(potential),
           '    uses: nicodes/komizo-actions/.github/workflows/cookbook-preview-request.yml@'+pin,
           '    permissions:','      contents: read','      pull-requests: read','      id-token: write','    with:',
           '      cookbook-ref: '+revision,'      komizo-ref: '+pin,
           '      declaration: '+json.dumps(data['definition']),'    secrets:',
           '      deploy-key: '+expression('secrets.KOMIZO_DEPLOY_KEY'),'  full:','    needs: request',
           '    if: '+expression("needs.request.outputs.enabled == 'true' && needs.request.outputs.action == 'up'"),
           '    uses: nicodes/cookbook/.github/workflows/cookbook-ci.yml@'+revision,
           '    permissions:','      contents: read','      actions: read','      pull-requests: read','    with:',
           '      cookbook-ref: '+revision,'      source: '+expression('needs.request.outputs.head'),
           '      head: '+expression('needs.request.outputs.head'),'      base: '+expression('needs.request.outputs.base'),
           '      preview-number: '+expression('needs.request.outputs.pr'),
           '      preview-app-origin: '+expression("needs.request.outputs['app-origin']"),
           '      preview-api-origin: '+expression("needs.request.outputs['api-origin']"),'      preview-environment: Preview',
           '      recipe: '+data['recipe'],'      merge-gate: false',
           '      runner: '+data['validation_runner'],'      timeout-minutes: '+str(data['validation_timeout_minutes']),
           '  publish:','    needs: [request, full]',
           '    if: '+expression("needs.request.outputs.enabled == 'true' && needs.request.outputs.action == 'up'"),
           '    uses: nicodes/komizo-actions/.github/workflows/cookbook-preview-publish.yml@'+pin,
           '    permissions:','      contents: read','      actions: read','      pull-requests: read','      packages: write','      attestations: read','      id-token: write',
           '    with:','      cookbook-ref: '+revision,'      komizo-ref: '+pin,
           '      plan: '+expression('needs.request.outputs.plan'),'      runner: '+data['publish_runner'],
           '      timeout-minutes: '+str(data['publish_timeout_minutes'])]
    if data['recipe']=='godot-game' and data['backend']:
        index=lines.index('  publish:')
        lines.insert(index,'      preview-public-key: true')
    for action, needs, budget in [('up','[request, full, publish]',data['up_timeout_minutes']),('down','request',data['down_timeout_minutes'])]:
        lines += ['  '+action+':','    needs: '+needs,
                  '    if: '+expression("needs.request.outputs.enabled == 'true' && needs.request.outputs.action == '"+action+"'"),
                  '    uses: nicodes/komizo-actions/.github/workflows/cookbook-preview-effects.yml@'+pin,
                  '    permissions:','      contents: read','      actions: read','      packages: read',
                  '      pull-requests: write','      attestations: read','      id-token: write','    with:',
                  '      cookbook-ref: '+revision,'      komizo-ref: '+pin,
                  '      plan: '+expression('needs.request.outputs.plan'),'      runner: '+data['runner'],
                  '      timeout-minutes: '+str(budget),'    secrets:',
                  '      deploy-key: '+expression('secrets.KOMIZO_DEPLOY_KEY'),
                  '      discord-webhook: '+expression('secrets.DISCORD_WEBHOOK_URL')]
    return {'.github/workflows/pr-preview.yml':'\n'.join(lines)+'\n'}


def auth_contract(root, config):
    config = validate_data(config)
    if not config.get('preview'):
        return {'present':False}
    data=declaration(root,config)
    import mise_entrypoint
    reference = mise_entrypoint.read(root)['revision']
    for name,expected in workflow_files(root,reference,config).items():
        if relative(root,name,exists=True).read_text()!=expected:
            raise ValueError('Preview source differs from its reviewed environment/origin/workflow contract')
    return {'present':True,'origin_variables':sorted(data['origin_variables']),'origin_layout':data['origin_layout']}


if __name__=='__main__':
    if sys.argv[1:]!=['auth']:
        raise SystemExit('usage: preview_workflows.py auth')
    try:
        root=Path.cwd().resolve()
        print(json.dumps(auth_contract(root,load(root))))
    except (OSError,ValueError,KeyError,TypeError) as error:
        raise SystemExit(str(error)) from None
