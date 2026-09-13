"""Synthetic graph contracts through the real native process and reference reader."""
import json
from pathlib import Path
import unittest
from scripts import memorycore_ai as bm
from scripts.graph_memory import recall
from scripts.knowledge_layer import Knowledge, SourceRoot, canonical, digest
from scripts.memory_mcp_lab import TOOLS, COMPACT_TOOL
from scripts.memory_packets import token_counter
from scripts.test_rust_native import NativeHarness
from scripts.test_rust_broker import BINARY, ROOT

@unittest.skipUnless(BINARY.is_file(),'build Rust release binary')
class GraphMemoryTests(unittest.TestCase):
    def setUp(self):
        self.h=NativeHarness(0)
        self.addCleanup(self.h.close)
        self.s=self.h.config['sessions'][0]
        self.s['allow_admin']=True
        self.k=Knowledge(self.h.conn,scope=self.s['scope'],sources=SourceRoot(self.s['source_root']),synthetic=True)
        self.count=token_counter()
        self.root=self.add('Release','Release requires the reviewed deployment procedure.')
        self.dependency=self.add('Procedure','Deploy only after approval; never bypass the review.')
        self.evidence=self.add('Evidence','The approval requirement follows the signed synthetic checklist.')
        self.conflict=self.add('Dispute','A draft suggests deployment before approval; this is disputed.')
        self.k.relate(self.root,self.dependency,'depends_on','Release requires the procedure.',reviewed=True)
        self.k.relate(self.dependency,self.evidence,'supported_by','Checklist supports the procedure.',reviewed=True)
        self.k.relate(self.root,self.conflict,'contradicts','The draft conflicts with the approval requirement.',reviewed=True)
        self.h.start('read')

    def add(self,name,summary):
        path=name.lower()+'.md'
        (Path(self.s['source_root'])/path).write_text('Fact: '+summary+'\n',encoding='utf-8')
        p=self.k.propose(path)['proposals'][0]
        return self.k.accept(p['id'],p['review_digest'])['memory_id']

    def graph(self,root=None,**kwargs):
        return self.h.tool(graph=root or self.root,**kwargs)

    def test_reference_parity_and_direction(self):
        for intent in ['related','dependencies','impact','evidence','conflicts']:
            with self.subTest(intent=intent):
                native=self.graph(intent=intent)
                expected=recall(self.k,self.root,intent=intent,count=self.count)
                self.assertEqual(native,expected)
        self.assertEqual({n['id'] for n in self.graph(intent='dependencies')['nodes']},{self.root,self.dependency})
        impact=self.graph(self.dependency,intent='impact')
        self.assertEqual({n['id'] for n in impact['nodes']},{self.root,self.dependency})
        edge=impact['edges'][0]
        self.assertEqual(impact['nodes'][edge['from']]['id'],self.root)
        self.assertEqual(impact['nodes'][edge['to']]['id'],self.dependency)

    def test_adjacency_indexes_preserve_read_only_legacy_results(self):
        intents=['related','dependencies','impact','evidence','conflicts']
        before={intent:self.graph(intent=intent) for intent in intents}
        self.assertEqual(self.h.conn.execute("SELECT count(*) FROM sqlite_master WHERE name IN ('knowledge_graph_owner','knowledge_graph_target')").fetchone()[0],0)
        for column in ('owner','target'):
            self.h.conn.execute(f"CREATE INDEX knowledge_graph_{column} ON knowledge_item(scope,{column},id) WHERE kind='relation'")
        self.h.conn.commit()
        self.assertEqual({intent:self.graph(intent=intent) for intent in intents},before)

    def test_depth_and_cycle_do_not_repeat_nodes(self):
        one=self.graph(depth='1')
        self.assertNotIn(self.evidence,[n['id'] for n in one['nodes']])
        self.k.relate(self.evidence,self.root,'depends_on','Synthetic cycle test.',reviewed=True)
        three=self.graph(depth='3')
        ids=[n['id'] for n in three['nodes']]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertIn(self.evidence,ids)
        self.assertFalse(three['inferred_truth'])

    def test_changed_source_is_removed_without_rebinding(self):
        (Path(self.s['source_root'])/'procedure.md').write_text('Fact: Changed synthetic procedure.',encoding='utf-8')
        graph=self.graph(intent='dependencies')
        self.assertEqual([n['id'] for n in graph['nodes']],[self.root])
        self.assertTrue(graph['omitted'])
        self.assertEqual(graph['edges'],[])

    def test_missing_and_unbound_roots_return_no_payload(self):
        (Path(self.s['source_root'])/'release.md').unlink()
        self.assertEqual(self.graph()['nodes'],[])
        args=bm.build_parser().parse_args(['remember','--scope',self.k.scope,'--type','semantic','--subject','Unbound','--summary','Synthetic unbound fact'])
        unbound=bm.remember(self.h.conn,args)['memory_id']
        self.assertEqual(self.graph(unbound)['nodes'],[])

    def test_foreign_and_disabled_sessions_cannot_read_graph(self):
        for session in ['chat-1','disabled']:
            self.h.send(session,'call',{'name':'memory','arguments':{'graph':self.root}})
            response=self.h.receive()
            self.assertNotIn('deployment',json.dumps(response))
            self.assertIn('error',response['result'])

    def test_invalid_options_and_multiple_actions_rejected(self):
        for args in [{'graph':self.root,'depth':'9'},{'graph':self.root,'intent':'all-projects'},
                     {'graph':self.root,'recall':'Release'},{'graph':self.root,'mode':'hybrid'}]:
            self.h.send('chat-0','call',{'name':'memory','arguments':args})
            response=self.h.receive()
            self.assertIn('error',response.get('result',response))

    def test_large_evidence_never_truncated_into_a_new_claim(self):
        other=self.add('Oversize','A synthetic dependency has a large evidence record.')
        self.k.relate(self.root,other,'depends_on','Preserve the full qualification. '*1000,reviewed=True)
        graph=self.graph(intent='dependencies')
        self.assertTrue(graph['truncated'])
        self.assertNotIn(other,[n['id'] for n in graph['nodes']])
        self.assertLessEqual(self.count(canonical(graph)),1100)
        for edge in graph['edges']:
            self.assertLess(edge['from'],len(graph['nodes']))
            self.assertLess(edge['to'],len(graph['nodes']))

    def test_compact_references_save_tokens_without_losing_edge_evidence(self):
        graph=self.graph(depth='3')
        expanded={**graph,'edges':[{**e,'from':graph['nodes'][e['from']], 'to':graph['nodes'][e['to']]} for e in graph['edges']]}
        self.assertLess(self.count(canonical(graph)),self.count(canonical(expanded)))
        self.assertTrue(all('confidence_reason' in n and 'valid_to' in n for n in graph['nodes']))
        self.assertTrue(any('never bypass' in n['summary'] for n in graph['nodes']))

    def test_native_catalogue_matches_reference(self):
        native=json.loads((ROOT/'rust-broker/src/native/catalogue.json').read_text(encoding='utf-8'))
        self.assertEqual(native['compact'],COMPACT_TOOL)
        self.assertEqual(native['named'],{key:list(value) for key,value in TOOLS.items()})

    def test_deleted_endpoint_is_not_traversed(self):
        import argparse
        bm.lifecycle(self.h.conn,argparse.Namespace(action='forget',scope=self.k.scope,memory_id=self.dependency))
        graph=self.graph(intent='dependencies')
        self.assertEqual([n['id'] for n in graph['nodes']],[self.root])
        self.assertTrue(graph['omitted'])

    def test_repeated_edges_preserve_evidence_but_not_duplicate_nodes(self):
        self.k.relate(self.root,self.dependency,'depends_on','A second reviewed evidence statement.',reviewed=True)
        graph=self.graph(intent='dependencies')
        self.assertEqual(len(graph['nodes']),2)
        self.assertEqual(len(graph['edges']),2)
        self.assertEqual(len({e['evidence'] for e in graph['edges']}),2)

    def test_corrupt_relation_rejected_without_payload(self):
        self.h.conn.execute("UPDATE knowledge_item SET checksum=? WHERE scope=? AND kind='relation'",('0'*64,self.k.scope))
        self.h.conn.commit()
        self.h.send('chat-0','call',{'name':'memory','arguments':{'graph':self.root}})
        result=self.h.receive()['result']
        self.assertIn('error',result)
        self.assertNotIn('deployment',json.dumps(result))

    def test_supersession_does_not_silently_transfer_reviewed_edges(self):
        (Path(self.s['source_root'])/'revised.md').write_text('Fact: Revised approval uses a different reviewed procedure.\n',encoding='utf-8')
        p=self.k.propose('revised.md')['proposals'][0]
        revised=self.k.accept(p['id'],p['review_digest'],supersedes=self.dependency)['memory_id']
        graph=self.graph(intent='dependencies')
        self.assertEqual([node['id'] for node in graph['nodes']],[self.root])
        self.assertNotIn(revised,canonical(graph))
        self.assertTrue(graph['omitted'])

    def test_intent_filter_avoids_spending_budget_on_other_relations(self):
        for i in range(70):
            self.k.relate(self.root,self.conflict,'contradicts',f'Synthetic unrelated evidence item {i}.',reviewed=True)
        graph=self.graph(intent='dependencies')
        self.assertEqual({node['id'] for node in graph['nodes']},{self.root,self.dependency})
        self.assertFalse(graph['truncated'])
        self.assertEqual(len(graph['edges']),1)

    def test_large_first_edge_does_not_hide_a_smaller_valid_path(self):
        other=self.add('Alternative','The alternate synthetic procedure preserves prior approval.')
        for forced,evidence in [('0'*31+'1','Reviewed evidence sentence. '*300),('f'*32,'Short reviewed statement.')]:
            edge=self.k.relate(self.root,other,'depends_on',evidence,reviewed=True)
            updated={**edge,'id':forced}
            self.h.conn.execute('UPDATE knowledge_item SET id=?,checksum=? WHERE id=?',(forced,digest(updated),edge['id']))
            self.h.conn.commit()
        graph=self.graph(intent='dependencies')
        self.assertIn(other,[node['id'] for node in graph['nodes']])
        self.assertTrue(graph['truncated'])
        self.assertTrue(any(edge['evidence']=='Short reviewed statement.' for edge in graph['edges']))
        self.assertEqual(graph,recall(self.k,self.root,intent='dependencies',count=self.count))
