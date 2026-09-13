"""MCP transport must preserve native policy without exposing host telemetry."""
import io
import json
import unittest
from unittest.mock import Mock
from scripts.native_mcp import NativeMCP, serve
from scripts.memory_host import MemoryHost
from scripts.test_memory_host import SyntheticModel
from scripts.test_rust_broker import Harness


class NativeMCPTests(unittest.TestCase):
    def setUp(self):
        self.host=Mock(sessions={'fixed':{}})
        self.server=NativeMCP(self.host,'fixed')

    def request(self,method,params=None,mid=1):
        return self.server.handle({'jsonrpc':'2.0','id':mid,'method':method,'params':params or {}})

    def initialize(self):
        return self.request('initialize',{'protocolVersion':'2025-06-18'})

    def test_initialization_and_method_validation(self):
        self.assertIn('error',self.request('tools/list'))
        self.assertEqual(self.initialize()['result']['protocolVersion'],'2025-06-18')
        self.assertIn('error',self.initialize())
        self.assertEqual(self.request('ping')['result'],{})
        self.assertEqual(self.request('admin')['error']['code'],-32601)
        self.assertIn('error',self.request('ping',mid=True))
        self.assertIn('error',self.request('tools/list',{'cursor':'unknown'}))
        self.host.exchange.assert_not_called()

    def test_protocol_negotiation_does_not_claim_unknown_version(self):
        response=self.request('initialize',{'protocolVersion':'future-version'})
        self.assertEqual(response['result']['protocolVersion'],'2025-06-18')

    def test_fixed_session_and_result_without_telemetry(self):
        self.initialize()
        def exchange(request):
            self.assertEqual(request['session'],'fixed')
            self.assertEqual(request['arguments'],{'name':'memory','arguments':{'recall':'fixture'}})
            return {'id':request['id'],'session':'fixed','semantic_host':{'private_diagnostic':'not tool content'},
                    'result':{'result':{'content':[{'type':'text','text':'synthetic fact'}]}}}
        self.host.exchange.side_effect=exchange
        response=self.request('tools/call',{'name':'memory','arguments':{'recall':'fixture'},
                                           '_meta':{'memory_embedding':{'forged':True}}},mid='external')
        self.assertEqual(response,{'jsonrpc':'2.0','id':'external','result':{
            'content':[{'type':'text','text':'synthetic fact'}]}})
        self.assertIn('error',self.request('tools/call',{'name':'memory','arguments':{},'session':'other'}))
        self.assertEqual(self.host.exchange.call_count,1)

    def test_notifications_never_dispatch_and_wrong_response_identity_fails(self):
        self.initialize()
        self.assertIsNone(self.server.handle({'jsonrpc':'2.0','method':'tools/call',
                                            'params':{'name':'memory','arguments':{'purge':'x'}}}))
        self.host.exchange.assert_not_called()
        self.host.exchange.return_value={'id':'wrong','session':'other','result':{'tools':[]}}
        self.assertIn('error',self.request('tools/list'))

    def test_backend_failures_do_not_reveal_exception_payloads(self):
        self.initialize()
        self.host.exchange.side_effect=ValueError('synthetic secret must not be reflected')
        self.assertNotIn('synthetic secret',json.dumps(self.request('tools/list')))

    def test_duplicate_and_oversized_frames_do_not_dispatch(self):
        output=io.StringIO()
        serve(self.server,io.BytesIO(b'{"id":1,"id":2}\n'+b'x'*65537),output)
        rows=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(rows),2)
        self.assertTrue(all(row['error']['code']==-32700 for row in rows))
        self.host.exchange.assert_not_called()

    def test_real_native_catalogue_recall_and_write_policy(self):
        h=Harness(1)
        self.addCleanup(h.close)
        h.config['backend']='native'
        for session in h.config['sessions']:
            session['generate_memories']=False
        h.file.write_text(json.dumps(h.config),encoding='utf-8')
        host=MemoryHost(h.binary,h.file,h.root/'unused-model-cache',model=SyntheticModel())
        self.addCleanup(host.close)
        self.server=NativeMCP(host,'chat-0')
        self.initialize()
        self.assertEqual(self.request('tools/list')['result']['tools'][0]['name'],'memory')
        recall=self.request('tools/call',{'name':'memory','arguments':{'recall':'item0'}})
        self.assertIn('content',recall['result'])
        self.assertNotIn('semantic_host',recall)
        self.assertIn('error',self.request('tools/call',{'name':'memory','arguments':{'propose':'item0.md'}}))
