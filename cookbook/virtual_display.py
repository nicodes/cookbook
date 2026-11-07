"""An explicit setup-owned display carrier for unchanged native assertions."""
import shutil


def require(environment):
    for name in ['xvfb-run', 'Xvfb', 'xauth']:
        if not shutil.which(name, path=environment.get('PATH')):
            raise ValueError('virtual display dependency is missing; run mise run setup first: ' + name)


def setup(root, environment, run):
    # Browser setup owns its OS dependencies. Refuse an incomplete installation
    # rather than installing packages while an assertion is running.
    require(environment)
    run(['xvfb-run', '-a', 'sh', '-c', 'test -n "$DISPLAY"'], root, environment)


def command(argv, environment):
    require(environment)
    return ['xvfb-run', '-a', *argv]
