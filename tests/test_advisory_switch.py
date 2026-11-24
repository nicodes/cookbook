from source_fixture import SDK_ROOT
"""COOKBOOK_ADVISORIES=skip removes advisory scans and their outputs; deterministic checks stay."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import advisories
import godot_game
import godot_security
import node_component
import react_native
import rust_cli

AUDIT = 'github:rustsec/rustsec'
SCAN_EVIDENCE = ['api/security.json', 'api/sbom.json', 'assets/web/scan-context.json']


def skipped():
    return patch.dict(os.environ, {advisories.VARIABLE: 'skip'})


def running():
    environment = patch.dict(os.environ)
    environment.start()
    os.environ.pop(advisories.VARIABLE, None)
    return environment


class Switch(unittest.TestCase):
    def test_unset_and_run_keep_every_scan_and_only_skip_skips(self):
        with running():
            self.assertFalse(advisories.skipped())
            self.assertFalse(advisories.skipped({}))
            for value, expected in [('', False), ('run', False), ('skip', True)]:
                with patch.dict(os.environ, {advisories.VARIABLE: value}):
                    self.assertIs(advisories.skipped(), expected)
            self.assertTrue(advisories.skipped({advisories.VARIABLE: 'skip'}))
            self.assertFalse(advisories.skipped({advisories.VARIABLE: 'run'}))

    def test_any_other_value_is_refused_rather_than_read_as_run(self):
        for value in ['Skip', 'true', '0', 'none']:
            with self.subTest(value=value), patch.dict(os.environ, {advisories.VARIABLE: value}), \
                    self.assertRaisesRegex(ValueError, 'run or skip'):
                advisories.skipped()


class Application(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pins = ['python', 'actionlint', 'github:aviorstudio/gdlint', 'pipx:gdtoolkit', 'rust', 'bun', AUDIT, 'trivy']
        (self.root / 'mise.toml').write_text('[tools]\n' + '\n'.join(json.dumps(name) + '="1.0.0"' for name in self.pins) + '\n')
        (self.root / 'Cargo.toml').write_text('[package]\nname="fixture"\nversion="0.1.0"\n')
        (self.root / 'Cargo.lock').write_text('version=4\n')
        self.config = {'components': {'rust': [{'manifest': 'Cargo.toml'}], 'image_security': {'reports': '.artifacts/reports/security'}},
                       'security': {'rust_inventory_manifests': ['Cargo.toml'], 'rust_inventory_sbom': True,
                                    'auth_policy': 'auth.toml'},
                       'tests': {'security': [{'runner': 'python', 'path': 'tests/security.py'}]}}
        self.addCleanup(running().stop)

    def test_godot_default_requires_and_installs_every_scanner(self):
        for stage in ['full', 'local']:
            tools = godot_game.required_tools(self.root, self.config, stage)
            self.assertTrue({AUDIT, 'trivy'} <= set(tools))
        with patch.object(godot_security, 'evidence_paths', return_value=SCAN_EVIDENCE):
            self.assertEqual(godot_game.evidence_paths(self.root, self.config),
                             ['.artifacts/security/rust-source.json', '.artifacts/security/rust-source-sbom.json', *SCAN_EVIDENCE])
        self.assertIn('.artifacts/reports/security', godot_game.artifact_paths(self.root, self.config))

    def test_godot_skip_drops_scanner_tools_evidence_and_report_directory(self):
        (self.root / 'mise.toml').write_text('[tools]\n' + '\n'.join(
            json.dumps(name) + '="1.0.0"' for name in self.pins if name not in {AUDIT, 'trivy'}) + '\n')
        with skipped():
            for stage in ['full', 'local', 'build', 'security']:
                tools = godot_game.required_tools(self.root, self.config, stage)
                self.assertFalse({AUDIT, 'trivy'} & set(tools), stage)
                self.assertIn('rust', tools)
            self.assertEqual(godot_game.evidence_paths(self.root, self.config), [])
            self.assertNotIn('.artifacts/reports/security', godot_game.artifact_paths(self.root, self.config))
        (self.root / 'mise.toml').write_text('[tools]\n' + '\n'.join(json.dumps(name) + '="1.0.0"' for name in self.pins) + '\n')
        with skipped():
            self.assertFalse({AUDIT, 'trivy'} & set(godot_game.required_tools(self.root, self.config, 'full')))

    def test_godot_skip_keeps_secret_scan_auth_boundary_and_declared_security_tests(self):
        commands = []
        with skipped(), patch.object(godot_game, 'run', side_effect=lambda argv, *args, **kwargs: commands.append(argv)), \
                patch.object(godot_game.images, 'rust_inventory') as inventory, \
                patch.object(godot_security, 'scan') as scan, \
                patch.object(godot_game.node_component, 'run_script'), \
                patch.object(godot_game, 'native_tests') as tests:
            godot_game.security(self.root, self.config, dict(os.environ))
        inventory.assert_not_called()
        scan.assert_not_called()
        self.assertFalse([argv for argv in commands if argv[:2] == ['cargo', 'audit']])
        self.assertTrue(any(argv[-1:] == ['auth.toml'] or 'auth_boundary.py' in ' '.join(argv) for argv in commands))
        self.assertTrue(any('source-secrets.py' in ' '.join(argv) for argv in commands))
        tests.assert_called_once()
        self.assertEqual(tests.call_args.args[-1], 'security')

    def test_godot_default_still_audits_inventories_and_scans(self):
        commands = []
        with patch.object(godot_game, 'run', side_effect=lambda argv, *args, **kwargs: commands.append(argv)), \
                patch.object(godot_game.images, 'rust_inventory') as inventory, \
                patch.object(godot_security, 'scan') as scan, \
                patch.object(godot_game, 'native_tests'):
            godot_game.security(self.root, self.config, dict(os.environ))
        inventory.assert_called_once()
        scan.assert_called_once()
        self.assertEqual([argv for argv in commands if argv[:2] == ['cargo', 'audit']],
                         [['cargo', 'audit', '--file', 'Cargo.lock', '--deny', 'warnings']])

    def test_godot_scan_setup_is_skipped_but_other_setup_is_not(self):
        calls = []
        config = {'components': {'image_security': {'reports': 'r'}}}
        for state, expected in [(running, [True]), (skipped, [])]:
            calls.clear()
            with state(), patch.dict(os.environ, {'COOKBOOK_STAGE': 'full'}), \
                    patch.object(godot_game, 'required_tools', return_value=['python']), patch.object(godot_game, 'run'), \
                    patch.object(godot_game.node_component, 'install'), patch.object(godot_game.playwright_sdk, 'setup'), \
                    patch.object(godot_security, 'setup', side_effect=lambda *_: calls.append(True)), \
                    patch.object(godot_game, 'project', return_value=(self.root, {})), \
                    patch.object(godot_game.godot_sdk, 'inputs', return_value=None):
                try:
                    godot_game.setup(self.root, config, dict(os.environ))
                except Exception:
                    pass  # Only the scan-setup boundary, before project preparation, is under test.
            self.assertEqual(calls, expected)

    def test_node_audit_is_skipped_and_counts_nothing(self):
        package = {'directory': 'app', 'manager': 'npm'}
        with patch.object(node_component, 'validate', return_value=(package, self.root)):
            calls = []
            with running():
                node_component.audit(self.root, [package], {}, lambda *args: calls.append(args))
            self.assertEqual(len(calls), 1)
            calls.clear()
            with skipped():
                self.assertEqual(node_component.audit(self.root, [package], dict(os.environ), lambda *args: calls.append(args)), 0)
            self.assertEqual(calls, [])

    def test_rust_cli_skip_drops_lockfile_audit_and_trivy_paths_only(self):
        config = {'components': {'rust': [{'manifest': 'Cargo.toml'}]},
                  'security': {'trivy_paths': ['.'], 'rust_inventory_manifests': ['Cargo.toml']}}
        commands = []
        with skipped(), patch.object(rust_cli, 'components', return_value=config['components']['rust']), \
                patch.object(rust_cli, 'run', side_effect=lambda argv, *args, **kwargs: commands.append(argv)), \
                patch.object(rust_cli, 'product_tests'), patch.object(rust_cli.node_component, 'audit'), \
                patch('images.rust_inventory') as inventory:
            rust_cli.security(self.root, config, dict(os.environ))
            tools = rust_cli.required_tools(self.root, config, 'security')
        self.assertFalse([argv for argv in commands if argv[0] in {'cargo', 'trivy'}])
        inventory.assert_called_once()
        self.assertNotIn(rust_cli.LOCKFILE_AUDIT_TOOL, tools)
        self.assertIn('trivy', tools)  # the inventory still needs it
        commands.clear()
        with running(), patch.object(rust_cli, 'components', return_value=config['components']['rust']), \
                patch.object(rust_cli, 'run', side_effect=lambda argv, *args, **kwargs: commands.append(argv)), \
                patch.object(rust_cli, 'product_tests'), patch.object(rust_cli.node_component, 'audit'), \
                patch('images.rust_inventory'):
            rust_cli.security(self.root, config, {})
        self.assertEqual({argv[0] for argv in commands if argv[0] != 'python3' and 'source-secrets' not in ' '.join(argv)},
                         {'cargo', 'trivy'})


class ReactNative(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'mise.toml').write_text('[tools]\npython="3.13.11"\nnode="24.18.1"\nactionlint="1.7.12"\ntrivy="0.74.0"\nsyft="1.0.0"\nrust="1.94.0"\n')
        self.config = {'components': {'image_security': {'reports': '.artifacts/reports/security'}},
                       'security': {'trivy_images': ['api'], 'spdx_images': ['api']},
                       'artifacts': {'evidence_paths': ['reports/browser']}}
        self.addCleanup(running().stop)

    def test_required_tools_artifacts_and_evidence_follow_the_switch(self):
        with running():
            self.assertTrue({'trivy', 'syft'} <= set(react_native.required_tools(self.root, self.config, 'full')))
        with skipped():
            for stage in ['full', 'security']:
                self.assertFalse({'trivy', 'syft'} & set(react_native.required_tools(self.root, self.config, stage)))
            self.assertEqual(react_native.evidence_paths(self.root, self.config), [])
            with patch.object(react_native.images, 'definition', return_value={'inputs': {'artifacts': [], 'images': []}}), \
                    patch.object(react_native, 'verification_scope', return_value={'artifacts': ['dist']}):
                self.assertEqual(react_native.artifact_paths(self.root, self.config), ['dist', 'reports/browser'])
        with running(), patch.object(react_native, 'verification_scope', return_value={'artifacts': ['dist']}):
            self.assertIn('.artifacts/reports/security', react_native.artifact_paths(self.root, self.config))

    def test_security_skips_every_scan_but_keeps_secrets_and_declared_tests(self):
        commands = []
        with skipped(), patch.object(react_native, 'run', side_effect=lambda argv, *args, **kwargs: commands.append(argv)), \
                patch.object(react_native.node_component, 'audit') as audit, \
                patch.object(react_native.images, 'rust_inventory') as inventory, \
                patch.object(react_native.images, 'runtime_image') as image, \
                patch.object(godot_security, 'scan') as scan, \
                patch.object(react_native, 'run_entries') as entries:
            config = {**self.config, 'tests': {'security': [{'runner': 'python', 'path': 'x.py'}]}}
            react_native.security(self.root, config, dict(os.environ))
        audit.assert_called_once()  # guarded inside node_component itself
        inventory.assert_not_called()
        image.assert_not_called()
        scan.assert_not_called()
        self.assertEqual(len(commands), 1)
        self.assertIn('source-secrets.py', ' '.join(commands[0]))
        entries.assert_called_once()

    def test_default_security_still_reaches_the_image_scans(self):
        with running(), patch.object(react_native, 'run'), patch.object(react_native.node_component, 'audit'), \
                patch.object(react_native.images, 'definition', return_value={}), \
                patch.object(godot_security, 'scan'), \
                self.assertRaisesRegex(ValueError, 'undeclared'):
            react_native.security(self.root, self.config, {})


if __name__ == '__main__':
    unittest.main()
