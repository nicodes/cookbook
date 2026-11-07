from source_fixture import SDK_ROOT, SDK_REF
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import generate
import mise_entrypoint as entry


def future(source, revision='e' * 40, recipe='godot-addon', groups=('lint', 'unit', 'security')):
    return entry.render('[tools]\npython="3.13.11"\n', revision, recipe,
                        source.encode(), list(groups), [])


class FutureIdentity(unittest.TestCase):
    source = 'version=1\nrecipe="godot-addon"\n[[tests.unit]]\nrunner="python"\npath="tests/check.py"\n'

    def prepare(self, root):
        (root / 'tests').mkdir()
        (root / 'tests/check.py').write_text('raise RuntimeError("must not execute")\n')
        (root / 'cookbook.toml').write_bytes(self.source.encode())
        (root / 'mise.toml').write_bytes(b'[tools]\npython="3.13.11"\n')

    def matches(self, record, raw=None):
        return entry.matches(record, tomllib.loads(self.source), raw or self.source.encode(),
                             test_groups=['lint', 'unit', 'security'])

    def test_decodes_closed_identity_and_preserves_native_receipt_group_order(self):
        value = entry.decode(future(self.source))
        self.assertEqual(value, {'version': 1, 'repository': 'nicodes/cookbook', 'revision': 'e' * 40,
            'recipe': 'godot-addon', 'configuration_sha256': hashlib.sha256(self.source.encode()).hexdigest(),
            'test_groups': ['lint', 'security', 'unit'], 'capabilities': []})
        self.assertEqual(self.matches(value), value)

    def test_native_text_newlines_preserve_future_identity_and_raw_configuration_hash(self):
        original = future(self.source)
        digest = hashlib.sha256(self.source.encode()).hexdigest()
        for newline in ['\n', '\r\n', '\r']:
            raw = original.replace('\n', newline).encode()
            before = hashlib.sha256(raw).hexdigest()
            with self.subTest(newline=repr(newline)):
                value = entry.decode(raw)
                self.assertEqual(value['configuration_sha256'], digest)
                self.assertEqual(self.matches(value), value)
                self.assertEqual(hashlib.sha256(raw).hexdigest(), before)
                self.assertEqual(raw.count(digest.encode()), 1)
                changed = raw.replace(digest.encode(), b'f' * 64)
                with self.assertRaises(ValueError):
                    self.matches(entry.decode(changed))

    def test_parser_does_not_execute_native_source_or_mise_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'unexpected-execution'
            malicious = "python3 -c " + repr("from pathlib import Path;Path(" + repr(str(marker)) + ").write_text('executed')")
            original = future(self.source)
            run = tomllib.loads(original)['tasks']['_sdk']['run']
            source = original.replace(json.dumps(run), json.dumps(malicious), 1)
            self.assertNotEqual(source, original)
            with patch('subprocess.run', side_effect=AssertionError('command executed')), \
                    patch('subprocess.check_output', side_effect=AssertionError('command executed')), \
                    patch.object(Path, 'read_bytes', side_effect=AssertionError('source loaded')):
                self.assertEqual(entry.decode(original)['revision'], 'e' * 40)
                with self.assertRaises(ValueError):
                    entry.decode(source)
            self.assertFalse(marker.exists())

    def test_unknown_partial_duplicate_oversize_and_control_overrides_refused(self):
        original = future(self.source)
        mutations = [original.replace('cookbook_contract = 1', 'cookbook_contract = 2', 1),
                     original.replace('cookbook_ref = "' + 'e' * 40 + '"', 'cookbook_ref = "main"', 1),
                     original.replace('cookbook_contract = 1', 'cookbook_contract = 1\nunknown_identity = true', 1),
                     original.replace('cookbook_contract = 1', 'cookbook_contract = 1\ncookbook_contract = 1', 1),
                     original.replace('task.run_auto_install = false', 'task.run_auto_install = 0', 1),
                     original.replace('task.run_auto_install = false', 'task.run_auto_install = true', 1),
                     original.replace('[tasks."test:unit"]', '[tasks."undeclared"]', 1),
                     original.replace('[tasks._sdk]', '[tasks.unknown-dispatcher]', 1),
                     original.replace('cookbook_recipe = "godot-addon"', 'cookbook_recipe = "custom"', 1),
                     original.replace(entry.END, entry.END + '[task_config]\nincludes=["remote"]\n'),
                     original + 'x' * entry.MAX_SOURCE]
        for value in mutations:
            self.assertNotEqual(value, original)
            with self.subTest(value=value[:200]), self.assertRaises(ValueError):
                entry.decode(value)

    def test_changed_config_recipe_or_missing_suite_cannot_select_another_contract(self):
        value = entry.decode(future(self.source))
        for override in [{'configuration_sha256': 'f' * 64}, {'recipe': 'rust-cli'},
                         {'test_groups': ['unit']}, {'capabilities': ['dev']},
                         {'repository': 'nicodes/tools'}, {'version': True}, {'revision': 'main'}]:
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.matches(value | override)

    def test_partial_new_identity_is_recognized_so_caller_cannot_fall_back(self):
        self.assertFalse(entry.declared('[tools]\npython="3.13.11"\n'))
        self.assertTrue(entry.declared('[vars]\ncookbook_contract=1\n'))
        self.assertTrue(entry.declared('[vars]\ncookbook_recipe="godot-addon"\n'))
        self.assertTrue(entry.declared('[tasks.extra]\nrun="never execute"\n'))
        for value in ['[vars]\ncookbook_contract=1\n', '[vars]\ncookbook_recipe="godot-addon"\n',
                      '[vars]\ntools_contract=1\n']:
            with self.assertRaises(ValueError):
                entry.decode(value)

    def test_canonical_entrypoint_and_native_command_have_one_Mise_ABI(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            expected = generate.entrypoint_files(root, 'e' * 40)['mise.toml']
            self.assertEqual(generate.entrypoint_files(root, 'e' * 40), {'mise.toml': expected})
            self.assertEqual(generate.task_command('test:security'), ['mise', 'run', 'test:security'])
            (root / 'mise.toml').write_bytes(expected.encode())
            generate.check_entrypoint(root, 'e' * 40)
            (root / 'mise.toml').write_bytes(expected.encode() + b'\n# changed\n')
            with self.assertRaises(ValueError):
                generate.check_entrypoint(root, 'e' * 40)

    def test_canonical_entrypoint_requires_bounded_regular_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            (root / 'mise.toml').unlink()
            with self.assertRaises(OSError):
                generate.check_entrypoint(root, 'e' * 40)
            (root / 'mise.toml').write_text('[tools]\npython="3.13.11"\n' + '#' * entry.MAX_SOURCE)
            with self.assertRaises(ValueError):
                generate.check_entrypoint(root, 'e' * 40)

    def test_native_text_newlines_preserve_canonical_Mise_comparison_and_raw_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            expected = generate.entrypoint_files(root, 'e' * 40)['mise.toml']
            for newline in ['\n', '\r\n', '\r']:
                raw = expected.replace('\n', newline).encode()
                (root / 'mise.toml').write_bytes(raw)
                with self.subTest(newline=repr(newline)):
                    generate.check_entrypoint(root, 'e' * 40)
                    self.assertEqual((root / 'mise.toml').read_bytes(), raw)
                (root / 'mise.toml').write_bytes(raw + b'# changed canonical source\n')
                with self.assertRaises(ValueError):
                    generate.check_entrypoint(root, 'e' * 40)

    def test_final_identity_refuses_any_residual_Makefile_at_native_source_gate(self):
        import ci
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            (root / 'mise.toml').write_bytes(generate.entrypoint_files(root, SDK_REF)['mise.toml'].encode())
            (root / 'Makefile').write_text('COOKBOOK_REF := ' + SDK_REF + '\n')
            for arguments in [['init', '-q'], ['add', '.'], ['-c', 'user.name=Source control',
                    '-c', 'user.email=source@example.invalid', '-c', 'commit.gpgsign=false', 'commit', '-qm', 'finite source']]:
                subprocess.run(['git', '-c', 'core.hooksPath=' + os.devnull, '-C', str(root), *arguments], check=True)
            revision = ci.git(root, 'rev-parse', 'HEAD')
            with self.assertRaisesRegex(ValueError, 'one task authority'):
                generate.check_entrypoint(root, SDK_REF)
            real_run = subprocess.run
            with patch.object(ci.subprocess, 'run', wraps=real_run) as native, \
                    self.assertRaisesRegex(ValueError, 'one task authority'):
                ci.identity(root, revision, SDK_REF)
            tracked = [call for call in native.call_args_list if call.args[0][:2] == ['git', '-C'] and 'diff' in call.args[0]]
            self.assertEqual(len(tracked), 2)
            self.assertTrue(all(call.args[0][:4] == ['git', '-C', str(path), 'diff']
                                for call, path in zip(tracked, [root, ci.COOKBOOK])))


if __name__ == '__main__':
    unittest.main()
