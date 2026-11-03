import pathlib
import sys
import tempfile
import unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/"integrations/slack_wecom"))
from scheduled_verifier import NodePublicationVerifier

class Tests(unittest.TestCase):
    def test_keeps_one_verifier_process_and_original_bundle_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            script=pathlib.Path(directory)/'fixture.py'
            script.write_text('import sys,json,base64\nfor i,line in enumerate(sys.stdin):\n r=json.loads(line);print(json.dumps({"ok":True,"result":{"poster_base64":r["bundle_base64"],"sequence":i}}),flush=True)\n')
            verifier=NodePublicationVerifier(sys.executable,str(script))
            try:
                a=verifier(b'original bytes','a'*40);pid=verifier.process.pid
                b=verifier(b'next bytes','a'*40)
                self.assertEqual(a,{"poster":b'original bytes',"sequence":0})
                self.assertEqual(b,{"poster":b'next bytes',"sequence":1})
                self.assertEqual(pid,verifier.process.pid)
            finally:verifier.close()
    def test_negative_or_trailing_result_does_not_authorize(self):
        with tempfile.TemporaryDirectory() as directory:
            script=pathlib.Path(directory)/'fixture.py'
            script.write_text('import sys\nfor line in sys.stdin:print(\'{"ok":false,"error":"publication_not_verified"}\',flush=True)\n')
            verifier=NodePublicationVerifier(sys.executable,str(script))
            try:
                with self.assertRaises(ValueError):verifier(b'bytes','a'*40)
                self.assertIsNotNone(verifier.process)
            finally:verifier.close()

if __name__=='__main__':unittest.main()
