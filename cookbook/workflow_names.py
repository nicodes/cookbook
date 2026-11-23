"""Keep workflow filenames derived from their literal operation titles."""
from pathlib import Path
import re

import dependencies


GENERATED = '# Generated cookbook caller.'
MAX_SOURCE = 1 << 20


def filename(title):
    if (not isinstance(title, str) or not re.fullmatch(r'[A-Z][A-Za-z0-9 ()-]*', title)
            or title != title.strip() or '  ' in title):
        raise ValueError('workflow name must be a literal operation title using words, not symbols: ' + repr(title))
    names = dependencies.helper('workflow-names')
    return names.filename(names.title(title, 'workflow name'))


def check(files):
    for name, source in files.items():
        titles = re.findall(r'^name: (.+)$', source, flags=re.MULTILINE)
        if len(titles) != 1:
            raise ValueError('workflow requires one literal top-level name: ' + str(name))
        expected = filename(titles[0])
        if Path(name).name != expected:
            raise ValueError('workflow filename must match its name: ' + str(name)
                             + ' declares ' + repr(titles[0]) + '; expected ' + expected)


def sources(root):
    result = {}
    for path in sorted((Path(root) / '.github/workflows').glob('*.y*ml')):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SOURCE:
            raise ValueError('workflow must be a bounded regular source file: ' + str(path))
        result[path.relative_to(root).as_posix()] = path.read_text(encoding='utf-8')
    return result


def check_provider(root):
    check(sources(root))


def obsolete_generated(root, expected):
    """Find generator-owned files by source ownership, without retired-name aliases."""
    return [name for name, source in sources(root).items()
            if source.startswith(GENERATED) and name not in expected]


def obsolete_recipe_files(root, expected):
    """Retire the duplicate policy implementation and superseded operation entry."""
    result = obsolete_generated(root, expected)
    paths = ['.github/workflows/documentation-policy.yml', '.github/scripts/check-documentation.py']
    if {'.github/workflows/maintenance.yml', '.github/workflows/security.yml'} & expected.keys():
        paths.append('.github/workflows/dependency-maintenance.yml')
    for name in paths:
        path = Path(root) / name
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SOURCE:
                raise ValueError('retired recipe source must be a bounded regular file: ' + name)
            if name not in result:
                result.append(name)
    return sorted(result)
