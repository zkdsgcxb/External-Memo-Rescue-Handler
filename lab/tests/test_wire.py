"""UART report round trip, heartbeat compatibility, and malformed input bounds."""
import importlib.util
import json
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('wire',Path(__file__).resolve().parents[1]/'guest/wire.py')
wire=importlib.util.module_from_spec(spec)
spec.loader.exec_module(wire)


class WireTests(unittest.TestCase):
    def test_large_report_and_plain_heartbeat_keep_ids_and_content(self):
        report={'id':9,'ok':True,'result':{'workload':'a'*150000,'text':'测试\n"quote"'}}
        heartbeat={'event':'heartbeat','uptime':4.5}
        frames=wire.encode(report)+wire.encode(heartbeat)
        self.assertLess(len(frames),16000)
        self.assertEqual([wire.decode(json.loads(line)) for line in frames.splitlines()],[report,heartbeat])
        self.assertEqual(wire.decode({'id':3,'ok':True}),{'id':3,'ok':True})

    def test_truncated_or_oversized_stream_is_rejected(self):
        encoded=json.loads(wire.encode({'result':'a'*20000}))
        import base64
        compressed=base64.b64decode(encoded['payload'])
        encoded['payload']=base64.b64encode(compressed[:-4]).decode()
        with self.assertRaises(ValueError):
            wire.decode(encoded)
        original=wire.LIMIT
        try:
            wire.LIMIT=100
            with self.assertRaises(ValueError):
                wire.decode(
                    {'wire_encoding':'zlib-base64','payload':base64.b64encode(compressed).decode()})
        finally:
            wire.LIMIT=original


if __name__=='__main__':
    unittest.main()
