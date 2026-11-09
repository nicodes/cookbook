"""Observe snapshot protocol failure and cleanup through real subprocess pipes."""
import sys
sys.dont_write_bytecode = True
from source_fixture import SDK_ROOT
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import application_synthetic as synthetic


class SnapshotLifetime(unittest.TestCase):
    def command(self, program, log):
        def selected(arguments, environment):
            self.assertEqual(arguments[:4], ['exec', '-i', 'owned-database', 'psql'])
            self.assertNotIn('synthetic-password', str(arguments))
            return [sys.executable, '-I', '-B', '-u', '-c', program, str(log)], {'PATH': '/usr/bin:/bin'}
        return selected

    def program(self, *, snapshot='00000004-0000001A-1', live='1'):
        return ("import sys\nfrom pathlib import Path\n"
            "for line in sys.stdin:\n"
            " with open(sys.argv[1],'a') as stream: stream.write(line)\n"
            " if 'pg_export_snapshot' in line: print(" + repr(snapshot) + ",flush=True)\n"
            " elif line.startswith('SELECT 1'): print(" + repr(live) + ",flush=True)\n"
            " elif line.startswith('COMMIT'): print('1',flush=True)\n")

    def test_observed_snapshot_remains_live_through_media_copy_then_commits(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / 'sql'
            with patch.object(synthetic.native_runtime, 'command', side_effect=self.command(self.program(), log)):
                with synthetic.exported_snapshot('owned-database', 'fixture', {'PASSWORD': 'synthetic-password'}) as snapshot:
                    self.assertEqual(snapshot, '00000004-0000001A-1')
                    self.assertIn('pg_export_snapshot()', log.read_text())
                    self.assertNotIn('COMMIT', log.read_text())
                    (Path(temporary) / 'media').write_bytes(b'owned original bytes')
            self.assertEqual(log.read_text().splitlines()[-2:], ['SELECT 1;', 'COMMIT; SELECT 1;'])

    def test_invalid_snapshot_or_lost_fence_cannot_report_success(self):
        for program in [self.program(snapshot='fabricated'), self.program(live='0')]:
            with tempfile.TemporaryDirectory() as temporary:
                log = Path(temporary) / 'sql'
                with patch.object(synthetic.native_runtime, 'command', side_effect=self.command(program, log)), self.assertRaises(ValueError):
                    with synthetic.exported_snapshot('owned-database', 'fixture', {}): pass
                self.assertNotIn('COMMIT', log.read_text())
                self.assertIn('ROLLBACK', log.read_text())

    def test_capture_failure_rolls_back_the_real_session_and_preserves_original_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / 'sql'
            with patch.object(synthetic.native_runtime, 'command', side_effect=self.command(self.program(), log)), self.assertRaisesRegex(ValueError, 'media copy failed'):
                with synthetic.exported_snapshot('owned-database', 'fixture', {}):
                    raise ValueError('media copy failed')
            self.assertIn('ROLLBACK', log.read_text()); self.assertNotIn('COMMIT', log.read_text())


if __name__ == '__main__': unittest.main()
