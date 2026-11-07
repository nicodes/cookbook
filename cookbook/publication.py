"""Admit native addon publication policies without executing product programs."""
import argparse
import configparser
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile

from configuration import ci_settings, load, relative, read_source_data
from godot_addon import package_data

import dependencies

COOKBOOK = Path(__file__).resolve().parents[1]
TOOLS = dependencies.tools_root()
SHA = re.compile(r'[a-f0-9]{40}\Z')
DIGEST = re.compile(r'[a-f0-9]{64}\Z')


def helper(name):
    return dependencies.recipe_helper(name)


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True, timeout=30).strip()


def policy(root, config):
    values = config.get('publication', {})
    allowed = {'mode', 'tag_prefix', 'initial', 'registry_auth', 'retry', 'resume',
               'provenance', 'validation_timeout', 'publish_timeout', 'recovery_timeout', 'environment', 'digests', 'assets', 'evidence_assets'}
    if not isinstance(values, dict) or not values or set(values) - allowed:
        raise ValueError('unknown addon publication setting')
    values = {'mode': 'bump', 'tag_prefix': 'v', 'initial': 'first', 'registry_auth': 'api-key',
              'retry': False, 'resume': False, 'validation_timeout': 20, 'publish_timeout': 10,
              'recovery_timeout': 10, 'environment': '', **values}
    if values['mode'] not in {'bump', 'plugin', 'tag'} or values['registry_auth'] not in {'api-key', 'oidc'}:
        raise ValueError('unsupported release identity or registry authentication policy')
    if values['initial'] not in {'first', 'bump'} or not re.fullmatch(r'(?:[a-z][a-z0-9-]*-)?v', values['tag_prefix']):
        raise ValueError('unsupported native release version policy')
    for name in ['retry', 'resume']:
        if type(values[name]) is not bool:
            raise ValueError('publication recovery flags must be booleans')
    for name in ['validation_timeout', 'publish_timeout', 'recovery_timeout']:
        if type(values[name]) is not int or not 1 <= values[name] <= 180:
            raise ValueError('publication requires reviewed bounded job budgets')
    if values['environment'] and not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', values['environment']):
        raise ValueError('publication environment must be a literal configured environment')
    if 'provenance' in values:
        relative(root, values['provenance'], exists=True)
    if 'digests' in values and not re.fullmatch(r'[A-Za-z0-9_.-]+\.txt', values['digests']):
        raise ValueError('legacy digest metadata requires one safe text basename')
    package = package_data(config)
    roles = values.get('assets', ['archive', 'checksum', *(['registry_archive'] if package.get('registry_archive') else [])])
    if (not isinstance(roles, list) or not roles or any(not isinstance(name, str) for name in roles) or len(set(roles)) != len(roles)
            or not set(roles) <= {'archive', 'checksum', 'registry_archive', 'registry_checksum', 'manifest'}
            or 'archive' not in roles):
        raise ValueError('publication assets must select a closed set of native artifact roles')
    values['assets'] = roles
    if package.get('registry_archive') and 'registry_archive' not in roles:
        raise ValueError('registry publication must include its declared flat archive asset')
    evidence = values.get('evidence_assets', [])
    if (not isinstance(evidence, list) or len(evidence) > 32 or any(not isinstance(name, str) for name in evidence)
            or len(set(evidence)) != len(evidence)):
        raise ValueError('publication evidence must be an explicit bounded asset allowlist')
    for name in evidence:
        path = relative(root, name)
        if not re.fullmatch(r'[A-Za-z0-9@][A-Za-z0-9@._-]*', path.name):
            raise ValueError('publication evidence requires safe canonical basenames')
    values['evidence_assets'] = evidence
    return values


def plugin_version(root, config, text=None):
    parser = configparser.ConfigParser(interpolation=None)
    if text is None:
        policy(root, config)
        text = relative(root, 'addon/plugin.cfg', exists=True).read_text()
    parser.read_string(text)
    match = re.fullmatch(r'"(\d+\.\d+\.\d+)"', parser.get('plugin', 'version', fallback=''))
    if not match:
        raise ValueError('addon plugin requires one native semantic version')
    return match[1]


def request_source(root, tag='', recovery=False, environment=None):
    environment = os.environ if environment is None else environment
    source = environment.get('GITHUB_SHA', '')
    if (environment.get('GITHUB_EVENT_NAME') != 'workflow_dispatch' or not SHA.fullmatch(source)
            or git(root, 'rev-parse', 'HEAD') != source):
        raise ValueError('publication requires an explicit dispatch and exact run checkout')
    allowed = {'refs/heads/main'} | ({'refs/tags/' + tag} if recovery and tag else set())
    if environment.get('GITHUB_REF') not in allowed:
        raise ValueError('publication requires main or the exact requested recovery tag')
    if subprocess.run(['git', '-C', str(root), 'diff', '--quiet', 'HEAD', '--'], timeout=30).returncode:
        raise ValueError('publication requires unchanged tracked source')
    return source


def identity(root, config, bump='patch', environment=None, *, tag='', retry_tag='', retry_sha256='', recovery=False):
    environment = os.environ if environment is None else environment
    values = policy(root, config)
    source = request_source(root, tag, recovery, environment)
    plugin = plugin_version(root, config)
    release = helper('github_release')
    tags = {name: git(root, 'rev-list', '-n', '1', name) for name in git(root, 'tag', '--list').splitlines()}
    operation, target, expected = 'release', source, ''
    if retry_tag or retry_sha256:
        if recovery or not values['retry'] or not retry_tag or not DIGEST.fullmatch(retry_sha256):
            raise ValueError('immutable retry requires both the permitted exact tag and SHA256')
        release.tag(retry_tag)
        if retry_tag not in tags or not SHA.fullmatch(tags[retry_tag]):
            raise ValueError('immutable retry tag is missing')
        target = tags[retry_tag]
        if subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', target, source], timeout=30).returncode:
            raise ValueError('immutable retry target is not an ancestor of permitted main')
        version = retry_tag[len(values['tag_prefix']):] if retry_tag.startswith(values['tag_prefix']) else ''
        old_plugin = plugin_version(root, config, git(root, 'show', target + ':addon/plugin.cfg'))
        if old_plugin != version or plugin != version:
            raise ValueError('retry tag and current/immutable plugin versions must agree')
        selected, expected, operation = retry_tag, retry_sha256, 'retry'
    elif recovery:
        if values['registry_auth'] != 'oidc' or values['mode'] != 'tag':
            raise ValueError('exact-tag recovery requires the declared trusted publication policy')
        release.tag(tag)
        if tag != values['tag_prefix'] + plugin or tags.get(tag) != source:
            raise ValueError('recovery tag must resolve to this exact run commit and package version')
        version, selected, operation = plugin, tag, 'registry-recovery'
    else:
        if values['mode'] == 'tag':
            release.tag(tag)
            version, selected = plugin, tag
            if tag != values['tag_prefix'] + plugin:
                raise ValueError('requested tag does not match the native package version')
        elif values['mode'] == 'plugin':
            version, selected = plugin, values['tag_prefix'] + plugin
        elif values['resume'] and values['tag_prefix'] == 'v':
            version, selected = release.resolve_version(plugin, bump, tags, source)
        else:
            version, selected = release.version(bump, values['tag_prefix'], tags, values['initial'])
        if plugin != version:
            raise ValueError('plugin version must match the next immutable release: ' + version)
        if selected in tags:
            if not values['resume'] or tags[selected] != source:
                raise ValueError('release tag already exists or differs from this exact source')
            operation = 'resume'
    package = package_data(config)
    asset = relative(root, package.get('registry_archive', package['archive'])).name
    if not re.fullmatch(r'[A-Za-z0-9@][A-Za-z0-9@._-]*\.zip', asset):
        raise ValueError('registry publication requires one exact safe ZIP basename')
    return {'version': version, 'tag': selected, 'asset': asset, 'operation': operation,
            'target': target, 'source': source, 'sha256': expected,
            'registry_auth': values['registry_auth'], 'environment': values['environment'],
            'validation_runner': ci_settings(config)['runner'],
            'validation_timeout': str(values['validation_timeout']),
            'publish_timeout': str(values['recovery_timeout'] if recovery else values['publish_timeout'])}


def require_remote_source(root, plan, environment=None):
    environment = os.environ if environment is None else environment
    request_source(root, plan['tag'], plan['operation'] == 'registry-recovery', environment)
    repository = environment.get('GITHUB_REPOSITORY', '')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('publication must identify this workflow repository')
    release = helper('github_release')
    if environment.get('GITHUB_REF') == 'refs/heads/main':
        main = json.loads(release.run('gh', 'api', f'repos/{repository}/git/ref/heads/main'))
        if main.get('object', {}).get('sha') != plan['source']:
            raise ValueError('permitted main changed after this workflow was dispatched')
    if plan['operation'] in {'retry', 'resume', 'registry-recovery'}:
        if release.remote_tag_commit(repository, plan['tag']) != plan['target']:
            raise ValueError('remote immutable tag differs from the admitted source')


def build_paths(root, config):
    from godot_addon import verification_scope
    return verification_scope(root, config)['artifacts']


def validate_proof(root, config, accepted, source, environment=None):
    environment = os.environ if environment is None else environment
    tree = git(root, 'rev-parse', 'HEAD^{tree}')
    tools = git(COOKBOOK, 'rev-parse', 'HEAD')
    dependencies.validate_binding(accepted, tools)
    if (git(root, 'rev-parse', 'HEAD') != source or accepted.get('source_commit') != source
            or accepted.get('source_tree') != tree or accepted.get('repository') != environment.get('GITHUB_REPOSITORY')
            or accepted.get('cookbook_commit') != tools or accepted.get('status') != 'passed'
            or any(accepted.get(name) is not True for name in ['tracked_source_clean', 'build_verified', 'tests_verified'])):
        raise ValueError('publication requires this exact source and cookbook full validation proof')
    for key in ['GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_WORKFLOW_REF']:
        if not environment.get(key) or accepted.get('execution_context', {}).get(key) != environment[key]:
            raise ValueError('publication proof belongs to another workflow execution')
    suites = {'lint', 'unit', 'security', *config.get('tests', {})}
    if set(accepted.get('test_groups', [])) != suites:
        raise ValueError('publication requires every implemented test group')
    expected = {}
    for name in build_paths(root, config):
        path = relative(root, name, exists=True)
        files = sorted(path.rglob('*')) if path.is_dir() else [path]
        if any(item.is_symlink() for item in files):
            raise ValueError('accepted build scope may not contain symlinks')
        files = [item for item in files if item.is_file()]
        if not files:
            raise ValueError('accepted build scope is empty')
        for item in files:
            relative(root, item.relative_to(root).as_posix(), exists=True)
            expected[item.relative_to(root).as_posix()] = hashlib.sha256(item.read_bytes()).hexdigest()
    if accepted.get('artifacts') != expected:
        raise ValueError('downloaded outputs differ from the complete accepted build scope')
    accepted_evidence(root, config, accepted)
    if subprocess.run(['git', '-C', str(root), 'diff', '--quiet', 'HEAD', '--'], timeout=30).returncode:
        raise ValueError('publication source changed after checkout')


def verify_package(root, config):
    from manifest_addon import verify
    return verify(root, config)


def accepted_evidence(root, config, accepted):
    result = {}
    for name in policy(root, config)['evidence_assets']:
        path = relative(root, name, exists=True)
        if not path.is_file() or not 0 < path.stat().st_size <= 64 * 1024 * 1024:
            raise ValueError('publication evidence must be a bounded regular file')
        expected = accepted.get('evidence', {}).get(name) if accepted else None
        if not isinstance(expected, str) or not DIGEST.fullmatch(expected) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('publication evidence differs from this run\'s accepted evidence hash')
        result[name] = expected
    return result


def assets(root, config, destination, accepted=None):
    values = policy(root, config)
    verify_package(root, config)
    package_data_values = package_data(config)
    archive = relative(root, package_data_values['archive'], exists=True)
    paths = {'archive': archive, 'checksum': archive.with_suffix(archive.suffix + '.sha256')}
    if package_data_values.get('registry_archive'):
        registry = relative(root, package_data_values['registry_archive'], exists=True)
        paths.update(registry_archive=registry, registry_checksum=registry.with_suffix(registry.suffix + '.sha256'),
                     manifest=archive.parent / 'PACKAGE_MANIFEST.json')
    if not set(values['assets']) <= paths.keys():
        raise ValueError('publication selects an undeclared native artifact')
    evidence = accepted_evidence(root, config, accepted)
    names = [paths[role].name for role in values['assets']] + [Path(name).name for name in evidence]
    if values.get('digests'):
        names.append(values['digests'])
    if len(names) != len(set(names)):
        raise ValueError('release artifacts and evidence must have distinct canonical basenames')
    destination.mkdir(parents=True, exist_ok=False)
    expected = {}
    for role in values['assets']:
        path = paths[role]
        shutil.copyfile(path, destination / path.name)
        expected[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name, digest in evidence.items():
        path = relative(root, name, exists=True)
        shutil.copyfile(path, destination / path.name)
        expected[path.name] = digest
    if values.get('digests'):
        digest = hashlib.sha256()
        names = helper('addon-package').manifest_files(relative(root, package_data_values['manifest'], exists=True))
        with zipfile.ZipFile(archive) as package:
            for name in names:
                digest.update(name.encode() + b'\0')
                digest.update(hashlib.sha256(package.read(name)).digest())
        path = destination / values['digests']
        path.write_text('GD_PACKAGE_SHA256=' + hashlib.sha256(archive.read_bytes()).hexdigest()
                        + '\nGD_INSTALLED_TREE_SHA256=' + digest.hexdigest() + '\n')
        expected[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return expected


def registry_recovery(root, config, plan, destination, environment=None):
    environment = os.environ if environment is None else environment
    require_remote_source(root, plan, environment)
    release = helper('github_release')
    repository = environment['GITHUB_REPOSITORY']
    if plan['operation'] == 'retry':
        digest = plan['sha256']
    elif plan['operation'] == 'registry-recovery':
        value = json.loads(release.run('gh', 'api', f"repos/{repository}/releases/tags/{plan['tag']}"))
        matching = [item for item in value.get('assets', []) if item.get('name') == plan['asset']]
        if len(matching) != 1 or not re.fullmatch(r'sha256:[a-f0-9]{64}', matching[0].get('digest', '')):
            raise ValueError('existing registry asset lacks an exact immutable digest')
        digest = matching[0]['digest'][7:]
    else:
        raise ValueError('registry recovery may never create or rebuild a release')
    expected = {plan['asset']: digest}
    release.download_existing(destination, repository, plan['tag'], plan['target'], expected)
    if plan['operation'] == 'registry-recovery':
        from manifest_addon import inputs
        files, _, _, _, _ = inputs(root, config)
        with zipfile.ZipFile(destination / plan['asset']) as archive:
            if archive.namelist() != sorted(files):
                raise ValueError('registry recovery asset is not the exact closed flat package')
            if any(stat.S_IFMT(item.external_attr >> 16) not in {0, stat.S_IFREG} for item in archive.infolist()):
                raise ValueError('registry recovery archive contains a non-regular entry')
            for name, original in files.items():
                if archive.read(name) != original.read_bytes():
                    raise ValueError('registry recovery payload differs from its exact tag source')
    require_remote_source(root, plan, environment)
    release.verify_existing(repository, plan['tag'], plan['target'], expected)
    return expected


def provenance_notes(root, config, plan):
    """Render source-owned public metadata without executing source-owned programs."""
    path = policy(root, config).get('provenance')
    if not path:
        return None
    values = read_source_data(relative(root, path, exists=True))
    vendor = json.loads(relative(root, values['vendor'], exists=True).read_text())
    allowlist = read_source_data(relative(root, values['allowlist'], exists=True))
    archive = relative(root, config['package']['archive'], exists=True)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    fields = [values['name'], values['description'], vendor['version'], vendor['integrity']]
    if any(not isinstance(value, str) or not value or '\0' in value for value in fields):
        raise ValueError('package provenance requires public literal metadata')
    return '\n'.join([f"# {values['name']} {plan['tag']}", '', values['description'], '',
        '- Plugin version: ' + plan['version'], '- Package: ' + archive.name, '- SHA256: ' + digest,
        '- ZIP entries: ' + str(allowlist['zip_entries']), '- ' + values['vendor_package'] + ': ' + vendor['version'],
        '- Integrity: ' + vendor['integrity'], '- Commit: ' + plan['source'], '- Tag: ' + plan['tag'], ''])


def publish(root, config, plan, environment=None):
    environment = os.environ if environment is None else environment
    require_remote_source(root, plan, environment)
    destination = root / '.artifacts/release-assets'
    if plan['operation'] in {'retry', 'registry-recovery'}:
        return registry_recovery(root, config, plan, destination, environment)
    accepted = json.loads((root / '.artifacts/cookbook-validation.json').read_text())
    validate_proof(root, config, accepted, plan['source'], environment)
    expected = assets(root, config, destination, accepted)
    release = helper('github_release')
    repository = environment['GITHUB_REPOSITORY']
    admit = lambda: require_remote_source(root, plan, environment)
    if plan['operation'] == 'resume':
        if release.optional_release(repository, plan['tag']) is None:
            release.publish(destination, expected, plan['tag'], plan['source'], repository,
                            provenance_notes(root, config, plan), tag_exists=True, admit=admit)
        else:
            archive = Path(config['package']['archive']).name
            existing = {archive: expected[archive], **{Path(name).name: digest for name, digest in accepted_evidence(root, config, accepted).items()}}
            with tempfile.TemporaryDirectory(prefix='addon-resume-') as directory:
                release.download_existing(Path(directory) / 'downloaded', repository, plan['tag'], plan['source'], existing)
            admit()
    else:
        release.publish(destination, expected, plan['tag'], plan['source'], repository,
                        provenance_notes(root, config, plan), admit=admit)
    return expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['identity', 'plan', 'publish'])
    parser.add_argument('--bump', default='patch')
    parser.add_argument('--tag', default='')
    parser.add_argument('--retry-tag', default='')
    parser.add_argument('--retry-sha256', default='')
    parser.add_argument('--registry-recovery', action='store_true')
    args = parser.parse_args()
    root = Path.cwd().resolve()
    try:
        config = load(root)
        if config['recipe'] != 'godot-addon':
            raise ValueError('this publisher requires the addon recipe')
        plan = identity(root, config, args.bump, tag=args.tag, retry_tag=args.retry_tag,
                        retry_sha256=args.retry_sha256, recovery=args.registry_recovery)
        require_remote_source(root, plan)
        if os.environ.get('EXPECTED_PLAN') and json.loads(os.environ['EXPECTED_PLAN']) != plan:
            raise ValueError('publication plan changed after the read-only admission stage')
        if args.operation == 'publish':
            publish(root, config, plan)
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                for name, value in {**plan, 'plan': json.dumps(plan, separators=(',', ':'), sort_keys=True)}.items():
                    output.write(name + '=' + value + '\n')
        print(json.dumps(plan, sort_keys=True))
    except (OSError, ValueError, KeyError, configparser.Error, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
