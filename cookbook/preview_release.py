"""Compile a Preview workload from public origins planned by trusted control."""
import copy
from pathlib import PurePosixPath
import re
import tomllib
from urllib.parse import urlsplit, urlunsplit

import artifact_packet
from configuration import relative


def origin(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(char) < 33 for char in value):
        raise ValueError('Preview requires an explicit planned public origin')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query
            or parsed.fragment or parsed.port not in {None, 443} or '..' in parsed.path.split('/')
            or not re.fullmatch('[a-z0-9.-]+', parsed.hostname)):
        raise ValueError('Preview requires a planned HTTPS origin without credentials or query data')
    return parsed.hostname, parsed.path.rstrip('/'), urlunsplit(('https', parsed.netloc, '', '', ''))


def project(root, config, environment, release):
    number = environment.get('COOKBOOK_PREVIEW_NUMBER')
    if not number:
        return release
    if not re.fullmatch('[1-9][0-9]{0,9}', number):
        raise ValueError('Preview requires its exact requested PR instance')
    selected = config.get('preview', {})
    if set(selected) != {'definition'}:
        raise ValueError('Preview workload has no explicit native source policy')
    policy = tomllib.loads(artifact_packet.read_regular(relative(root, selected['definition'], exists=True), 32768).decode())
    if policy.get('project') != release['application']:
        raise ValueError('Preview workload belongs to another application')
    app_host, app_path, app_origin = origin(environment.get('COOKBOOK_PREVIEW_APP_ORIGIN'))
    workload = release['workload']
    assets = [route for route in workload['routes'] if route.get('target', {}).get('kind') == 'asset']
    api_routes = [route for route in workload['routes'] if route.get('target', {}).get('kind') == 'service']
    api_names = {route['target']['name'] for route in api_routes}
    if not assets or len(api_names) > 1:
        raise ValueError('Preview projection requires one frontend and at most one public backend')
    api_name = next(iter(api_names), None)
    api_host = api_path = api_origin = None
    if api_name is not None:
        api_host, api_path, api_origin = origin(environment.get('COOKBOOK_PREVIEW_API_ORIGIN'))
        if app_path or api_path or app_host == api_host:
            raise ValueError('authenticated Preview backends require their declared distinct PR subdomains')
    result = copy.deepcopy(release)
    projected = result['workload']
    primary = assets[0]
    primary_host = primary['host']
    route = copy.deepcopy(primary)
    route['host'] = app_host
    route['path'] = app_path + '/*' if app_path else '/'
    if app_path:
        route['strip_prefix'] = True
    projected['routes'] = [route]
    if app_path:
        exact = copy.deepcopy(route)
        exact['path'] = app_path
        projected['routes'].insert(0, exact)
    if api_name is not None:
        dedicated = next((item for item in api_routes if item['host'] != primary_host), api_routes[0])
        backend = copy.deepcopy(dedicated)
        backend.update(host=api_host, path='/')
        backend.pop('strip_prefix', None)
        same_origin = next((item for item in api_routes if item['host'] == primary_host and item['path'] == '/api/*'), None)
        projected['routes'] = [route, backend]
        if same_origin:
            same_origin=copy.deepcopy(same_origin)
            same_origin['host']=app_host
            projected['routes'].insert(0,same_origin)
        service = projected['services'][api_name]
        service.setdefault('environment', {}).update(policy.get('environment', {}))
        origins = {'app': app_origin, 'api': api_origin}
        for name, requested in policy.get('origin_variables', {}).items():
            if (not isinstance(name,str) or not re.fullmatch('[A-Z][A-Z0-9_]{0,79}',name)
                    or name.startswith(('GITHUB_','MISE_','COOKBOOK_','KOMIZO_','TOOLS_','REQUEST_'))
                    or not isinstance(requested, list) or not requested or not set(requested) <= set(origins)):
                raise ValueError('Preview selects an undeclared public origin carrier')
            service['environment'][name] = ','.join(dict.fromkeys(origins[key] for key in requested))
        for name, kind in policy.get('clerk_urls', {}).items():
            key = {'issuer': 'COOKBOOK_PREVIEW_CLERK_ISSUER', 'jwks': 'COOKBOOK_PREVIEW_CLERK_JWKS_URL'}.get(kind)
            if name not in {'CLERK_ISSUER', 'CLERK_JWKS_URL'} or key is None:
                raise ValueError('Preview selects an undeclared public Clerk carrier')
            value = environment.get(key)
            origin(value)
            service['environment'][name] = value
    readiness = []
    source_api_hosts = {route['host'] for route in api_routes if route['host'] != primary_host}
    for check in workload['readiness']:
        parsed = urlsplit(check['url'])
        if parsed.hostname == primary_host:
            target_host, target_path = app_host, app_path
        elif api_name is not None and parsed.hostname in source_api_hosts:
            target_host, target_path = api_host, api_path
        else:
            continue
        selected = copy.deepcopy(check)
        selected['url'] = 'https://' + target_host + target_path + parsed.path
        readiness.append(selected)
    if not readiness:
        raise ValueError('Preview requires the source-owned readiness assertions on its planned origins')
    projected['readiness'] = readiness
    return result
