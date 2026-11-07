#!/usr/bin/env python3
"""Dispatch canonical Mise recipe tasks to the existing native adapters."""
import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys

from configuration import load
import mise_entrypoint
import recipe
import sdk_environment
import dependencies
import sdk_source
import effective_tasks

COOKBOOK = Path(__file__).resolve().parents[1]


def invoke(root, config, operation):
    module = recipe.MODULES[config['recipe']]
    subprocess.run(sdk_environment.python_entry(COOKBOOK / 'cookbook' / (module + '.py'), operation),
                   cwd=root, env=os.environ, check=True)


def execute(root, operation):
    root = Path(root).resolve()
    config = load(root)
    identity = mise_entrypoint.read(root)
    revision = sdk_source.git(COOKBOOK, 'rev-parse', 'HEAD')
    sdk_source.verify(COOKBOOK, revision, repository='nicodes/cookbook')
    tools = dependencies.tools_root()
    effective_tasks.check(root)
    os.environ.update(COOKBOOK_ROOT=str(COOKBOOK), COOKBOOK_REF=revision,
                      TOOLS_ROOT=str(tools), TOOLS_REF=dependencies.tools_identity()['revision'],
                      COOKBOOK_CALLER_ROOT=str(root))
    suites = recipe.test_suites(root, config)
    capabilities = (['dev'] if config.get('dev') else []) + [
        'db:' + value for value in mise_entrypoint.DATABASE if value in config.get('database', {})]
    if (identity['revision'] != revision or identity['recipe'] != config['recipe']
            or identity['configuration_sha256'] != hashlib.sha256((root / 'cookbook.toml').read_bytes()).hexdigest()
            or identity['test_groups'] != sorted(suites) or identity['capabilities'] != capabilities):
        raise ValueError('native task differs from its exact pinned recipe declaration')
    os.environ.update(MISE_AUTO_INSTALL='false', MISE_EXEC_AUTO_INSTALL='false', MISE_TASK_RUN_AUTO_INSTALL='false')
    if operation == '_tests':
        for suite in suites:
            invoke(root, config, 'test:' + suite)
        return
    if operation not in mise_entrypoint.names(suites, capabilities) or operation == 'help':
        raise ValueError('recipe task is not an implemented native capability')
    invoke(root, config, operation)
    if operation == 'build':
        invoke(root, config, 'validate')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation')
    args = parser.parse_args()
    try:
        execute(Path.cwd(), args.operation)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
