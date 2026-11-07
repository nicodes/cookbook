from source_fixture import SDK_ROOT
"""Source caller identity and production deadlines remain explicit in shared static jobs."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import godot_static_workflows as workflows


class StaticCallers(unittest.TestCase):
    def test_manual_workflow_inputs_source_policy_and_historical_restore_authority_are_preserved(self):
        data={'komizo_ref':'a'*40,'deploy_runner':'blacksmith-4vcpu-ubuntu-2404','deploy_timeout':15}
        static={'repository':'example/game','production':True,'recovery':True,
          'production_inputs':{'source':'source_commit','release':'release_id'},'production_current_source':False}
        plan={'production':{'include':[{'runner':'blacksmith-4vcpu-ubuntu-2204','timeout_minutes':15}]}}
        with patch('delivery_workflows.declaration',return_value=data),patch.object(workflows.godot_static,'declaration',return_value=static),patch.object(workflows.godot_ci,'plan',return_value=plan):
            files=workflows.files(Path('.'),{},'b'*40)
        public=files['.github/workflows/production-verification.yml']
        self.assertIn('inputs.source_commit',public);self.assertIn('inputs.release_id',public)
        self.assertIn('source: ${{ github.sha }}',public);self.assertNotIn('secrets.',public)
        recovery=files['.github/workflows/recovery.yml']
        verification,restore=recovery.split('  restore:',1)
        self.assertNotIn('secrets.',verification);self.assertNotIn('packages: write',verification)
        self.assertIn('needs: verify',restore);self.assertIn('environment: production',restore)
        self.assertIn('needs.verify.outputs.proof-sha256',restore)
        self.assertIn('KOMIZO_DEPLOY_KEY: ${{ secrets.KOMIZO_DEPLOY_KEY }}',restore)
        self.assertNotIn('KOMIZO\n',recovery)
        self.assertNotIn('id-token: write',restore)
        self.assertIn('source_run_id:',recovery)

    def test_source_cd_retains_host_identity_while_readonly_production_uses_existing_gate(self):
        data={'komizo_ref':'a'*40,'publish_runner':'blacksmith-4vcpu-ubuntu-2404','deploy_runner':'blacksmith-4vcpu-ubuntu-2404',
          'publish_timeout':15,'deploy_timeout':15,'notification_url':'https://game.example.test/'}
        plan={'production':{'include':[{'runner':'blacksmith-4vcpu-ubuntu-2204','timeout_minutes':15}]}}
        with patch('delivery_workflows.declaration',return_value=data),patch.object(workflows.godot_static,'declaration',return_value={'production':True}),patch.object(workflows.godot_ci,'plan',return_value=plan):
            source=workflows.jobs(Path('.'),{},'b'*40)
        self.assertIn('  cd:\n',source);self.assertIn('environment: production',source)
        self.assertIn('group: deploy-production\n      cancel-in-progress: false',source)
        self.assertIn('uses: nicodes/komizo-actions/godot-static-delivery@'+'a'*40,source)
        deployment,public=source.split('  public_verification:',1)
        self.assertNotIn('workflow_call',deployment)
        self.assertIn('id-token: write',deployment)
        public=public.split('  report:',1)[0]
        self.assertNotIn('secrets.',public);self.assertNotIn('packages: write',public)
        self.assertIn('runner: blacksmith-4vcpu-ubuntu-2204',public);self.assertIn('timeout-minutes: 15',public)
        self.assertIn('needs.cd.outputs.release-id',public)


if __name__=='__main__':unittest.main()
