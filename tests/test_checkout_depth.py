import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Path (relative to the repository root) -> reason a full-history checkout is permitted there.
FULL_HISTORY_ALLOWED = {}
SOURCES = ['.github/workflows/*.yml', '*/action.yml', 'cookbook/*.py', 'cookbook/*.in']


def sources():
    for pattern in SOURCES:
        yield from sorted(ROOT.glob(pattern))


class CheckoutDepthTest(unittest.TestCase):
    def test_no_full_history_or_filtered_checkout_without_an_allow_list_reason(self):
        for path in sources():
            relative = path.relative_to(ROOT).as_posix()
            text = path.read_text()
            for pattern in [r'fetch-depth:\s*[\'"]?0\b', r'filter:\s*blob:none', r'--unshallow', r'github\.event\.repository\.private']:
                if re.search(pattern, text):
                    self.assertTrue(FULL_HISTORY_ALLOWED.get(relative), relative + ' needs history: ' + pattern)
        for relative, reason in FULL_HISTORY_ALLOWED.items():
            self.assertTrue(reason.strip() and (ROOT / relative).exists())

    def test_only_release_planning_fetches_tags_and_never_history(self):
        for path in (ROOT / '.github/workflows').glob('release-addon-*.yml'):
            self.assertIn('fetch-tags: true', path.read_text())
        for path in sources():
            if 'release-addon' not in path.name and 'addon_release' not in path.name:
                self.assertNotIn('fetch-tags', path.read_text(), path.name)


if __name__ == '__main__':
    unittest.main()
