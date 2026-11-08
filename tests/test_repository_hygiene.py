"""Exercise source-path rejection without reading environment contents."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cookbook'))
import mise_entrypoint
import repository_hygiene as hygiene


class RepositoryHygiene(unittest.TestCase):
    def repository(self, root, names):
        subprocess.run(['git','-C',str(root),'init','-q'],check=True)
        for name in names:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('synthetic fixture only\n')
        subprocess.run(['git','-C',str(root),'add','-f','.'],check=True)

    def test_tracked_active_artifacts_are_case_insensitive_and_never_read(self):
        for name in ['app/.env.example.local','deploy/postgres-owner.env.example','THIRD-PARTY-ACTIONS.json',
                     'deploy/FLEET-POLICY.toml','engineering-plan.yaml']:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.repository(root,[name])
                with patch.object(Path,'read_bytes',side_effect=AssertionError('artifact contents read')), \
                        patch.object(Path,'read_text',side_effect=AssertionError('artifact contents read')):
                    with self.assertRaisesRegex(ValueError,'retired active source artifacts'):
                        hygiene.check_tracked(root)

    def test_testdata_vendored_history_and_ignore_patterns_are_not_active_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.repository(root,['tests/fixtures/.env','testdata/.env.example',
                'vendor/parser/.env','history/legacy/fleet-policy.json',
                '.gitignore','addons/gd_env/parser.gd'])
            hygiene.check_tracked(root)
            hygiene.check_configuration({'tests':{'unit':[{'path':'tests/parser.gd','args':['.env']}]},
                'inputs':{'exclude':['.env*']},'policy':{'forbidden_names':['.env.json']}})

    def test_configured_paths_and_source_file_destinations_cannot_recreate_artifacts(self):
        for value in [{'artifacts':{'paths':['app/.env.json']}},
                      {'inputs':{'configuration':['fleet-policy.json']}},
                      {'components':{'godot':{'exports':[{'source_files':{'.env.json':'config/public.json'}}]}}}]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError,'retired active source artifact configured'):
                hygiene.check_configuration(value)

    def test_deleted_tracked_artifact_does_not_block_cleanup_before_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.repository(root,['.env.example'])
            (root/'.env.example').unlink()
            hygiene.check_tracked(root)

    def test_dotenv_loading_directives_refuse_without_loading_source_or_running_mise(self):
        for declaration in ["[env._]\nfile='.env'\n", "[env._]\nfile=['.env','.env.local']\n",
                            "[env]\n_.file='.env.example'\n"]:
            with self.subTest(declaration=declaration), \
                    patch.object(Path,'read_bytes',side_effect=AssertionError('dotenv read')), \
                    patch('subprocess.run',side_effect=AssertionError('Mise executed')), \
                    self.assertRaisesRegex(ValueError,'Mise dotenv file loading is not supported'):
                mise_entrypoint.prefix_data('[tools]\npython="3.13.11"\n'+declaration)

    def test_public_environment_and_path_configuration_remain_native_mise_data(self):
        source = '[tools]\npython="3.13.11"\n[env]\nPUBLIC_ORIGIN="https://example.invalid"\n_.path=["{{config_root}}/bin"]\n'
        self.assertEqual(mise_entrypoint.prefix_data(source)['env']['PUBLIC_ORIGIN'],'https://example.invalid')
        self.assertEqual(mise_entrypoint.prefix_data(source)['env']['_']['path'],['{{config_root}}/bin'])

    def test_dotenv_shell_sources_refuse_while_ordinary_sources_remain_native_data(self):
        for value in ["'.env'", "['deploy/postgres-owner.env.example']", "{path='.ENV.local'}"]:
            with self.subTest(value=value), \
                    patch.object(Path,'read_bytes',side_effect=AssertionError('dotenv read')), \
                    patch('subprocess.run',side_effect=AssertionError('Mise executed')), \
                    self.assertRaisesRegex(ValueError,'retired active source artifact configured'):
                mise_entrypoint.prefix_data('[tools]\npython="3.13.11"\n[env._]\nsource='+value+'\n')
        source = '[tools]\npython="3.13.11"\n[env._]\nsource="scripts/public-environment.sh"\n'
        self.assertEqual(mise_entrypoint.prefix_data(source)['env']['_']['source'],
                         'scripts/public-environment.sh')


if __name__ == '__main__':
    unittest.main()
