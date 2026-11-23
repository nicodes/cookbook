"""Jobs fetch only the commit they build; history is opt-in, explained and blobless unless listed."""
from source_fixture import SDK_ROOT
import unittest

# Addon publication reads a blob at an older tag, and credentials are not persisted for lazy fetches.
FULL_HISTORY = {'release-addon-api-key.yml', 'release-addon-oidc.yml', 'addon_release.yml.in'}


# Private repositories have no history boundary; an unknown visibility evaluates to 0, the full history.
VISIBILITY_DEPTH = 'fetch-depth: ${{ github.event.repository.private && 1 || 0 }}'


def sources():
    files = [*(SDK_ROOT / '.github/workflows').glob('*.yml'), *(SDK_ROOT / 'cookbook').glob('*.py'),
             *(SDK_ROOT / 'cookbook').glob('*.in')]
    return sorted(files)


def indent(line):
    return len(line) - len(line.lstrip())


class CheckoutDepth(unittest.TestCase):
    def test_full_history_checkouts_are_blobless_and_explained(self):
        count = 0
        for path in sources():
            lines = path.read_text().split('\n')
            for number, line in enumerate(lines):
                if line.strip() not in {'fetch-depth: 0', VISIBILITY_DEPTH}:
                    continue
                count += 1
                with self.subTest(file=path.name, line=number + 1):
                    block = [other.strip() for other in lines[max(0, number - 4):number + 5] if indent(other) == indent(line)]
                    self.assertTrue(lines[number - 1].strip().startswith('# History: '), 'name what needs the history')
                    if path.name not in FULL_HISTORY or line.strip() == VISIBILITY_DEPTH:
                        self.assertIn('filter: blob:none', block)
        self.assertGreater(count, 0)

    def test_checkouts_never_fetch_history_by_accident(self):
        for path in sources():
            for number, line in enumerate(path.read_text().split('\n')):
                with self.subTest(file=path.name, line=number + 1):
                    self.assertNotIn('fetch-depth: -', line)
                    if line.strip().startswith('fetch-depth:'):
                        self.assertIn(line.strip(), {'fetch-depth: 0', 'fetch-depth: 1', VISIBILITY_DEPTH})


if __name__ == '__main__':
    unittest.main()
