from source_fixture import SDK_ROOT
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(SDK_ROOT / 'cookbook'))
import application_fixtures


class IntrinsicApplicationFixtures(unittest.TestCase):
    def test_recovery_uses_descriptor_bound_native_recipe_with_original_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls=[]
            config={'components':{'recovery_fixture':{'timeout':900}}}
            self.assertTrue(application_fixtures.recovery(root,config,{},lambda *args,**kwargs:calls.append((args,kwargs))))
            command=calls[0][0][0]
            self.assertEqual(Path(command[-1]),application_fixtures.COOKBOOK/'cookbook/application_synthetic.py')
            self.assertEqual(command[1:3],['-I','-B'])
            self.assertEqual(calls[0][0][-1],900)
            self.assertNotIn('--engine',command)
            self.assertNotIn('--project',command)

    def test_recovery_refuses_unbounded_or_unknown_policy_before_effects(self):
        for policy in [{'timeout':0},{'timeout':3601},{'timeout':True},{'engine':'postgres:latest'}]:
            with self.subTest(policy=policy),self.assertRaises(ValueError):
                application_fixtures.recovery(Path('/unused'),{'components':{'recovery_fixture':policy}},{},
                    lambda *args,**kwargs:self.fail('unadmitted fixture ran'))
        self.assertFalse(application_fixtures.recovery(Path('/unused'),{}, {},lambda *args:self.fail('undeclared fixture ran')))
