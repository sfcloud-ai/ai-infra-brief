import json
import unittest
from unittest.mock import patch
import scheduled_socket as socket

class Queue:
    def __init__(self):self.reads=[];self.writes=[]
    def status(self,id):self.reads.append(id);return {"status":"not_found"}
    def admit(self,*args):self.writes.append(args);return {"status":"queued"}

class Tests(unittest.TestCase):
    def test_status_is_pure_read(self):
        q=Queue();socket.dispatch(b'{"action":"status","request_id":"day"}',q)
        self.assertEqual(q.reads,["day"]);self.assertEqual(q.writes,[])
    def test_unknown_fields_operations_and_duplicate_keys_fail_closed(self):
        q=Queue()
        for value in [b'{"action":"status","action":"admit","request_id":"day"}',b'{"action":"send","request_id":"day"}',b'{"action":"status","request_id":"day","destination":"other"}']:
            with self.assertRaises(ValueError):socket.dispatch(value,q)
        self.assertEqual(q.writes,[])
    def test_peer_rejection_before_request_read(self):
        q=Queue()
        with patch.object(socket,'peer_uid',return_value=123),patch.object(socket,'bounded_read',side_effect=AssertionError("must not read")):
            socket.handle(object(),456,q)
        self.assertEqual(q.reads,[]);self.assertEqual(q.writes,[])
    def test_admit_has_no_send_or_tick(self):
        q=Queue();socket.dispatch(json.dumps({"action":"admit","request_id":"day","bundle_base64":"eA==","report_commit":"a"*40}).encode(),q)
        self.assertEqual(q.writes,[("day",b'x','a'*40)])

if __name__=='__main__':unittest.main()
