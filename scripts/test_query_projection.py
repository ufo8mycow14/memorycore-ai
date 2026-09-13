"""The Rust and inference query projections share the same regression cases."""
import json
from pathlib import Path
import unittest
from scripts.query_projection import search_query
from scripts.memory_host import Embeddings


class QueryProjectionTests(unittest.TestCase):
    def test_shared_projection_contract_preserves_ambiguous_references(self):
        path=Path(__file__).resolve().parents[1]/"rust-broker/src/native/query_projection_cases.json"
        for query,expected in json.loads(path.read_text(encoding="utf-8")):
            with self.subTest(query=query):
                self.assertEqual(search_query(query),query if expected is None else expected)

    def test_tracking_labels_do_not_collapse_distinct_query_or_score_cache_keys(self):
        calls=[]
        class Model:
            identity="synthetic-projection-model"
            reranker_identity="synthetic-projection-reranker"
            def query(self,value):
                calls.append(("query",value))
                return [1.0]
            def rerank(self,value):
                calls.append(("rerank",value["query"]))
                return [2.0]*len(value["documents"])
        engine=Embeddings(Model(),workers=2)
        self.addCleanup(engine.close)
        for query in ("Request reference 1: Where is the pump?","Request reference 2: Where is the pump?"):
            self.assertEqual(engine.run("query",query,1,"scope"),[1.0])
            self.assertEqual(engine.run("rerank",{"query":query,"documents":["The pump is in bay 4."]},1,"scope",["id"]),[2.0])
        self.assertEqual(len(calls),4)
        self.assertEqual(engine.metrics["cache_hit"],0)
