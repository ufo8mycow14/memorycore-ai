"""Encrypted synthetic integration tests for derived vectors and atomic batches."""
import hashlib
import json
import time
import os
from pathlib import Path
import unittest

from scripts import test_rust_encryption as encryption_tests
from scripts.vector_transport import pack_vector


@unittest.skipUnless(os.environ.get("MEMORYCORE_AI_VECTOR_BINARY"), "set feature-enabled vector binary")
class VectorRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.fixture=encryption_tests.EncryptedNativeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.h=self.fixture.h
        self.command=self.fixture.command
        self.model="synthetic-axis-model-v1"
        self.command("vector-configure",{"model":self.model,"dimensions":64})
        self.ids=[]
        subjects=["Vehicle servicing","Fruit trees","Invoice approval"]
        summaries=["Fact: The automobile needs annual maintenance.",
                   "Fact: The orchard needs regular watering.",
                   "Fact: Invoice processing needs manager approval."]
        for i,(subject,summary) in enumerate(zip(subjects,summaries)):
            raw=summary.encode()
            path=Path(self.h.config["sessions"][0]["source_root"])/f"fact{i}.md"
            path.write_bytes(raw)
            saved=self.command("remember",{"type":"semantic","subject":subject,"summary":summary,
                "source":path.name,"source_hash":hashlib.sha256(raw).hexdigest()})
            self.ids.append(saved["memory_id"])
            self.command("bind-source",{"memory_id":self.ids[-1],"path":path.name,"sha256":hashlib.sha256(raw).hexdigest()})

    def index(self):
        jobs=self.command("vector-jobs")["jobs"]
        items=[]
        for job in jobs:
            vector=[0.0]*64
            vector[self.ids.index(job["id"])]=1.0
            items.append({"id":job["id"],"checksum":job["checksum"],"vector":vector})
        self.assertEqual(self.command("vector-put",{"model":self.model,"items":items})["stored"],3)
        return items

    def query(self):
        return self.command("vector-recall",{"query":"motorcar","model":self.model,"vector":[1.0]+[0.0]*63})

    def test_packed_vectors_preserve_recall_and_validate_frames_atomically(self):
        items=self.index()
        packed=[dict(item,vector=pack_vector(item["vector"])) for item in items]
        self.assertEqual(self.command("vector-put",{"model":self.model,"items":packed})["stored"],3)
        before=self.query()["memories"]
        result=self.command("vector-recall",{"query":"motorcar","model":self.model,
            "vector":pack_vector([1.0]+[0.0]*63)})
        self.assertEqual(result["memories"],before)
        for vector in ({"encoding":"unknown","data":packed[0]["vector"]["data"]},
                       {"encoding":"f32le-base64","data":"x"*344},
                       {"encoding":"f32le-base64","data":"A"*10000},
                       pack_vector([0.0]*64), pack_vector([1.0]*32)):
            self.command("vector-put",{"model":self.model,"items":[packed[1],dict(items[0],vector=vector)]},success=False)
        self.assertEqual(self.query()["memories"],before)

    def test_semantic_candidate_without_lexical_overlap_and_fallback(self):
        self.assertFalse(self.command("recall",{"query":"motorcar"})["memories"])
        self.assertEqual(self.query()["vector_state"],"lagging")
        self.index()
        result=self.query()
        self.assertEqual(result["vector_state"],"ready")
        self.assertEqual(result["memories"][0]["id"],self.ids[0])
        self.assertFalse(self.command("vector-recall",{"query":"motorcar"})["memories"])
        self.command("vector-recall",{"query":"motorcar","model":self.model,"vector":[0.0]*64},success=False)
        self.command("vector-recall",{"query":"motorcar","model":self.model,"vector":[1.0]*32},success=False)

    def test_unrelated_vector_abstains_instead_of_filling_nearest_results(self):
        self.index()
        vector=[0.0]*64
        vector[3]=1.0
        result=self.command("vector-recall",{"query":"distant astronomy","model":self.model,"vector":vector})
        self.assertEqual(result["memories"],[])
        self.assertEqual(result["relevance_filtered"],3)

    def test_weak_lexical_overlap_does_not_override_semantic_relevance_gate(self):
        self.index()
        vector=[0.0]*64
        vector[3]=1.0
        result=self.command("vector-recall",{"query":"automobile","model":self.model,"vector":vector})
        self.assertEqual(result["memories"],[])

    def test_purge_and_archive_cannot_be_undone_by_old_index_jobs(self):
        items=self.index()
        self.command("purge",{"memory_id":self.ids[0],"user_confirmed":True})
        old=next(i for i in items if i["id"]==self.ids[0])
        self.assertEqual(self.command("vector-put",{"model":self.model,"items":[old]})["stale"],1)
        self.assertNotIn(self.ids[0],[m["id"] for m in self.query()["memories"]])
        self.command("lifecycle",{"memory_id":self.ids[1],"action":"archive"})
        self.assertNotIn(self.ids[1],[m["id"] for m in self.query()["memories"]])

    def test_stale_sources_and_changed_versions_are_excluded(self):
        old=self.index()
        path=Path(self.h.config["sessions"][0]["source_root"])/"fact0.md"
        path.write_text("Fact: This source has been revised.",encoding="utf-8")
        self.assertNotIn(self.ids[0],[m["id"] for m in self.query()["memories"]])
        self.command("lifecycle",{"memory_id":self.ids[1],"action":"pin"})
        changed=next(i for i in old if i["id"]==self.ids[1])
        self.assertEqual(self.command("vector-put",{"model":self.model,"items":[changed]})["stale"],1)
        self.assertIn(self.ids[1],[j["id"] for j in self.command("vector-jobs")["jobs"]])

    def test_cache_reuse_and_scope_isolation(self):
        self.index()
        self.h.start()
        hits=[]
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            self.h.send("chat-0","admin",{"action":"vector-recall","arguments":{"query":"motorcar","model":self.model,"vector":[1.0]+[0.0]*63}})
            result=self.h.receive()["result"]
            hits.append(result["index_cache_hit"])
            if hits[-1]:
                self.assertEqual(result["memories"][0]["id"],self.ids[0])
                break
            self.assertEqual(result["vector_state"],"warming")
            time.sleep(.01)
        self.assertIn(True,hits)
        self.h.send("chat-1","admin",{"action":"vector-recall","arguments":{"query":"motorcar","model":self.model,"vector":[1.0]+[0.0]*63}})
        self.assertFalse(self.h.receive()["result"]["memories"])

    def test_model_change_requeues_and_rejects_old_vectors(self):
        old=self.index()
        self.command("vector-configure",{"model":"synthetic-axis-model-v2","dimensions":64})
        self.command("vector-put",{"model":self.model,"items":old},success=False)
        self.assertEqual(len(self.command("vector-jobs")["jobs"]),3)

    def test_claims_are_disjoint_and_stale_owners_cannot_commit(self):
        first=self.command("vector-claim")["jobs"]
        self.assertEqual(len(first),3)
        self.assertTrue(all(0<j["enqueued_ms"]<=j["claimed_ms"] for j in first))
        self.assertEqual(self.command("vector-claim")["jobs"],[])
        released=self.command("vector-release",{"items":[{"id":j["id"],"lease":j["lease"]} for j in first],"failed":False})
        self.assertEqual(released["released"],3)
        second=self.command("vector-claim")["jobs"]
        self.assertNotEqual(first[0]["lease"],second[0]["lease"])
        def items(jobs):
            return [{"id":j["id"],"checksum":j["checksum"],"lease":j["lease"],"vector":[1.0]+[0.0]*63} for j in jobs]
        stale=self.command("vector-put",{"model":self.model,"items":items(first)})
        self.assertEqual(stale["stale"],3)
        self.assertEqual(stale["indexing_enqueued_ms"],[])
        committed=self.command("vector-put",{"model":self.model,"items":items(second)})
        self.assertEqual(committed["stored"],3)
        self.assertEqual(committed["indexing_enqueued_ms"],[j["enqueued_ms"] for j in second])

    def test_multiple_background_claims_get_different_batches(self):
        for i in range(12):
            raw=f"Synthetic value {i}.".encode()
            path=Path(self.h.config["sessions"][0]["source_root"])/f"claim-{i}.md"
            path.write_bytes(raw)
            self.command("remember-bound",{"type":"semantic","subject":f"Claim batch {i}","summary":raw.decode(),"source":path.name,"source_hash":hashlib.sha256(raw).hexdigest()})
        first=self.command("vector-claim")["jobs"]
        second=self.command("vector-claim")["jobs"]
        self.assertEqual(len(first),8)
        self.assertEqual(len(second),7)
        self.assertFalse({j["id"] for j in first}&{j["id"] for j in second})

    def test_ineligible_jobs_do_not_consume_inference_and_binding_requeues(self):
        self.index()
        saved=self.command("remember",{"type":"semantic","subject":"Awaiting source","summary":"Synthetic source pending."})["memory_id"]
        self.assertEqual(self.command("vector-claim")["jobs"],[])
        self.assertEqual(self.command("vector-status")["pending"],0)
        path=Path(self.h.config["sessions"][0]["source_root"])/"pending.md"
        raw=b"Synthetic source pending."
        path.write_bytes(raw)
        self.command("bind-source",{"memory_id":saved,"path":path.name,"sha256":hashlib.sha256(raw).hexdigest()})
        jobs=self.command("vector-claim")["jobs"]
        self.assertEqual([j["id"] for j in jobs],[saved])

    def test_bound_remember_rolls_back_if_source_disagrees(self):
        self.index()
        self.command("remember-bound",{"type":"semantic","subject":"Rejected binding","summary":"Synthetic record.",
            "source":"missing.md","source_hash":"a"*64},success=False)
        self.assertEqual(self.command("vector-status")["pending"],0)
        self.assertFalse(self.command("recall",{"query":"Rejected binding"})["memories"])

    def test_repeated_binding_keeps_one_job_and_claim_rechecks_changed_source(self):
        self.index()
        path=Path(self.h.config["sessions"][0]["source_root"])/"queued-binding.md"
        raw=b"Fact: Synthetic deployment requires approved checks."
        digest=hashlib.sha256(raw).hexdigest()
        path.write_bytes(raw)
        saved=self.command("remember-bound",{"type":"semantic","subject":"Queued binding",
            "summary":raw.decode(),"source":path.name,"source_hash":digest})["memory_id"]
        self.command("bind-source",{"memory_id":saved,"path":path.name,"sha256":digest})
        self.assertEqual(self.command("vector-status")["pending"],1)
        path.write_bytes(b"Fact: This source changed before the indexing claim.")
        self.assertEqual(self.command("vector-claim")["jobs"],[])
        self.assertEqual(self.command("vector-status")["pending"],0)
        path.write_bytes(raw)
        self.command("bind-source",{"memory_id":saved,"path":path.name,"sha256":digest})
        self.assertEqual([job["id"] for job in self.command("vector-claim")["jobs"]],[saved])

    def test_expired_lease_cannot_commit_and_crashed_attempts_quarantine(self):
        jobs=self.command("vector-claim")["jobs"]
        time.sleep(15.05)
        items=[{key:j[key] for key in ("id","checksum","lease")} | {"vector":[1.0]+[0.0]*63} for j in jobs]
        self.assertEqual(self.command("vector-put",{"model":self.model,"items":items})["stale"],3)
        for attempt in range(4):
            jobs=self.command("vector-claim")["jobs"]
            self.assertEqual(len(jobs),3)
            self.command("vector-release",{"items":[{"id":j["id"],"lease":j["lease"]} for j in jobs],"failed":True})
            if attempt<3:
                time.sleep(2.05)
        self.assertEqual(self.command("vector-status")["quarantined"],3)
        self.assertEqual(self.command("vector-claim")["jobs"],[])
        self.assertEqual(self.command("vector-retry",{"ids":self.ids})["reset"],3)
        recovered=self.command("vector-claim")["jobs"]
        self.assertEqual(len(recovered),3)
        self.assertNotEqual(recovered[0]["lease"],jobs[0]["lease"])

    def test_job_age_and_budget_status_are_explicit(self):
        status=self.command("vector-status")
        self.assertGreaterEqual(status["oldest_pending_age_ms"],0)
        self.assertGreater(status["estimated_vector_capacity"],10_000)
        self.index()
        self.assertIsNone(self.command("vector-status")["oldest_pending_age_ms"])

    def test_incremental_cache_updates_and_deletes_without_full_rebuild(self):
        items=self.index()
        self.h.config["read_workers"]=1
        self.h.file.write_text(json.dumps(self.h.config),encoding="utf-8")
        self.h.start()
        def admin(action,args):
            self.h.send("chat-0","admin",{"action":action,"arguments":args})
            response=self.h.receive()
            self.assertNotIn("error",response)
            return response["result"]
        query={"query":"motorcar","model":self.model,"vector":[1.0]+[0.0]*63}
        self.assertFalse(admin("vector-recall",query)["index_cache_hit"])
        deadline=time.monotonic()+2
        while admin("vector-recall",query)["vector_state"]=="warming":
            self.assertLess(time.monotonic(),deadline)
            time.sleep(.01)
        self.assertTrue(admin("vector-recall",query)["index_cache_hit"])
        original=next(i for i in items if i["id"]==self.ids[0])
        admin("vector-put",{"model":self.model,"items":[original]})
        self.assertTrue(admin("vector-recall",query)["index_cache_hit"])
        changed=dict(original,vector=[0.0,0.0,0.0,1.0]+[0.0]*60)
        admin("vector-put",{"model":self.model,"items":[changed]})
        # Status only observes the cache; it must catch up without another recall.
        deadline=time.monotonic()+2
        while not (status:=admin("vector-status",{}))["cache"]["current"]:
            self.assertLess(time.monotonic(),deadline)
            time.sleep(.01)
        self.assertEqual(status["cache"]["cached_vectors"],len(items))
        result=admin("vector-recall",query)
        self.assertTrue(result["index_cache_hit"])
        self.assertEqual(result["index_delta_updates"],0)
        self.assertNotIn(self.ids[0],[r["id"] for r in result["memories"]])
        admin("vector-put",{"model":self.model,"items":[original]})
        result=admin("vector-recall",query)
        self.assertTrue(result["index_delta_updates"]==1 or result["index_cache_hit"])
        self.assertIn(self.ids[0],[r["id"] for r in result["memories"]])
        admin("purge",{"memory_id":self.ids[0],"user_confirmed":True})
        result=admin("vector-recall",query)
        self.assertTrue(result["index_delta_updates"]==1 or result["index_cache_hit"])
        self.assertNotIn(self.ids[0],[r["id"] for r in result["memories"]])

    def test_candidate_receipt_rejects_replay_and_rechecks_record_and_source(self):
        self.index()
        self.h.start()
        def call(meta,query="motorcar",session="chat-0"):
            self.h.send(session,"call",{"name":"memory","arguments":{"recall":query},"_meta":{"memory_embedding":meta}})
            return self.h.receive()["result"]
        deadline=time.monotonic()+2
        while True:
            candidates=call({"phase":"candidates","model":self.model,"vector":[1.0]+[0.0]*63,"reranker":"synthetic-reranker"})["memory_candidates"]
            if candidates["telemetry"]["vector_state"]!="warming":
                break
            self.assertLess(time.monotonic(),deadline)
            time.sleep(.01)
        selected={"phase":"select","selection":{k:candidates[k] for k in ("context","binding")} | {"scores":[5.0]*len(candidates["documents"])}}
        partial={"phase":"select","selection":{k:candidates[k] for k in ("context","binding")} | {"scores":[None]*len(candidates["documents"])}}
        self.assertNotIn("Vehicle",call(partial)["result"]["content"][0]["text"])
        result=call(selected)
        self.assertIn("Vehicle",result["result"]["content"][0]["text"])
        self.assertIn("error",call(selected,query="orchard"))
        self.assertIn("error",call(selected,session="chat-1"))
        path=Path(self.h.config["sessions"][0]["source_root"])/"fact0.md"
        path.write_text("Fact: Changed source",encoding="utf-8")
        self.assertNotIn("Vehicle",call(selected)["result"]["content"][0]["text"])
        self.h.send("chat-0","admin",{"action":"lifecycle","arguments":{"memory_id":self.ids[1],"action":"archive"}})
        self.assertNotIn("error",self.h.receive())
        self.assertNotIn("Fruit",call(selected)["result"]["content"][0]["text"])

    def test_stale_best_vector_cannot_raise_the_relevance_floor(self):
        items=self.index()
        second=next(i for i in items if i["id"]==self.ids[1])
        second["vector"]=[.7,.71414284285]+[0.0]*62
        self.command("vector-put",{"model":self.model,"items":[second]})
        path=Path(self.h.config["sessions"][0]["source_root"])/"fact0.md"
        path.write_text("Fact: Changed source",encoding="utf-8")
        result=self.query()
        self.assertIn(self.ids[1],[r["id"] for r in result["memories"]])

    def test_candidate_prefilter_removes_wrong_identifiers_and_missing_fields(self):
        self.index()
        expected=None
        for code in ("ZX-41", "ZX-42"):
            raw=f"Camera {code} is stored in bay 12. Taylor is responsible for it.".encode()
            path=Path(self.h.config["sessions"][0]["source_root"])/(code+".md")
            path.write_bytes(raw)
            saved=self.command("remember-bound",{"type":"semantic","subject":"Camera "+code,"summary":raw.decode(),
                "source":path.name,"source_hash":hashlib.sha256(raw).hexdigest()})["memory_id"]
            if code=="ZX-41":
                expected=saved
        jobs=self.command("vector-jobs")["jobs"]
        self.command("vector-put",{"model":self.model,"items":[{"id":j["id"],"checksum":j["checksum"],"vector":[1.0]+[0.0]*63} for j in jobs]})
        self.h.start()
        for query,wanted in [("Where is camera ZX-41?",[expected]),("Who manufactured camera ZX-41?",[]),
                             ("What is the purchase price of camera ZX-41?",[]),("When does the warranty on camera ZX-41 expire?",[])]:
            deadline=time.monotonic()+2
            while True:
                self.h.send("chat-0","call",{"name":"memory","arguments":{"recall":query},"_meta":{"memory_embedding":{
                    "phase":"candidates","model":self.model,"vector":[1.0]+[0.0]*63,"reranker":"synthetic-reranker"}}})
                candidates=self.h.receive()["result"]["memory_candidates"]
                if candidates["telemetry"]["vector_state"]!="warming":
                    break
                self.assertLess(time.monotonic(),deadline)
                time.sleep(.01)
            self.assertEqual([r["id"] for r in candidates["context"]["items"]],wanted,query)
            self.assertGreater(candidates["telemetry"]["answerability_filtered"],0)

    def test_focused_selection_keeps_same_subject_qualifications_and_compound_queries(self):
        indexed=self.index()
        invoice=next(item for item in indexed if item["id"]==self.ids[2])
        self.command("vector-put",{"model":self.model,"items":[dict(invoice,vector=[1.0]+[0.0]*63)]})
        raw=b"The automobile needs an additional inspection after towing."
        path=Path(self.h.config["sessions"][0]["source_root"])/"qualification.md"
        path.write_bytes(raw)
        qualifier=self.command("remember-bound",{"type":"semantic","subject":"Vehicle servicing","summary":raw.decode(),
            "source":path.name,"source_hash":hashlib.sha256(raw).hexdigest()})["memory_id"]
        pending=self.command("vector-jobs")["jobs"]
        self.command("vector-put",{"model":self.model,"items":[{"id":j["id"],"checksum":j["checksum"],"vector":[1.0]+[0.0]*63} for j in pending]})
        self.h.start()
        def call(query,meta):
            self.h.send("chat-0","call",{"name":"memory","arguments":{"recall":query},"_meta":{"memory_embedding":meta}})
            return self.h.receive()["result"]
        for query in ["vehicle servicing", "vehicle servicing and invoice approval"]:
            deadline=time.monotonic()+2
            while True:
                candidate=call(query,{"phase":"candidates","model":self.model,"vector":[1.0]+[0.0]*63,"reranker":"synthetic-reranker"})["memory_candidates"]
                if candidate["telemetry"]["vector_state"]!="warming":
                    break
                self.assertLess(time.monotonic(),deadline)
                time.sleep(.01)
            scores=[5.0 if item["id"]==self.ids[0] else 4.0 if item["id"]==qualifier else 3.0 for item in candidate["context"]["items"]]
            result=call(query,{"phase":"select","selection":{k:candidate[k] for k in ("context","binding")} | {"scores":scores}})
            packet=json.loads(result["result"]["content"][0]["text"])["packet"]
            self.assertIn(self.ids[0],packet)
            self.assertIn(qualifier,packet)
            self.assertIn("observed_at",packet)
            self.assertEqual(self.ids[2] in packet," and " in query)

    def test_weak_reranker_does_not_replace_stronger_semantic_topic(self):
        indexed=self.index()
        for index,cosine in ((0,.63),(2,.57)):
            item=next(item for item in indexed if item["id"]==self.ids[index])
            vector=[cosine,(1-cosine*cosine)**.5]+[0.0]*62
            self.command("vector-put",{"model":self.model,"items":[dict(item,vector=vector)]})
        self.h.start()
        def call(meta):
            self.h.send("chat-0","call",{"name":"memory","arguments":{"recall":"Annual vehicle maintenance policy"},
                "_meta":{"memory_embedding":meta}})
            return self.h.receive()["result"]
        deadline=time.monotonic()+2
        while True:
            candidate=call({"phase":"candidates","model":self.model,"vector":[1.0]+[0.0]*63,
                "reranker":"synthetic-reranker"})["memory_candidates"]
            if candidate["telemetry"]["vector_state"]!="warming":
                break
            self.assertLess(time.monotonic(),deadline)
            time.sleep(.01)
        self.assertIn(self.ids[0],[i["id"] for i in candidate["context"]["items"]])
        self.assertIn(self.ids[2],[i["id"] for i in candidate["context"]["items"]])
        scores=[-11.1 if i["id"]==self.ids[0] else -10.3 for i in candidate["context"]["items"]]
        result=call({"phase":"select","selection":{k:candidate[k] for k in ("context","binding")} | {"scores":scores}})
        packet=json.loads(result["result"]["content"][0]["text"])["packet"]
        self.assertIn(self.ids[0],packet)
        self.assertNotIn(self.ids[2],packet)

    def test_atomic_batch_and_vector_jobs_rollback_together(self):
        self.index()
        item={"action":"remember","arguments":{"type":"semantic","subject":"Batch fact","summary":"Synthetic batch data."}}
        bad={"action":"remember","arguments":{"type":"unsupported","subject":"Bad","summary":"Rejected synthetic data."}}
        self.command("batch",{"items":[item,bad]},success=False)
        self.assertFalse(self.command("vector-jobs")["jobs"])
        saved=self.command("batch",{"items":[item,{"action":"stage","arguments":{"text":"Synthetic staged batch data."}}]})
        self.assertTrue(saved["atomic"])
        self.assertFalse(saved["results"][0]["deduplicated"])
        self.assertEqual(len(self.command("vector-jobs")["jobs"]),1)
        self.command("batch",{"items":[item]*9},success=False)

    @unittest.skipUnless(os.environ.get("MEMORYCORE_AI_MODEL_CACHE"),"set local model cache")
    def test_local_embedding_pipeline_end_to_end(self):
        from scripts.vector_pipeline import LocalModel,BrokerClient,drain
        model=LocalModel(Path(os.environ["MEMORYCORE_AI_MODEL_CACHE"]))
        self.command("vector-configure",{"model":model.identity,"dimensions":384})
        client=BrokerClient(encryption_tests.BINARY,self.h.file,"chat-0",environment=self.fixture.env)
        self.addCleanup(client.close)
        result=drain(client,model)
        self.assertTrue(result["drained"])
        self.assertEqual(result["stored"],3)
        for query,expected in [("How should I look after my car?",self.ids[0]),
                               ("Who must authorise a bill?",self.ids[2]),
                               ("Keeping fruit plants hydrated",self.ids[1])]:
            deadline=time.monotonic()+2
            while True:
                result=client.call("vector-recall",{"query":query,"model":model.identity,"vector":model.query(query)})
                if result["vector_state"]!="warming":
                    break
                self.assertLess(time.monotonic(),deadline)
                time.sleep(.01)
            self.assertEqual(result["memories"][0]["id"],expected)


if __name__=="__main__":
    unittest.main()
