"""Intrinsic application integration fixtures; no delivery or live recovery operations."""
import sdk_environment
from pathlib import Path
import re
import sys

from configuration import relative
import images

import dependencies

COOKBOOK = Path(__file__).resolve().parents[1]
TOOLS = dependencies.tools_root()


def recovery(root, config, environment, run):
    value = config.get('components', {}).get('recovery_fixture')
    if value is None:
        return False
    if not isinstance(value, dict) or set(value) - {'pull_timeout', 'timeout'}:
        raise ValueError('disposable recovery fixture requires bounded native SDK deadlines')
    for key, default in [('pull_timeout', 300), ('timeout', 900)]:
        if type(value.get(key, default)) is not int or not 1 <= value.get(key, default) <= 3600:
            raise ValueError('disposable recovery fixture deadline is invalid')
    compose = config.get('components', {}).get('compose', {}).get('path', 'deploy/compose.yml')
    engines = set(re.findall(r'postgres@sha256:[a-f0-9]{64}', relative(root, compose, exists=True).read_text()))
    if len(engines) != 1:
        raise ValueError('disposable recovery must use the single exact production-declared PostgreSQL image')
    engine = next(iter(engines))
    project = images.definition(root, config)['project']
    run(['docker', 'pull', engine], root, environment, value.get('pull_timeout', 300))
    run(sdk_environment.python_entry(str(TOOLS / 'helpers/test-restore-drill.py'), '--project', project, '--engine', engine), root, environment, value.get('timeout', 900))
    return True
