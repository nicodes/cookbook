from source_fixture import SDK_ROOT
"""Exercise actual platform paths through nested Cargo cache and audit planning."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(SDK_ROOT/'cookbook'))
import compiler_cache
import rust_cli
from configuration import relative


class CargoRecipePaths(unittest.TestCase):
    def test_nested_manifest_cache_and_audit_use_real_owned_lockfiles(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            (root/'mise.toml').write_text('[tools]\npython="3.13.11"\n')
            manifests=['Cargo.toml', 'crates/fixture/Cargo.toml']
            for manifest in manifests:
                path=root/manifest;path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text('[package]\nname="fixture"\nversion="0.0.1"\n')
                path.with_name('Cargo.lock').write_text('version=4\n')
            config={'version':1, 'recipe':'rust-cli',
                    'components':{'rust':[{'name':'cli' if index==0 else 'fixture',
                                           'manifest':name, 'profile':'release'}
                                          for index,name in enumerate(manifests)]},
                    'package':{'binary':'fixture', 'version_arguments':['--version'],
                               'version_template':'{binary} {version} ({commit})',
                               'native_targets':[{'platform':'Linux', 'arch':'x86_64',
                                   'runner':'ubuntu-24.04', 'target':'x86_64-unknown-linux-musl',
                                   'extension':'tar.gz'}]}}
            environment={'CARGO_TARGET_DIR':str(root/'.artifacts/rust-target'), 'CARGO_HOME':str(root/'.cargo'),
                         'VERSION':'dev', 'COMMIT':'a'*40}
            result=compiler_cache.plan(root,config,{},environment,'a'*40,'Windows','X64')
            self.assertTrue(result['enabled'])
            self.assertIn(str(root/'.artifacts/rust-target'),result['paths'])
            with patch.object(rust_cli,'run') as command:
                rust_cli.security(root,config,environment)
            audits=[call.args[0] for call in command.call_args_list if call.args[0][:2]==['cargo','audit']]
            self.assertEqual([args[args.index('--file')+1] for args in audits],
                             [str(root/'Cargo.lock'),str(root/'crates/fixture/Cargo.lock')])
            with self.assertRaises(ValueError):relative(root,'crates\\fixture\\Cargo.lock',exists=True)


if __name__=='__main__':unittest.main()
