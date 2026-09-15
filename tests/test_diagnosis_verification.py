import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
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

    def test_known_failures_keep_counts_even_with_success_exit(self):
        output='Ran 109 tests in 1s\nFAILED (failures=1)\n'
        for code in (0,1,None):
            result=verifier.classify(code,output,'python',109)
            self.assertEqual(result['status'],'failed');self.assertEqual(result['executed'],109);self.assertEqual(result['failed'],1)
        result=verifier.classify(0,'Ran 109 tests in 1s\nOK (skipped=2)\n','python',109)
        self.assertEqual(result['skipped'],2);self.assertEqual(result['status'],'incomplete')
        result=verifier.classify(1,'  OK   first\n  FAIL second\n','python',109)
        self.assertEqual((result['executed'],result['failed'],result['status']),(2,1,'failed'))

    def test_timeout_cleans_owned_container_and_rejects_foreign_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);container=root/'container';container.mkdir();binary=root/'bin';binary.mkdir()
            identifier='diagnosis-test-0123456789ab';(container/'owner.json').write_text(json.dumps({'identifier':identifier}))
            marker=root/'marker';marker.write_text(identifier)
            fake=binary/'docker'
            fake.write_text('#!'+sys.executable+'\n'+'''import json,os,sys
from pathlib import Path
marker=Path(os.environ['DIAGNOSIS_TEST_MARKER'])
args=sys.argv[1:]
if args[:2]==['container','ls']:
 print(marker.read_text() if marker.exists() else '')
elif args[0]=='inspect':
 print(json.dumps([{'Config':{'Labels':{'diagnosis.owner':os.environ.get('DIAGNOSIS_TEST_OWNER',marker.read_text())}}}]))
elif args[:2]==['rm','-f']:
 marker.unlink()
else:sys.exit(2)
''');fake.chmod(0o700)
            env={**os.environ,'PATH':str(binary)+os.pathsep+os.environ['PATH'],'DIAGNOSIS_TEST_MARKER':str(marker),'PYTHONDONTWRITEBYTECODE':'1'}
            code=verifier.execute([sys.executable,'-c','import time; time.sleep(10)'],root/'timeout.log',env,.1)
            self.assertIsNone(code)
            self.assertEqual(verifier.cleanup_container(sys.executable,env,root),0);self.assertFalse(marker.exists())
            marker.write_text(identifier);env['DIAGNOSIS_TEST_OWNER']='another-owner'
            self.assertNotEqual(verifier.cleanup_container(sys.executable,env,root),0);self.assertTrue(marker.exists())
