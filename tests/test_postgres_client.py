"""Client identity, transaction-loss and no-effect refusal controls."""
from source_fixture import SDK_ROOT
import contextlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import postgres_client as pg


class PostgresClient(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='postgres-client-control-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.record = {'application': 'demo', 'container_id': 'a' * 64,
            'deployment': {'release': {'workload': {'services': {'postgres': {'artifact': 'postgres',
                'command': ['postgres', '-c', 'unix_socket_directories=/var/run/postgresql,@/komizo-capture']}}}}}}
        self.resources = SimpleNamespace(validate=Mock(), environment=lambda: {'PATH': '/usr/bin:/bin'})
        self.options = []
        @contextlib.contextmanager
        def container(*arguments, **options):
            self.options.append(options)
            yield 'b' * 64
        self.runtime = SimpleNamespace(private_directory=lambda value: Path(value), container=container)

    def test_client_is_an_immutable_task_helper_with_no_data_mount_or_password_transport(self):
        with patch.object(pg, 'runtime_module', return_value=self.runtime), \
                patch.object(pg, 'server_process', return_value=(123, '42')), \
                patch.dict(os.environ, {'KOMIZO_DEADLINE_UNIX': '1003', 'BACKUP_S3_SECRET_KEY': 'unused'}, clear=True), \
                patch.object(pg.time, 'time', return_value=1000), patch.object(pg.time, 'monotonic', return_value=50):
            with pg.context(self.resources, self.record, self.root, database='demo', role='demo_reader') as client:
                self.assertEqual(client.container_id, 'b' * 64)
                self.assertEqual(client.expires, 53)
                self.assertEqual(client.arguments('pg_dump')[-2:], ['b' * 64, 'pg_dump'])
        options = self.options[0]
        self.assertEqual(options['artifact'], 'postgres')
        self.assertEqual(options['user'], '65534:65534')
        self.assertEqual(options['network'], 'postgres')
        self.assertEqual(options['mounts'], {})
        self.assertEqual(options['timeout'], 3)
        self.assertEqual(options['environment']['PGHOST'], '@/komizo-capture')
        self.assertIn('client_connection_check_interval=100ms', options['environment']['PGOPTIONS'])
        self.assertNotIn('PASSWORD', str(options))
        self.assertNotIn('unused', str(options))
        self.assertEqual(options['environment']['PGPASSFILE'], '/dev/null')

    def test_undeclared_socket_bad_scope_and_expired_deadline_refuse_before_create(self):
        with patch.object(pg, 'runtime_module', return_value=self.runtime), \
                patch.object(pg, 'server_process', return_value=(123, '42')):
            for values in [{'role': "reader';SELECT1"}, {'database': '../demo'}, {'socket_dir': '@/elsewhere'},
                    {'timeout': 0}, {'timeout': True}, {'server_uid': 0}]:
                options = {'database': 'demo', 'role': 'demo_reader', **values}
                with self.subTest(values=values), self.assertRaises((ValueError, TimeoutError)):
                    with pg.context(self.resources, self.record, self.root, **options):
                        self.fail('invalid client scope was yielded')
            with patch.dict(os.environ, {'KOMIZO_DEADLINE_UNIX': '1'}), self.assertRaises(TimeoutError):
                with pg.context(self.resources, self.record, self.root, database='demo', role='demo_reader'):
                    self.fail('expired client scope was yielded')
        self.assertFalse(self.options)

    def test_kernel_uid_container_membership_and_start_time_bind_server_identity(self):
        process = self.root / 'proc/123'; process.mkdir(parents=True)
        (process / 'status').write_text('Uid:\t999\t999\t999\t999\n')
        (process / 'cgroup').write_text('0::/owned/libpod-' + 'a' * 64 + '\n')
        (process / 'stat').write_text('123 (postgres) ' + ' '.join(['S'] + ['0'] * 18 + ['42'] + ['0'] * 5))
        resources = SimpleNamespace(inspect=lambda *args: {'State': {'Pid': 123}})
        runtime = SimpleNamespace(run=lambda *args: b'[]')
        with patch.object(pg, 'PROC_ROOT', self.root / 'proc'), patch.object(pg, 'runtime_module', return_value=runtime):
            self.assertEqual(pg.server_process(resources, self.record, 999), (123, '42'))
            (process / 'status').write_text('Uid:\t0\t0\t0\t0\n')
            with self.assertRaisesRegex(ValueError, 'writer user'):
                pg.server_process(resources, self.record, 999)
            (process / 'status').write_text('Uid:\t999\t999\t999\t999\n')
            (process / 'cgroup').write_text('0::/foreign/libpod-' + 'c' * 64 + '\n')
            with self.assertRaisesRegex(ValueError, 'exact native container'):
                pg.server_process(resources, self.record, 999)

    def session(self, rows):
        session = pg.Session.__new__(pg.Session)
        session.client = SimpleNamespace(application_name='kmz-backup-' + 'd' * 32, role='demo_reader',
            remaining=lambda: 100, verify_server=Mock())
        session.witness = '123:4:5::on:repeatable read'
        session.snapshot_id = '00000003-0000001A-1'
        session.query = Mock(side_effect=rows)
        session.run = Mock()
        return session

    def test_commit_requires_same_snapshot_no_lingering_dump_and_actual_acknowledgment(self):
        session = self.session(['123:4:5::on:repeatable read', '0', '1'])
        session.commit()
        session.run.assert_called_once_with('COMMIT')
        session.client.verify_server.assert_called_once()
        for rows in [['124:4:5::on:repeatable read'], ['123:4:5::off:read committed'],
                ['123:4:5::on:repeatable read', '0', '0']]:
            session = self.session(rows)
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                session.commit()
            session.client.verify_server.assert_not_called()

    def test_lost_or_non_repeatable_export_never_yields_a_snapshot(self):
        session = self.session(['123:4:5::off:read committed'])
        with self.assertRaises(ValueError):
            session.export_snapshot()
        self.assertEqual(session.query.call_count, 1)
        session = self.session(['123:4:5::on:repeatable read', 'invented'])
        with self.assertRaises(ValueError):
            session.export_snapshot()

    def test_dump_is_private_staging_only_and_rechecks_actual_server(self):
        client = pg.Client(self.resources, 'b' * 64, self.root, 'demo', 'demo_reader', 'kmz-backup-' + 'd' * 32,
            pg.time.monotonic() + 100, self.record, 999, (123, '42'))
        with patch.object(client, 'verify_server') as verify, patch.object(client, 'run') as invoke:
            target = self.root / 'database.dump'
            client.dump('00000003-0000001A-1', target)
            self.assertEqual(verify.call_count, 2)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            argv = invoke.call_args.args[0]
            self.assertIn('b' * 64, argv)
            self.assertNotIn('a' * 64, argv)
            self.assertIn('--no-password', argv)
            with self.assertRaises(ValueError):
                client.dump('00000003-0000001A-1', self.root.parent / 'outside.dump')


if __name__ == '__main__':
    unittest.main()
