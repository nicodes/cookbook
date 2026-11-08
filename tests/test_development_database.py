from pathlib import Path
import hashlib
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cookbook'))
import development_database as database


class DevelopmentDatabaseTests(unittest.TestCase):
    def fixture(self, root):
        (root / 'cookbook.toml').write_text('''[components.development_database]
image="docker.io/library/postgres@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
database="demo"
owner_role="demo_owner"
password="synthetic-local-only"
port_environment="DB_PORT"
default_port=5433
data_directory="/var/lib/postgresql"
''')
        return database.Database(root, 'demo-dev-123', {})

    def test_foreign_container_cannot_be_adopted_stopped_or_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            resource = self.fixture(Path(temporary))
            foreign = {'Config': {'Labels': {database.LABEL: 'other-checkout', database.PURPOSE: 'postgres'}}}
            with patch.object(resource, 'exists', return_value=True), patch.object(resource, 'inspected', return_value=foreign), patch.object(resource, 'run') as run:
                for operation in [resource.up, resource.stop, resource.reset]:
                    with self.assertRaisesRegex(ValueError, 'another checkout'):
                        operation()
                run.assert_not_called()

    def test_running_database_cannot_have_its_data_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            resource = self.fixture(Path(temporary))
            with patch.object(resource, 'running', return_value=True), patch.object(resource, 'stop') as stop, patch.object(resource, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'stop'):
                    resource.reset()
                stop.assert_not_called()
                run.assert_not_called()

    def test_changed_port_cannot_silently_reuse_an_existing_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            resource = self.fixture(Path(temporary))
            existing = {'ImageName': resource.value['image'], 'HostConfig': {'PortBindings': {
                '5432/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '5433'}]}}, 'State': {'Running': True}}
            with patch.object(resource, 'owned', return_value=existing), patch.object(resource, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'loopback'):
                    resource.up()
                run.assert_not_called()

    def test_new_database_has_explicit_ownership_and_loopback_only_transport(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resource = self.fixture(root)
            with patch.object(resource, 'owned', return_value=None), patch.object(resource, 'run') as run, patch.object(resource, 'execute') as execute, patch.object(database.native_runtime, 'run') as create:
                execute.return_value.returncode = 0
                resource.up()
            command = create.call_args.args[0]
            self.assertIn(database.LABEL + '=' + hashlib.sha256(str(root.resolve()).encode()).hexdigest(), command)
            self.assertEqual(command[command.index('--publish') + 1], '127.0.0.1:5433:5432')
            self.assertNotIn('synthetic-local-only', command)
            self.assertEqual(create.call_args.kwargs['environment']['POSTGRES_PASSWORD'], 'synthetic-local-only')
            self.assertNotIn('--network=host', command)
            self.assertEqual(command[-1], resource.value['image'])


if __name__ == '__main__':
    unittest.main()
