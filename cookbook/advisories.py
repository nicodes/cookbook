"""Select whether advisory and vulnerability scans run beside the deterministic checks."""
import os

VARIABLE = 'COOKBOOK_ADVISORIES'
VALUES = ('run', 'skip')


def skipped(environment=None):
    """True only for an explicit `skip`; unset or empty keeps every scan (`run`)."""
    value = (environment if environment is not None and VARIABLE in environment else os.environ).get(VARIABLE, '') or 'run'
    if value not in VALUES:
        raise ValueError(VARIABLE + ' must be run or skip')
    return value == 'skip'
