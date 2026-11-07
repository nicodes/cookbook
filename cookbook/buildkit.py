"""Release only the disposable Actions builder's duplicate compiler store."""
import re


def release_cache(root, environment, run):
    if environment.get('GITHUB_ACTIONS') != 'true':
        return
    builder = environment.get('BUILDX_BUILDER', '')
    if not re.fullmatch(r'builder-[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', builder):
        raise ValueError('cache release requires the job-owned Actions Buildx builder')
    run(['docker', 'buildx', 'prune', '--builder', builder, '--all', '--force'], root, environment)
