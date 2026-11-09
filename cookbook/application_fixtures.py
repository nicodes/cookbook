"""Intrinsic application integration fixtures; no delivery or live recovery operations."""
import sdk_environment
from pathlib import Path

COOKBOOK = Path(__file__).resolve().parents[1]


def recovery(root, config, environment, run):
    value = config.get('components', {}).get('recovery_fixture')
    if value is None:
        return False
    if not isinstance(value, dict) or set(value) - {'timeout'}:
        raise ValueError('disposable recovery fixture requires bounded native SDK deadlines')
    for key, default in [('timeout', 900)]:
        if type(value.get(key, default)) is not int or not 1 <= value.get(key, default) <= 3600:
            raise ValueError('disposable recovery fixture deadline is invalid')
    # The accepted packet carries the actual migration job, all API/database
    # images, static bytes and provider revisions. No tag reconstruction or
    # disposable gate image can stand in for those identities.
    run(sdk_environment.python_entry(str(COOKBOOK / 'cookbook/application_synthetic.py')),
        root, environment, value.get('timeout', 900))
    return True
