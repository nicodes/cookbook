"""Exercise real Compose parsing without credentials, images or a Docker daemon."""
from source_fixture import SDK_ROOT
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import images


class ComposeValidationCLI(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='native Compose source ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'deploy').mkdir()
        self.source = self.root / 'deploy/compose.yml'
        self.config = {'components': {'compose': {}}}
        self.environment = {**os.environ, 'PUBLIC_ORIGIN': 'https://process.invalid'}
        self.environment.pop('CHECKOUT_PRIVATE_SENTINEL', None)

    def run_native(self, command, root, environment):
        subprocess.run(command, cwd=root, env=environment, check=True,
                       capture_output=True, text=True, timeout=30)

    def test_real_cli_validates_missing_secret_refs_without_reading_or_materializing_files(self):
        content = '''services:
  api:
    image: example:latest
    read_only: true
    env_file:
      - secrets.env
      - path: secrets/owner.env
        required: false
    environment:
      PUBLIC_ORIGIN: ${PUBLIC_ORIGIN:?process input required}
'''
        self.source.write_text(content)
        (self.root / 'deploy/secrets').mkdir()
        secret = self.root / 'deploy/secrets/owner.env'
        secret.write_text("CHECKOUT_PRIVATE_SENTINEL='unterminated\n")
        (self.root / '.env').write_text('PUBLIC_ORIGIN=https://dotenv.invalid\n')
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob('*') if path.is_file()}
        images.validate_compose(self.root, self.config, self.environment, self.run_native)
        model = images.compose_model(self.root, self.config, self.environment)
        api = model['services']['api']
        self.assertTrue(api['read_only'])
        self.assertEqual(api['environment'], {'PUBLIC_ORIGIN': 'https://process.invalid'})
        self.assertEqual(api['env_file'], [{'path': 'secrets.env', 'required': True},
            {'path': 'secrets/owner.env', 'required': False}])
        self.assertNotIn('CHECKOUT_PRIVATE_SENTINEL', json.dumps(model))
        self.assertEqual(before, {str(path.relative_to(self.root)): path.read_bytes()
                                 for path in self.root.rglob('*') if path.is_file()})

    def test_real_cli_keeps_schema_consistency_and_required_process_inputs(self):
        for content in [
            'services:\n  api:\n    image: example:latest\n    depends_on: [missing]\n',
            'services:\n  api:\n    image: example:latest\n    networks: [missing]\n',
            'services:\n  api:\n    read_only: true\n',
            'services:\n  api:\n    image: example:latest\n    env_file: 19\n',
            'services:\n  api:\n    image: ${ABSENT_COMPOSE_INPUT:?process input required}\n']:
            with self.subTest(content=content):
                self.source.write_text(content)
                with self.assertRaises(subprocess.CalledProcessError):
                    images.validate_compose(self.root, self.config, self.environment, self.run_native)

    def test_real_cli_environment_file_scope_cannot_escape_or_resolve_dynamic_paths(self):
        for path in ['/outside/secret.env', '../outside.env', r'..\outside.env',
                     'https://host.invalid/file.env', '${PRIVATE_FILE}', 'compose.yml']:
            with self.subTest(path=path):
                self.source.write_text('services:\n  api:\n    image: example:latest\n    env_file: ' + json.dumps(path) + '\n')
                with self.assertRaises(ValueError):
                    images.compose_model(self.root, self.config, self.environment)
        outside = self.root / 'outside.env'
        outside.write_text("CHECKOUT_PRIVATE_SENTINEL='unterminated\n")
        (self.root / 'deploy/linked.env').symlink_to(outside)
        self.source.write_text('services:\n  api:\n    image: example:latest\n    env_file: linked.env\n')
        with self.assertRaisesRegex(ValueError, 'symlinks|escapes'):
            images.compose_model(self.root, self.config, self.environment)

    def test_returned_scope_preserves_required_secret_refs_on_every_service(self):
        self.source.write_text('services:\n  renderer:\n    image: example:latest\n    env_file: secrets.env\n')
        model = images.compose_model(self.root, self.config, self.environment)
        self.assertEqual(model['services']['renderer']['env_file'],
                         [{'path': 'secrets.env', 'required': True}])


if __name__ == '__main__':
    unittest.main()
