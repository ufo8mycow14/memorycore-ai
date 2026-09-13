"""Equivalent-content baselines must preserve every graph field."""
import json
import unittest
import time
from scripts.benchmark_graph_memory import deduplicated_text
from scripts.test_rust_broker import Harness


class GraphBaselineTests(unittest.TestCase):
    def test_receive_timestamp_excludes_consumer_queue_delay(self):
        h=Harness(0,timestamp_responses=True)
        self.addCleanup(h.close)
        self.assertIn('_benchmark_received_at',h.start())
        h.send('chat-0','call',{'name':'memory','arguments':{'recall':'synthetic'}})
        deadline=time.monotonic()+3
        while h.responses.empty() and time.monotonic()<deadline:
            time.sleep(.001)
        queued_at=time.perf_counter()
        self.assertFalse(h.responses.empty())
        time.sleep(.03)
        response=h.receive()
        self.assertLessEqual(response['_benchmark_received_at'],queued_at)
        self.assertGreaterEqual(time.perf_counter()-response['_benchmark_received_at'],.03)

    def test_both_deduplicated_baselines_roundtrip(self):
        body={'nodes':[{'id':'one','summary':'Never skip approval.','sources':[{'hash':'abc'}]},
                       {'id':'two','summary':'A disputed statement.','confidence':0.4}],
              'edges':[{'from':0,'to':1,'relation':'contradicts','rationale':'Reviewed evidence.'}],
              'truncated':False,'intent':'conflicts','depth':2,'extra':{'retained':True}}
        for full_ids in (False,True):
            with self.subTest(full_ids=full_ids):
                rows=[json.loads(line) for line in deduplicated_text(body,full_ids).splitlines()]
                recovered=rows[0]['metadata']
                recovered['nodes']=[r['node'] for r in rows if 'node' in r]
                recovered['edges']=[r['edge'] for r in rows if 'edge' in r]
                if full_ids:
                    indexes={node['id']:i for i,node in enumerate(recovered['nodes'])}
                    for edge in recovered['edges']:
                        edge['from']=indexes[edge['from']]
                        edge['to']=indexes[edge['to']]
                self.assertEqual(recovered,body)

    def test_empty_graph_keeps_metadata(self):
        body={'nodes':[],'edges':[],'truncated':True,'reason':'deadline'}
        self.assertEqual(json.loads(deduplicated_text(body)),
                         {'metadata':{'truncated':True,'reason':'deadline'}})
