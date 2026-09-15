import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('diagnosis_verifier',Path(__file__).resolve().parents[1]/'scripts'/'verify_diagnosis.py')
verifier=importlib.util.module_from_spec(spec);spec.loader.exec_module(verifier)


class VerificationTests(unittest.TestCase):
    def test_framework_failure_missing_collection_and_skip_cannot_pass(self):
        output='Ran 34 tests in .1s\nRan 41 tests in .1s\nRan 12 tests in .1s\n'+'  OK   fixture\n'*22+'All engine and integration checks passed.'
        self.assertEqual(verifier.classify(0,output,'python',12)['status'],'passed')
        for code,text,count in [(1,output,12),(0,output,13),(0,output+'\nOK (skipped=1)',12),(0,'All engine and integration checks passed.',12),(0,output+'\nFAILED',12)]:
            self.assertNotEqual(verifier.classify(code,text,'python',count)['status'],'passed')
        self.assertNotEqual(verifier.classify(0,'{"status":"passed","checks":0,"skipped":0}','json')['status'],'passed')
        self.assertNotEqual(verifier.classify(1,'{"status":"passed","checks":10,"skipped":0}','json')['status'],'passed')
