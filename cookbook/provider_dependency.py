"""Bind the exact Cookbook provider and its separate generic Tools source."""
import os
from pathlib import Path

import dependencies
import sdk_identity


def bind():
    cookbook = Path(__file__).resolve().parents[1]
    revision = os.environ['COOKBOOK_REF']
    sdk_identity.verify(cookbook, revision)
    tools = dependencies.tools_root(Path.cwd() / '.artifacts/recipe-sdks/_tools')
    identity = dependencies.tools_identity()
    if os.environ.get('TOOLS_REF') != identity['revision']:
        raise ValueError('workflow Tools identity differs from the checked Cookbook dependency')
    values = {'COOKBOOK_ROOT': str(cookbook), 'COOKBOOK_REF': revision,
              'TOOLS_ROOT': str(tools), 'TOOLS_REF': identity['revision'],
              'PYTHONDONTWRITEBYTECODE': '1', 'MISE_TASK_RUN_AUTO_INSTALL': 'false',
              'MISE_AUTO_INSTALL': 'false', 'MISE_EXEC_AUTO_INSTALL': 'false'}
    with open(os.environ['GITHUB_ENV'], 'a') as output:
        for key, value in values.items():
            if '\n' in value or '\r' in value:
                raise ValueError('SDK source roots must be literal environment values')
            output.write(key + '=' + value + '\n')


if __name__ == '__main__':
    bind()
