from source_fixture import SDK_ROOT
from pathlib import Path
import json
import signal
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import urlsplit

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import postgres_fixture


class DisposablePostgresControls(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        sql = self.root / 'tests/fixtures/roles.sql'
        sql.parent.mkdir(parents=True)
        sql.write_text("CREATE ROLE fixture_runtime LOGIN PASSWORD :'runtime_password';\nREVOKE CREATE ON SCHEMA public FROM PUBLIC;\n")
        self.config = {'components': {'postgres_fixture': dict(
            image='docker.io/library/postgres@sha256:' + 'a' * 64, database='fixture_database', owner_role='fixture_owner',
            runtime_role='fixture_runtime', sql_paths=['tests/fixtures/roles.sql'],
            url_environment={'MIGRATOR_DATABASE_URL': 'owner', 'RUNTIME_DATABASE_URL': 'runtime'},
            role_environment=['RUNTIME_DATABASE_ROLE'], password_environment={'RUNTIME_DATABASE_PASSWORD': 'runtime'},
            suites=['integration', 'e2e'])}}
        self.container = 'b' * 64
        self.foreign = 'c' * 64
        self.calls = []
        self.removed = []
        self.fail_run = False
        self.not_ready = False
        self.port = '127.0.0.1:43210\n'

    def execute(self, command, **options):
        self.assertEqual(command[:2], ['podman', '--remote=false'])
        command = [command[0], *command[2:]]
        self.calls.append((command, options))
        output, code = '', 0
        if command[1] == 'run':
            self.nonce = command[command.index('--label') + 1].split('=', 1)[1]
            self.cid_file = Path(command[command.index('--cidfile') + 1])
            self.cid_file.write_text(self.container)
            output = self.container + '\n'
            code = int(self.fail_run)
        elif command[1] == 'inspect':
            output = json.dumps({postgres_fixture.LABEL: self.nonce if command[-1] == self.container else 'another-checkout'})
        elif command[1] == 'port':
            output = self.port
        elif command[1] == 'ps':
            output = self.container + '\n' + self.foreign + '\n'
        elif command[1] == 'rm':
            self.removed.append(command[-1])
        elif 'pg_isready' in command:
            code = int(self.not_ready)
        return subprocess.CompletedProcess(command, code, output, '')

    def test_role_sql_dynamic_loopback_and_credentials_are_owned_not_artifacts(self):
        environment = {'GITHUB_RUN_ID': 'test-context', 'GH_TOKEN': 'read-only-context'}
        with postgres_fixture.context(self.root, self.config, environment, 'integration', execute=self.execute) as selected:
            owner, runtime = map(urlsplit, [selected['MIGRATOR_DATABASE_URL'], selected['RUNTIME_DATABASE_URL']])
            self.assertEqual((owner.hostname, owner.port, owner.username), ('127.0.0.1', 43210, 'fixture_owner'))
            self.assertEqual(runtime.username, 'fixture_runtime')
            self.assertNotEqual(owner.password, runtime.password)
            self.assertEqual(runtime.password, selected['RUNTIME_DATABASE_PASSWORD'])
            self.assertEqual(selected['GH_TOKEN'], environment['GH_TOKEN'])
            sql = next(options['input'] for command, options in self.calls if 'psql' in command)
            self.assertTrue(sql.startswith('\\set runtime_password ' + runtime.password + '\n'))
            self.assertIn('REVOKE CREATE ON SCHEMA public FROM PUBLIC;', sql)
            for command, _ in self.calls:
                self.assertNotIn(owner.password, ' '.join(command))
                self.assertNotIn(runtime.password, ' '.join(command))
            self.assertFalse((self.root / '.artifacts').exists())
        self.assertEqual(self.removed, [self.container])
        self.assertFalse(self.cid_file.exists())
        self.assertNotIn('MIGRATOR_DATABASE_URL', environment)

    def test_native_failure_and_partial_startup_always_remove_only_owned_container(self):
        with self.assertRaisesRegex(RuntimeError, 'native test failed'):
            with postgres_fixture.context(self.root, self.config, {}, 'e2e', execute=self.execute):
                raise RuntimeError('native test failed')
        self.assertEqual(self.removed, [self.container])
        self.calls.clear()
        self.removed.clear()
        self.fail_run = True
        with self.assertRaisesRegex(ValueError, 'fixture command failed'):
            with postgres_fixture.context(self.root, self.config, {}, 'integration', execute=self.execute):
                self.fail('failed native startup entered the test body')
        self.assertEqual(self.removed, [self.container])

    def test_readiness_deadline_and_nonloopback_binding_fail_closed(self):
        self.not_ready = True
        now = iter([0, 100])
        with self.assertRaisesRegex(ValueError, 'deadline'):
            with postgres_fixture.context(self.root, self.config, {}, 'integration', execute=self.execute, now=lambda: next(now), pause=lambda _: None):
                self.fail('unready PostgreSQL entered the test body')
        self.assertEqual(self.removed, [self.container])
        self.removed.clear()
        self.not_ready = False
        self.port = '0.0.0.0:5432\n'
        with self.assertRaisesRegex(ValueError, 'loopback'):
            with postgres_fixture.context(self.root, self.config, {}, 'integration', execute=self.execute):
                self.fail('public PostgreSQL binding entered the test body')
        self.assertEqual(self.removed, [self.container])

    def test_floating_image_or_shared_owner_role_rejected_before_side_effects(self):
        for change in [{'image': 'postgres:latest'}, {'runtime_role': 'fixture_owner'}, {'sql_paths': ['../outside.sql']}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                config = {'components': {'postgres_fixture': self.config['components']['postgres_fixture'] | change}}
                with postgres_fixture.context(self.root, config, {}, 'integration', execute=self.execute):
                    self.fail('invalid fixture policy entered the test body')
        self.assertEqual(self.calls, [])

    def test_unselected_suite_has_no_fixture_side_effects(self):
        with postgres_fixture.context(self.root, self.config, {'ordinary': 'value'}, 'unit', execute=self.execute) as selected:
            self.assertEqual(selected, {'ordinary': 'value'})
        self.assertEqual(self.calls, [])

    def test_graceful_termination_cleans_the_fixture_and_restores_caller_signal_policy(self):
        previous = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(SystemExit) as result:
            with postgres_fixture.context(self.root, self.config, {}, 'integration', execute=self.execute):
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        self.assertEqual(result.exception.code, 143)
        self.assertEqual(self.removed, [self.container])
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)


if __name__ == '__main__':
    unittest.main()
