from source_fixture import SDK_ROOT
"""Keep full canonical browser coverage and reviewed SDK/runtime identities."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import godot_browser as browser


class BrowserControls(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        package=self.root/'tests/browser';package.mkdir(parents=True)
        (package/'package.json').write_text('{"private":true}')
        for name in ['run','cache','startup','tls-proxy']:(package/(name+'.mjs')).write_text('throw Error("owned fixture only");')
        (self.root/'mise.toml').write_text('[tools]\n"github:fixture/gd-playwright-cli"={version="cli-v0.0.8",asset_pattern="gd-playwright-cli_Linux_x86_64.tar.gz",bin="gd-playwright-cli"}\n[env]\nGD_PLAYWRIGHT_ASSET_SHA256="'+'a'*64+'"\nGODOT_BROWSER_RUNTIME_IMAGE="mcr.microsoft.com/playwright@sha256:'+'b'*64+'"\n')
        self.config={'components':{'browser':{'package':'tests/browser','hostname':'game.example','reports':'reports/browser',
            'runner':'tests/browser/run.mjs','cache_runner':'tests/browser/cache.mjs','startup_runner':'tests/browser/startup.mjs',
            'tls_proxy':'tests/browser/tls-proxy.mjs','cli_tool':'github:fixture/gd-playwright-cli','browsers':['chromium','firefox','webkit']}},
            'node':{'packages':[{'directory':'tests/browser','manager':'npm'}]}}
        sdk=self.root/'.artifacts/browser-sdk';sdk.mkdir(parents=True)
        (sdk/'gd-playwright-cli').write_bytes(b'reviewed fixture binary')
        (sdk/'gd-playwright-cli.json').write_text(json.dumps({'source':{'repository':'fixture/gd-playwright-cli','version':'cli-v0.0.8',
            'asset':'gd-playwright-cli_Linux_x86_64.tar.gz','sha256':'a'*64},'binary_sha256':hashlib.sha256(b'reviewed fixture binary').hexdigest()}))

    def test_canonical_native_entry_always_executes_all_three_browsers(self):
        entry={'runner':'godot-browser','path':'tests/browser/run.mjs','timeout':900}
        environment={'BROWSER':'chromium','BROWSER_TEST_MODE':'retention','BROWSER_STARTUP':'1','BROWSER_SOAK_SECONDS':'1'}
        with patch.object(browser.subprocess,'run') as run:
            browser.journeys(self.root,self.config,environment,entry)
            call=run.call_args
            self.assertEqual(call.kwargs['env']['BROWSER'],'chromium firefox webkit')
            self.assertEqual(call.kwargs['env']['BROWSER_TEST_MODE'],'')
            self.assertEqual(call.kwargs['env']['BROWSER_STARTUP'],'0')
            self.assertEqual(call.kwargs['timeout'],900)
            self.assertEqual(call.kwargs['env']['GODOT_BROWSER_RUNNER'],'tests/browser/run.mjs')
            self.assertEqual(len(run.call_args_list),1)

    def test_changed_cli_bytes_or_source_pin_are_refused_before_browser_launch(self):
        executable=self.root/'.artifacts/browser-sdk/gd-playwright-cli'
        for data in [b'tainted']:
            executable.write_bytes(data)
            with patch.object(browser.subprocess,'run') as run,self.assertRaises(ValueError):
                browser.journeys(self.root,self.config,{})
            run.assert_not_called()
        executable.write_bytes(b'reviewed fixture binary')
        path=self.root/'mise.toml';path.write_text(path.read_text().replace('cli-v0.0.8','cli-v0.0.9'))
        with patch.object(browser.subprocess,'run') as run,self.assertRaises(ValueError):browser.journeys(self.root,self.config,{})
        run.assert_not_called()

    def test_runtime_drift_alternate_program_or_reduced_matrix_cannot_pass(self):
        with self.assertRaises(ValueError):browser.journeys(self.root,self.config,{'BROWSER_RUNTIME_IMAGE':'moving:latest'})
        with self.assertRaises(ValueError):browser.journeys(self.root,self.config,{}, {'path':'tests/browser/startup.mjs'})
        self.config['components']['browser']['browsers']=['chromium']
        with self.assertRaises(ValueError):browser.declaration(self.root,self.config)

    def test_native_programs_cannot_escape_owned_tests_or_follow_links(self):
        value=self.config['components']['browser'];value['runner']='app/runner.mjs'
        (self.root/'app').mkdir();(self.root/'app/runner.mjs').write_text('owned app code')
        with self.assertRaises(ValueError):browser.declaration(self.root,self.config)
        value['runner']='tests/browser/run.mjs'
        (self.root/value['runner']).unlink();(self.root/value['runner']).symlink_to(self.root/'app/runner.mjs')
        with self.assertRaises(ValueError):browser.declaration(self.root,self.config)

    def test_named_ordinary_and_diagnostic_journeys_select_exact_declared_images(self):
        value=self.config['components']['browser']
        (self.root/'tests/browser/blackbox.mjs').write_text('throw Error("owned ordinary fixture");')
        value['journeys']=[{'name':'ordinary','path':'tests/browser/blackbox.mjs','image':'gate'},
                           {'name':'diagnostic','path':'tests/browser/run.mjs','image':'gate-diagnostic'}]
        value['forward_environment']=['BROWSER_SOLUTION_POSITION','BROWSER_BLOCK_POSITION']
        definition={'image_base':'ghcr.io/fixture/game','inputs':{'images':['ghcr.io/fixture/game-gate:{revision}']}}
        self.config['components']['images']=[{'name':'gate'},{'name':'gate-diagnostic'}]
        self.config['components']['fixture_images']={'directory':'.artifacts/fixtures/images'}
        with patch.object(browser.images,'definition',return_value=definition),patch.object(browser.subprocess,'run') as run:
            for journey in value['journeys']:
                browser.journeys(self.root,self.config,{'SOURCE_COMMIT':'c'*40,'EXPECTED_RELEASE_ID':'d'*64},
                    {'runner':'godot-browser','path':journey['path'],'timeout':900})
                environment=run.call_args.kwargs['env']
                self.assertEqual(environment['GATE_IMAGE'],'ghcr.io/fixture/game-'+journey['image']+':'+'c'*40)
                self.assertEqual(environment['GODOT_BROWSER_RUNNER'],journey['path'])
                self.assertEqual(environment['EXPECTED_RELEASE_ID'],'d'*64)
                self.assertEqual(environment['BROWSER'],'chromium firefox webkit')
                self.assertEqual(environment['GODOT_BROWSER_FORWARD_ENVIRONMENT'],'BROWSER_SOLUTION_POSITION BROWSER_BLOCK_POSITION')
            value['journeys'][1]['image']='unshipped'
            with self.assertRaises(ValueError):browser.declaration(self.root,self.config)

    def test_forwarding_cannot_smuggle_workflow_sdk_or_token_authority(self):
        for key in ['GH_TOKEN','GITHUB_TOKEN','GITHUB_RUN_ID','MISE_DATA_DIR','COOKBOOK_PREVIEW_NUMBER','REQUEST_HEAD']:
            self.config['components']['browser']['forward_environment']=[key]
            with self.assertRaises(ValueError):browser.declaration(self.root,self.config)


if __name__=='__main__':unittest.main()
