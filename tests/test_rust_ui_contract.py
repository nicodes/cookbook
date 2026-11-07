from source_fixture import SDK_ROOT
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import rust_cli


class RustUiInputs(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'mise.toml').write_text('[tools]\npython="3.13.11"\nrust="1.94.0"\n')
        self.config = {'recipe': 'rust-cli', 'components': {'rust': [{'name': 'cli', 'manifest': 'Cargo.toml'}], 'browser': {'package': 'app'}}}

    def test_browser_setup_is_explicit_host_setup_after_locked_node_install(self):
        calls = []
        browser = SimpleNamespace(prepare=lambda *_args, **_kwargs: calls.append('browser'))
        for stage, partial, expected in [('fast', '', ['node']), ('full', '["api"]', ['cargo', 'node']),
                                          ('full', '', ['cargo', 'node', 'browser'])]:
            calls.clear()
            with patch.dict(os.environ, {'COOKBOOK_STAGE': stage, 'COOKBOOK_NATIVE_COMPONENTS': partial}), \
                    patch.object(rust_cli, 'required_tools', return_value=['python', 'rust']), \
                    patch.object(rust_cli, 'run'), patch.object(rust_cli, 'helper', return_value=browser), \
                    patch.object(rust_cli.rust_component, 'setup', side_effect=lambda *_args: calls.append('cargo')), \
                    patch.object(rust_cli.node_component, 'install', side_effect=lambda *_args: calls.append('node')):
                rust_cli.setup(self.root, self.config | {'node': {'packages': [{'directory': 'app'}]}}, {})
            self.assertEqual(calls, expected)

    def test_native_binary_input_paths_bind_only_reviewed_platform_fields(self):
        record = {'platform': 'Linux', 'arch': 'x86_64'}
        data = self.config | {'components': {**self.config['components'], 'native': [
            dict(name='api', component='cli', role='api', binary='api',
                 output='.artifacts/native-api/api_{platform}_{arch}')]}}
        with patch.object(rust_cli.subprocess, 'check_output', return_value='a' * 40), \
                patch.dict(os.environ, {}, clear=True), \
                patch.object(rust_cli, 'package_data', return_value={'output': '.artifacts/build/example'}), \
                patch.object(rust_cli, 'selected_target', return_value=record | {'target': 'x86_64-unknown-linux-musl'}):
            values = rust_cli.environment(self.root, data)
            self.assertEqual(values['COOKBOOK_NATIVE_API_BINARY'], str(self.root / '.artifacts/native-api/api_Linux_x86_64'))
            self.assertEqual(values['PLAYWRIGHT_BROWSERS_PATH'], str(self.root / '.artifacts/playwright'))
            data['components']['native'][0]['output'] = '{platform.__class__}'
            with self.assertRaises(ValueError): rust_cli.environment(self.root, data)

    def test_native_setup_selects_actual_runtime_sdks_without_repeating_host_security_tools(self):
        (self.root / 'mise.toml').write_text('[tools]\npython="3.13.11"\nrust="1.94.0"\n'
            '"cargo:cargo-audit"="0.22.2"\ntrivy="0.74.0"\nactionlint="1.7.12"\n'
            'shellcheck="0.11.0"\nnode="26.7.0"\nbun="1.4.1"\ncaddy="2.11.6"\n')
        (self.root / 'app').mkdir()
        (self.root / 'app/package.json').write_text('{"scripts":{"build":"vite build"}}')
        config = self.config | {'node': {'packages': [{'directory': 'app', 'build': 'build'}]},
            'tests': {'unit': [{'runner': 'bun-test', 'path': 'tests/unit.test.ts'}]},
            'security': {'rust_inventory_manifests': ['Cargo.toml']}}
        config['components']['native_tools'] = ['caddy']
        self.assertEqual(set(rust_cli.required_tools(self.root, config, 'native')), {'python', 'rust', 'node', 'bun', 'caddy'})
        self.assertTrue({'cargo:cargo-audit', 'trivy', 'actionlint', 'shellcheck'} <= set(rust_cli.required_tools(self.root, config, 'full')))
        self.assertTrue({'cargo:cargo-audit', 'trivy'} <= set(rust_cli.required_tools(self.root, config, 'security')))
        config['components']['native_tools'] = ['--anything']
        with self.assertRaises(ValueError): rust_cli.required_tools(self.root, config, 'native')


if __name__ == '__main__': unittest.main()
