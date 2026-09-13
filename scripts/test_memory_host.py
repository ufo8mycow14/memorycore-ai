"""Standard memory calls are enhanced automatically without bypassing native policy."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import time
import unittest
import subprocess
import sys
import threading
import queue
from scripts import test_rust_encryption as fixtures
from scripts.memory_host import (MemoryHost, unique_object, Embeddings, QueryModels, ScopePolling,
                                 vector_write_batches, interactive_request_is_write,
                                 foreground_pool_sizes)
from scripts.model_pool import ModelPool
from scripts.resource_budget import ResourceBudget, sqlite_cache_budget


class SyntheticModel:
    identity="synthetic-host-384"
    reranker_identity="synthetic-reranker-v1"
    def query(self,text):
        return [1.0]+[0.0]*383
    def passages(self,texts):
        return [self.query(text) for text in texts]
    def rerank(self,value):
        return [5.0 for _ in value["documents"]]


class EmbeddingSchedulingTests(unittest.TestCase):
    def test_concurrent_diagnostics_do_not_mix_or_keep_previous_failure(self):
        from scripts.model_pool import ModelAdmissionTimeout
        barrier=threading.Barrier(2)
        class Model(SyntheticModel):
            def query(self,value):
                barrier.wait(2)
                if value=="failed":
                    raise ModelAdmissionTimeout({"cpu_pressure":True},0)
                return [1.0]
        embedding=Embeddings(Model(),workers=2)
        self.addCleanup(embedding.close)
        diagnostics=[{},{}]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            jobs=[executor.submit(embedding.run,"query",value,3,diagnostics=diagnostic)
                  for value,diagnostic in zip(("failed","good"),diagnostics)]
            self.assertEqual([job.result() for job in jobs],[None,[1.0]])
        self.assertEqual(diagnostics[0]["reason"],"admission_timeout")
        self.assertEqual(diagnostics[1],{"reason":"ready"})
        self.assertEqual(embedding.run("query","good",1,diagnostics=diagnostics[0]),[1.0])
        self.assertEqual(diagnostics[0],{"reason":"ready"})

    def test_admission_diagnostics_are_request_local_for_query_and_rerank(self):
        from scripts.model_pool import ModelAdmissionTimeout
        from unittest.mock import Mock
        model=SyntheticModel()
        state={"memory_pressure":True,"available_memory_bytes":123}
        model.query=Mock(side_effect=ModelAdmissionTimeout(state,2))
        model.rerank=Mock(side_effect=ModelAdmissionTimeout(state,1))
        embedding=Embeddings(model,workers=2)
        self.addCleanup(embedding.close)
        query={}
        rerank={}
        self.assertIsNone(embedding.run("query","synthetic",1,"a",diagnostics=query))
        self.assertEqual(embedding.run("rerank",{"query":"synthetic","documents":["fact"]},
                                       1,"b",diagnostics=rerank),[None])
        self.assertEqual(query,{"reason":"admission_timeout","idle_models":2,"resource_budget":state})
        self.assertEqual(rerank,{"reason":"admission_timeout","idle_models":1,"resource_budget":state})
        self.assertFalse(embedding.cache)
        model.query.side_effect=None
        model.query.return_value=[1.0]
        recovered={}
        self.assertEqual(embedding.run("query","recovered",1,"a",diagnostics=recovered),[1.0])
        self.assertEqual(recovered,{"reason":"ready"})
        self.assertEqual(query["reason"],"admission_timeout")

    def test_busy_diagnostics_do_not_release_running_permit(self):
        from unittest.mock import Mock
        entered=threading.Event()
        release=threading.Event()
        model=SyntheticModel()
        def blocked(_):
            entered.set()
            release.wait(3)
            return [1.0]
        model.query=Mock(side_effect=blocked)
        embedding=Embeddings(model)
        self.addCleanup(embedding.close)
        try:
            timed={}
            self.assertIsNone(embedding.run("query","first",.01,diagnostics=timed))
            self.assertTrue(entered.wait(1))
            self.assertEqual(timed["reason"],"inference_deadline")
            busy={}
            self.assertIsNone(embedding.run("query","second",1,diagnostics=busy))
            self.assertEqual(busy["reason"],"executor_busy")
            self.assertEqual(model.query.call_count,1)
        finally:
            release.set()

    def test_model_errors_do_not_expose_exception_payload(self):
        from unittest.mock import Mock
        model=SyntheticModel()
        model.rerank=Mock(side_effect=ValueError("private synthetic payload"))
        embedding=Embeddings(model)
        self.addCleanup(embedding.close)
        diagnostic={}
        self.assertEqual(embedding.run("rerank",{"query":"q","documents":["fact"]},
                                       1,diagnostics=diagnostic),[None])
        self.assertEqual(diagnostic,{"reason":"model_error"})

    def test_pressure_keeps_bounded_recall_cache_but_not_revoked_entries(self):
        from unittest.mock import Mock
        model=SyntheticModel()
        model.query=Mock(return_value=[1.0]*384)
        embedding=Embeddings(model,cache_bytes=4*1024**2)
        self.addCleanup(embedding.close)
        self.assertEqual(embedding.run("query","synthetic query",1,"scope"),[1.0]*384)
        deadline=time.monotonic()+1
        while embedding.inflight and time.monotonic()<deadline:
            time.sleep(.001)
        embedding.set_memory_pressure(True)
        model.query.side_effect=RuntimeError("Model unloaded under pressure")
        self.assertEqual(embedding.run("query","synthetic query",1,"scope"),[1.0]*384)
        model.query.assert_called_once()
        self.assertLessEqual(embedding.cached_bytes,1024**2)
        embedding.invalidate("scope")
        self.assertIsNone(embedding.run("query","synthetic query",1,"scope"))
        self.assertIsNone(embedding.run("query","synthetic query",1,"other-scope"))

    def test_pressure_cache_trims_to_one_mib_and_restores_normal_budget(self):
        embedding=Embeddings(SyntheticModel(),cache_bytes=4*1024**2,max_entries=2000)
        self.addCleanup(embedding.close)
        for n in range(1500):
            key=("model","scope","rerank",str(n))
            embedding.cache[key]=(1.0,)
            embedding.cache_records[key]=frozenset({str(n)})
            embedding.cached_bytes+=1056
        embedding.set_memory_pressure(True)
        self.assertLessEqual(embedding.cached_bytes,1024**2)
        self.assertTrue(embedding.cache)
        self.assertEqual(set(embedding.cache),set(embedding.cache_records))
        self.assertEqual(embedding.cached_bytes,1056*len(embedding.cache))
        self.assertGreater(embedding.metrics["pressure_evictions"],0)
        embedding.set_memory_pressure(False)
        self.assertEqual(embedding.cache_bytes,4*1024**2)

    def test_idle_retention_sleeps_to_next_deadline(self):
        from unittest.mock import Mock, patch
        host=MemoryHost.__new__(MemoryHost)
        host.stop=Mock()
        host.stop.is_set.side_effect=[False,False,True]
        host.sessions={"writer":{"id":"writer","scope":"a","allow_admin":True}}
        host.retention_ticks=host.retention_errors=0
        host.retention_inactive_scopes=set()
        host.maintenance=Mock()
        host.maintenance.admin.return_value={"removed":0,"pending":False,"project_state":"active"}
        with patch("scripts.memory_host.time.monotonic",return_value=100.0):
            host.retention_loop()
        host.stop.wait.assert_called_once_with(60.0)
        self.assertEqual(host.retention_ticks,1)

    def test_retention_runs_without_generation_models_or_quota_and_deduplicates_scopes(self):
        from unittest.mock import Mock
        host=MemoryHost.__new__(MemoryHost)
        host.stop=threading.Event()
        host.sessions={"writer":{"id":"writer","scope":"a","allow_admin":True,"generate_memories":False},
                       "second":{"id":"second","scope":"a","allow_admin":True},
                       "reader":{"id":"reader","scope":"b","allow_admin":False},
                       "other":{"id":"other","scope":"c","allow_admin":True}}
        host.retention_ticks=host.retention_errors=0
        host.retention_inactive_scopes=set()
        host.invalidate_scope=Mock()
        host.maintenance=Mock()
        def cleanup(session,action,args):
            self.assertEqual(action,"archive-cleanup")
            self.assertEqual(args,{})
            if session=="other":
                host.stop.set()
                raise RuntimeError("Synthetic maintenance failure")
            return {"removed":32,"pending":True,"project_state":"deleted"}
        host.maintenance.admin.side_effect=cleanup
        host.retention_loop()
        self.assertEqual([c.args[0] for c in host.maintenance.admin.call_args_list],["writer","other"])
        self.assertEqual((host.retention_ticks,host.retention_errors),(1,1))
        host.invalidate_scope.assert_called_once_with("a")
        self.assertEqual(host.retention_inactive_scopes,{"a"})

    def test_inflight_embedding_cannot_repopulate_purged_passage_cache(self):
        from collections import OrderedDict
        from unittest.mock import Mock
        from scripts.index_telemetry import IndexStages
        host=MemoryHost.__new__(MemoryHost)
        host.sessions={"session":{"scope":"scope"}}
        host.passage_lock=threading.Lock()
        host.passage_cache=OrderedDict()
        host.passage_generation={}
        host.embeddings=Mock()
        host.index_stages=IndexStages()
        host.background=Mock()
        def encode(texts):
            host.invalidate_scope("scope")
            return [[1.0]]
        host.background.passages.side_effect=encode
        result=host.encode_batch("session",{"model":"synthetic","jobs":[{"id":"one","text":"Synthetic fact"}]})
        self.assertEqual(result,[[1.0]])
        self.assertFalse(host.passage_cache)

    def test_active_index_scope_skips_counts_and_returns_to_idle_polling(self):
        from unittest.mock import Mock
        from scripts.index_telemetry import IndexStages
        host=MemoryHost.__new__(MemoryHost)
        host.embeddings=None
        clock=[0.0]
        host.stop=threading.Event()
        host.index_wake=Mock()
        host.scope_polling=ScopePolling(clock=lambda:clock[0])
        host.index_sessions=["busy","idle"]
        host.sessions={name:{"scope":name} for name in host.index_sessions}
        host.pending_batches={}
        host.finish_batches=Mock(side_effect=host.pending_batches.clear)
        host.configure_scopes=Mock()
        host.budget=Mock()
        host.budget.current.return_value={"pressured":False}
        host.background=Mock()
        host.background.identity="synthetic-index-model"
        host.background.state.return_value={"resident":1}
        host.index_executor=Mock()
        host.index_stages=IndexStages()
        host.index_metrics={"claims":0,"status_polls":0}
        host.index_errors=0
        calls=[]
        claims=iter(([{"id":"one"}],[{"id":"two"}],[]))
        def admin(session,action,args):
            calls.append((session,action))
            if action=="vector-status":
                # Quarantined work must not be sent to the claim path.
                if session=="idle":
                    return {"pending":2,"quarantined":2}
                return {"pending":0 if host.index_metrics["claims"]==3 else 16}
            self.assertEqual((session,action),("busy","vector-claim"))
            return {"model":host.background.identity,"jobs":next(claims)}
        host.maintenance=Mock()
        host.maintenance.admin.side_effect=admin
        host.submit_index_batch=Mock(side_effect=lambda session,jobs:host.pending_batches.update({session:jobs}))
        def wait(_):
            clock[0]+=.1
            if clock[0]>=.6:
                host.stop.set()
        host.index_wake.wait.side_effect=wait
        host.index_loop()
        self.assertEqual([action for session,action in calls if session=="busy"],
                         ["vector-status","vector-claim","vector-claim","vector-claim","vector-status"])
        self.assertTrue(any(session=="idle" for session,_ in calls))
        self.assertFalse(any(session=="idle" and action=="vector-claim" for session,action in calls))
        self.assertEqual(host.submit_index_batch.call_count,2)
        self.assertEqual(host.index_errors,0)
        host.index_executor.shutdown.assert_called_once_with(wait=True,cancel_futures=True)

    def test_interactive_write_counter_pauses_only_for_writes(self):
        host=MemoryHost.__new__(MemoryHost)
        host.interactive_lock=threading.Lock()
        host.interactive_inflight=0
        host.interactive_writes=0
        write=host._interactive_enter({"operation":"admin","arguments":{"action":"remember","arguments":{}}})
        read=host._interactive_enter({"operation":"call","arguments":{"name":"memory","arguments":{"recall":"synthetic"}}})
        self.assertTrue(write)
        self.assertFalse(read)
        self.assertTrue(host._interactive_busy())
        host._interactive_leave(read)
        self.assertTrue(host._interactive_busy())
        host._interactive_leave(write)
        self.assertFalse(host._interactive_busy())
        self.assertEqual(host.interactive_inflight,0)
        self.assertEqual(host.interactive_writes,0)

    def test_interactive_write_classification_matches_read_only_broker_actions(self):
        reads=[
            {"operation":"ping","arguments":{}},
            {"operation":"catalogue","arguments":{}},
            {"operation":"admin","arguments":{"action":"stats","arguments":{}}},
            {"operation":"admin","arguments":{"action":"prune","arguments":{"apply":False}}},
            {"operation":"call","arguments":{"name":"memory","arguments":{"recall":"synthetic"}}},
            {"operation":"call","arguments":{"name":"memory_recall","arguments":{"query":"synthetic"}}},
            {"operation":"routing-plan","arguments":{}},
        ]
        writes=[
            {"operation":"admin","arguments":{"action":"remember","arguments":{}}},
            {"operation":"admin","arguments":{"action":"archive-cleanup","arguments":{}}},
            {"operation":"admin","arguments":{"action":"prune","arguments":{"apply":True}}},
            {"operation":"admin","arguments":{"action":"prune"}},
            {"operation":"call","arguments":{"name":"memory","arguments":{"propose":"fixture.md"}}},
            {"operation":"call","arguments":{"name":"memory","arguments":{"forget":"id","review_digest":"digest"}}},
            {"operation":"routing-checkpoint","arguments":{}},
            {"operation":"background-propose","arguments":{}},
            {"operation":"future-operation","arguments":{}},
        ]
        self.assertTrue(all(not interactive_request_is_write(request) for request in reads))
        self.assertTrue(all(interactive_request_is_write(request) for request in writes))

    def test_foreground_pools_keep_write_waiters_separate_and_bounded(self):
        self.assertEqual(foreground_pool_sizes(1),(8,2))
        self.assertEqual(foreground_pool_sizes(4),(16,4))
        self.assertEqual(foreground_pool_sizes(8),(24,8))
        with self.assertRaises(ValueError):
            foreground_pool_sizes(0)
        with self.assertRaises(ValueError):
            foreground_pool_sizes(4.0)

    def test_index_completion_wakes_waiter_even_if_already_finished(self):
        from unittest.mock import Mock
        for completed in (False,True):
            for failed in (False,True):
                with self.subTest(completed=completed,failed=failed):
                    host=MemoryHost.__new__(MemoryHost)
                    host.index_executor=Mock()
                    host.pending_batches={}
                    host.index_wake=threading.Event()
                    host.encode_batch=Mock()
                    future=concurrent.futures.Future()
                    finish=lambda:future.set_exception(ValueError("synthetic failure")) if failed else future.set_result([])
                    host.index_executor.submit.return_value=future
                    if completed:
                        finish()
                    jobs={"jobs":[]}
                    host.submit_index_batch("scope-session",jobs)
                    self.assertEqual(host.pending_batches[future],("scope-session",jobs))
                    if not completed:
                        self.assertFalse(host.index_wake.is_set())
                        finish()
                    self.assertTrue(host.index_wake.is_set())
                    host.index_executor.submit.assert_called_once_with(host.encode_batch,"scope-session",jobs)

    def test_unreviewed_model_configuration_starts_no_resources_or_broker(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            config=Path(root)/"synthetic-host.json"
            config.write_text(json.dumps({"sessions":[]}),encoding="utf-8")
            for key in ("MEMORYCORE_AI_EMBEDDING_PROFILE","MEMORYCORE_AI_RERANKER"):
                with patch("scripts.memory_host.ResourceBudget") as budget, \
                     patch("scripts.memory_host.MultiplexBroker") as broker:
                    with self.assertRaisesRegex(ValueError,"Unreviewed host"):
                        MemoryHost("not-launched",config,root,environment={key:"unreviewed"})
                    budget.assert_not_called()
                    broker.assert_not_called()

    def test_rerank_deadline_is_reviewed_and_configurable(self):
        import tempfile
        from unittest.mock import patch
        config={"sessions":[]}
        with tempfile.TemporaryDirectory() as root:
            with patch("scripts.memory_host.MultiplexBroker"), \
                 patch("scripts.memory_host.VerificationJobs"), \
                 patch("scripts.memory_host.ResourceBudget") as budget, \
                 patch.object(MemoryHost,"configure_scopes"), \
                 patch.object(threading.Thread,"start"):
                budget.return_value.cpu_percent=50
                budget.return_value.current.return_value={
                    "memory_limit_bytes":512*1024**2,"worker_ceiling":1,
                    "inference_limit":1,"inference_cpu_percent":40}
                path=Path(root)/"host.json"
                path.write_text(json.dumps(config),encoding="utf-8")
                host=MemoryHost("not-launched",path,root,model=SyntheticModel(),
                                resource_policy={"rerank_deadline_seconds":.12})
                self.assertEqual(host.rerank_deadline_seconds,.12)
                with self.assertRaises(ValueError):
                    MemoryHost("not-launched",path,root,model=SyntheticModel(),
                               resource_policy={"rerank_deadline_seconds":.5})

    def test_sqlite_cache_budget_accounts_for_readers_and_warmers(self):
        for readers in (1,4,8):
            budget=sqlite_cache_budget(5*1024**3,readers)
            self.assertLessEqual(budget*(2*readers+2),5*1024**3//50)
            self.assertGreaterEqual(budget,2*1024**2)
            self.assertLessEqual(budget,16*1024**2)
        self.assertEqual(sqlite_cache_budget(0,8),2*1024**2)

    def test_cpu_pressure_can_return_cached_scores_without_waiting_on_missing_scores(self):
        from unittest.mock import Mock
        class Scorer(SyntheticModel):
            def __init__(self):
                self.calls=0
                self.budget=Mock()
                self.budget.current.return_value={"pressured":False}
            def rerank(self,value):
                self.calls+=1
                return super().rerank(value)
        model=Scorer()
        engine=Embeddings(model)
        self.addCleanup(engine.close)
        first={"query":"release rules","documents":["Approved releases need tests."]}
        self.assertEqual(engine.run("rerank",first,1,"scope",["a"]),[5.0])
        model.budget.current.return_value={"pressured":True}
        changed={"query":first["query"],"documents":first["documents"]+["Additional versioned evidence."]}
        self.assertEqual(engine.run("rerank",changed,1,"scope",["a","b"]),[5.0,None])
        self.assertEqual(model.calls,1)
        self.assertEqual(engine.metrics["pressure_partial"],1)
        model.budget.current.return_value={"pressured":False}
        self.assertEqual(engine.run("rerank",changed,1,"scope",["a","b"]),[5.0,5.0])
        self.assertEqual(model.calls,2)

    def test_idle_scope_backoff_and_racing_mutation_are_not_lost(self):
        clock=[10.0]
        polling=ScopePolling(clock=lambda:clock[0])
        ticket=polling.due("a")
        polling.defer("a",ticket)
        self.assertIsNone(polling.due("a"))
        self.assertEqual(polling.due("b"),0)
        clock[0]+=.5
        ticket=polling.due("a")
        polling.wake("a")
        polling.defer("a",ticket)
        self.assertIsNotNone(polling.due("a"))
        polling.defer("a",polling.due("a"))
        self.assertIsNone(polling.due("a"))
        polling.wake("b")
        self.assertIsNone(polling.due("a"))

    def test_vector_batches_fill_available_bytes_without_exceeding_native_frames(self):
        for dimensions in (64,384,1536):
            items=[{"id":str(i)*32,"checksum":"a"*64,"lease":"b"*32,
                    "vector":[-0.012345678901234567]*dimensions} for i in range(8)]
            batches=list(vector_write_batches(items))
            self.assertEqual([item for batch in batches for item in batch],items)
            for batch in batches:
                request={"session":"s"*128,"id":"i"*32,"operation":"admin",
                         "arguments":{"action":"vector-put","arguments":{"model":"m"*200,"items":batch}}}
                self.assertLessEqual(len(json.dumps(request,separators=(",",":"),allow_nan=False).encode())+1,65536)
                self.assertLessEqual(len(batch),8)
            if dimensions==64:
                self.assertEqual(len(batches),1)
            if dimensions==384:
                self.assertGreater(len(batches[0]),4)
        with self.assertRaises(ValueError):
            list(vector_write_batches([{"oversized":"x"*60001}]))

    def test_two_distinct_queries_run_concurrently_and_cache_results(self):
        class Blocking:
            identity="synthetic-blocking"
            def __init__(self):
                self.lock=threading.Lock()
                self.calls=0
                self.both=threading.Event()
                self.release=threading.Event()
            def query(self,text):
                with self.lock:
                    self.calls+=1
                    if self.calls==2:
                        self.both.set()
                self.release.wait(3)
                return [1.0,0.0]
        model=Blocking()
        embeddings=Embeddings(model,workers=2)
        self.addCleanup(embeddings.close)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            one=pool.submit(embeddings.run,"query","one",2)
            two=pool.submit(embeddings.run,"query","two",2)
            try:
                self.assertTrue(model.both.wait(1))
                self.assertIsNone(embeddings.run("query","third",.01))
            finally:
                model.release.set()
            self.assertEqual(one.result(),[1.0,0.0])
            self.assertEqual(two.result(),[1.0,0.0])
        self.assertEqual(embeddings.run("query","one",1),[1.0,0.0])
        self.assertEqual(model.calls,2)

    def test_failed_query_worker_is_retired_while_healthy_worker_remains(self):
        class Failed:
            identity="synthetic-host-384"
            def query(self,value):
                raise ValueError("synthetic failure")
        snapshot={"total":32*1024**3,"available":24*1024**3,"rss":0,"cpus":16,"system_cpu":0,"host_cpu":0}
        budget=ResourceBudget(snapshot_provider=lambda:snapshot,monitor=False)
        self.addCleanup(budget.close)
        models=iter([Failed(),SyntheticModel()])
        pool=ModelPool(lambda:next(models),budget,"foreground",monitor=False)
        self.addCleanup(pool.close)
        with self.assertRaises(ValueError):
            pool.query("one")
        self.assertEqual(pool.query("two"),[1.0]+[0.0]*383)
        self.assertEqual(pool.state()["resident"],1)

    def test_query_cache_is_bounded_and_keyed_by_model(self):
        embeddings=Embeddings(SyntheticModel())
        self.addCleanup(embeddings.close)
        for n in range(260):
            deadline=time.perf_counter()+1
            while embeddings.run("query",str(n),1) is None:
                self.assertLess(time.perf_counter(),deadline)
        embeddings.pool.shutdown(wait=True)
        self.assertEqual(len(embeddings.cache),256)
        self.assertTrue(all(len(key)==4 for key in embeddings.cache))

    def test_reranker_cache_partitions_scope_query_and_document_version(self):
        class Scorer(SyntheticModel):
            calls=0
            def rerank(self,value):
                self.calls+=1
                return super().rerank(value)
        model=Scorer()
        embeddings=Embeddings(model)
        self.addCleanup(embeddings.close)
        value={"query":"q","documents":["original"]}
        self.assertEqual(embeddings.run("rerank",value,1,"scope-a"),[5.0])
        self.assertEqual(embeddings.run("rerank",value,1,"scope-a"),[5.0])
        self.assertEqual(embeddings.run("rerank",value,1,"scope-b"),[5.0])
        self.assertEqual(embeddings.run("rerank",dict(value,documents=["changed"]),1,"scope-a"),[5.0])
        self.assertEqual(model.calls,3)

    def test_byte_budget_evicts_and_invalidation_blocks_late_cache_publish(self):
        model=SyntheticModel()
        embeddings=Embeddings(model,cache_bytes=15000,max_entries=4096)
        self.addCleanup(embeddings.close)
        for n in range(4):
            self.assertIsNotNone(embeddings.run("query",str(n),1,"a"))
        self.assertLessEqual(embeddings.cached_bytes,15000)
        self.assertEqual(len(embeddings.cache),1)
        embeddings.invalidate("a")
        self.assertEqual(embeddings.cached_bytes,0)
        started=threading.Event()
        release=threading.Event()
        def slow(text):
            started.set()
            release.wait(2)
            return [1.0]
        model.query=slow
        self.assertIsNone(embeddings.run("query","slow",.01,"a"))
        self.assertTrue(started.is_set())
        embeddings.invalidate("a")
        release.set()
        embeddings.pool.shutdown(wait=True)
        self.assertFalse(embeddings.cache)

    def test_candidate_churn_reuses_individual_scores_and_keeps_order(self):
        class Scorer(SyntheticModel):
            def __init__(self):
                self.batches=[]
            def rerank(self,value):
                self.batches.append(value["documents"])
                return [float(len(d)) for d in value["documents"]]
        model=Scorer()
        embeddings=Embeddings(model)
        self.addCleanup(embeddings.close)
        def score(documents,ids):
            return embeddings.run("rerank",{"query":"q","documents":documents},1,"scope-a",ids)
        self.assertEqual(score(["a","bb"],["1","2"]),[1.0,2.0])
        self.assertEqual(score(["ccc","bb","a"],["3","2","1"]),[3.0,2.0,1.0])
        self.assertEqual(model.batches,[["a","bb"],["ccc"]])
        embeddings.invalidate("scope-a",{"2"},methods={"rerank"})
        self.assertEqual(score(["a","bb"],["1","2"]),[1.0,2.0])
        self.assertEqual(model.batches[-1],["bb"])
        self.assertEqual(embeddings.cached_bytes,1056*3)

    def test_partial_rerank_keeps_verified_scores_during_busy_and_deadline(self):
        release=threading.Event()
        model=SyntheticModel()
        embeddings=Embeddings(model,workers=1)
        self.addCleanup(embeddings.close)
        value={"query":"q","documents":["cached"]}
        self.assertEqual(embeddings.run("rerank",value,1,"scope",["1"]),[5.0])
        extended=dict(value,documents=["cached","pending"])
        self.assertTrue(embeddings.capacity.acquire(timeout=1))
        try:
            self.assertEqual(embeddings.run("rerank",extended,.01,"scope",["1","2"]),[5.0,None])
        finally:
            embeddings.capacity.release()
        def slow(value):
            release.wait(2)
            return [3.0]*len(value["documents"])
        model.rerank=slow
        try:
            self.assertEqual(embeddings.run("rerank",extended,.01,"scope",["1","2"]),[5.0,None])
            self.assertFalse(embeddings.capacity.acquire(blocking=False))
        finally:
            release.set()
        embeddings.pool.shutdown(wait=True)
        self.assertEqual(embeddings.run("rerank",extended,1,"scope",["1","2"]),[5.0,3.0])

    def test_overlapping_reranks_share_inflight_scores_and_revoke_per_record(self):
        started=threading.Event()
        release=threading.Event()
        model=SyntheticModel()
        batches=[]
        def slow(value):
            batches.append(value["documents"])
            started.set()
            release.wait(2)
            return [5.0]*len(value["documents"])
        model.rerank=slow
        embeddings=Embeddings(model,workers=2)
        self.addCleanup(embeddings.close)
        value={"query":"q","documents":["a","b"]}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(embeddings.run,"rerank",value,1,"scope-a",["1","2"])
            self.assertTrue(started.wait(1))
            second=pool.submit(embeddings.run,"rerank",dict(value,documents=["b"]),1,"scope-a",["2"])
            embeddings.invalidate("scope-a",{"1"},methods={"rerank"})
            release.set()
            self.assertEqual(first.result(timeout=2),[None,5.0])
            self.assertEqual(second.result(timeout=2),[5.0])
        self.assertEqual(batches,[["a","b"]])
        self.assertEqual(len(embeddings.cache),1)
        self.assertEqual(embeddings.cached_bytes,1056)


@unittest.skipUnless(os.environ.get("MEMORYCORE_AI_VECTOR_BINARY"),"native vector build required")
class MemoryHostTests(unittest.TestCase):
    def test_host_deletes_project_data_with_generation_disabled(self):
        for session in self.h.config["sessions"]:
            session["generate_memories"]=False
        self.h.file.write_text(json.dumps(self.h.config),encoding="utf-8")
        self.fixture.command("project-event",{"state":"deleted","version":1})
        host=self.host()
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            status=host.broker.admin("chat-0","archive-retention-status",{})
            if status["project"][2]:
                break
            time.sleep(.05)
        self.assertTrue(status["project"][2],status)
        self.assertGreater(host.retention_ticks,0)
        self.assertEqual(host.retention_errors,0)
        self.assertTrue(host.broker.admin("chat-0","verify",{})["verified"])
        rejected=host.exchange({"session":"chat-0","id":"deleted-project","operation":"call",
                               "arguments":{"name":"memory","arguments":{"recall":"motorcar"}}})
        self.assertIn("error",rejected)

    def setUp(self):
        self.fixture=fixtures.EncryptedNativeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.h=self.fixture.h
        raw=b"Fact: The automobile needs annual maintenance."
        path=Path(self.h.config["sessions"][0]["source_root"])/"service.md"
        path.write_bytes(raw)
        digest=hashlib.sha256(raw).hexdigest()
        self.saved=self.fixture.command("remember",{"type":"semantic","subject":"Automobile servicing",
            "summary":raw.decode(),"source":path.name,"source_hash":digest})["memory_id"]
        self.fixture.command("bind-source",{"memory_id":self.saved,"path":path.name,"sha256":digest})

    def host(self,real=False):
        policy=None if real else {"snapshot_provider":lambda:{"total":32*1024**3,"available":24*1024**3,
            "rss":128*1024**2,"cpus":8,"system_cpu":0,"host_cpu":0},"monitor":False}
        host=MemoryHost(self.h.binary,self.h.file,os.environ.get("MEMORYCORE_AI_MODEL_CACHE","missing-model"),
                        environment=self.fixture.env,model=None if real else SyntheticModel(),resource_policy=policy)
        self.addCleanup(host.close)
        return host

    def recall(self,host,query="motorcar",session="chat-0"):
        response=host.exchange({"session":session,"id":"recall","operation":"call",
            "arguments":{"name":"memory","arguments":{"recall":query}}})
        if "error" in response.get("result",{}):
            return response["result"]
        self.last_telemetry=response.get("semantic_host",{})
        return json.loads(response["result"]["result"]["content"][0]["text"])

    def wait_index(self,host,warm=True):
        deadline=time.perf_counter()+10
        while time.perf_counter()<deadline:
            if not host.broker.admin("chat-0","vector-status",{})["configured"]:
                time.sleep(.05)
                continue
            status=host.broker.admin("chat-0","vector-jobs",{})
            if not status["jobs"]:
                if warm:
                    ready=0
                    while ready<self.h.config["read_workers"] and time.perf_counter()<deadline:
                        result=host.broker.admin("chat-0","vector-recall",{"query":"synthetic readiness probe","model":host.embeddings.model.identity,"vector":[1.0]+[0.0]*383})
                        ready=ready+1 if result["vector_state"]=="ready" else 0
                        time.sleep(.01)
                    self.assertEqual(ready,self.h.config["read_workers"])
                return
            time.sleep(.05)
        self.fail("Automatic indexing did not complete: "+json.dumps({
            "budget":host.budget.current(),"foreground":host.embeddings.model.state(),
            "background":host.background.state(),"index_errors":host.index_errors}))

    def test_normal_memory_call_automatically_indexes_and_uses_vectors(self):
        host=self.host()
        self.assertEqual(host.containment.state()["cpu_percent"],host.budget.current()["inference_cpu_percent"])
        self.wait_index(host)
        result=self.recall(host)
        self.assertIn("Automobile",result["packet"])
        self.assertEqual(self.last_telemetry["retrieval"]["vector_state"],"ready")
        self.assertEqual(result["mode"],"verified_hybrid")
        self.assertNotIn("Automobile",self.recall(host,session="chat-1")["packet"])
        host.broker.admin("chat-0","lifecycle",{"memory_id":self.saved,"action":"archive"})
        self.assertNotIn("Automobile",self.recall(host)["packet"])

    def test_busy_embeddings_fall_back_and_disabled_memory_stays_disabled(self):
        host=self.host()
        self.wait_index(host)
        host.embeddings.capacity.acquire()
        try:
            start=time.perf_counter()
            result=self.recall(host,"automobile")
            self.assertLess(time.perf_counter()-start,2)
            self.assertIn("Automobile",result["packet"])
            self.assertEqual(self.last_telemetry["retrieval"]["vector_state"],"unavailable")
        finally:
            host.embeddings.capacity.release()
        self.assertIn("error",self.recall(host,session="disabled"))

    def test_ten_concurrent_requests_keep_response_identity(self):
        host=self.host()
        def call(i):
            return host.exchange({"session":f"chat-{i}","id":str(i),"operation":"ping","arguments":{}})
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            responses=list(pool.map(call,range(10)))
        for i,response in enumerate(responses):
            self.assertEqual(response["session"],f"chat-{i}")
            self.assertEqual(response["id"],str(i))
            self.assertNotIn("error",response)

    @unittest.skipUnless(os.environ.get("MEMORYCORE_AI_MODEL_CACHE"),"local model required")
    def test_real_model_process_enhances_ordinary_memory_call(self):
        host=self.host(real=True)
        self.assertIsNotNone(host.embeddings)
        self.wait_index(host)
        result=self.recall(host,"How should I look after my car?")
        self.assertIn("Automobile",result["packet"])
        self.assertEqual(self.last_telemetry["retrieval"]["vector_state"],"ready")
        self.assertEqual(self.last_telemetry["retrieval"]["reranker_state"],"ready")

    def test_duplicate_host_json_is_rejected(self):
        with self.assertRaises(ValueError):
            json.loads('{"arguments":{"recall":"one","recall":"two"}}',object_pairs_hook=unique_object)

    def test_ordinary_readonly_session_uses_semantic_path_without_admin(self):
        session=dict(self.h.config["sessions"][0],id="reader",allow_admin=False,generate_memories=False)
        self.h.config["sessions"].append(session)
        self.h.file.write_text(json.dumps(self.h.config),encoding="utf-8")
        host=self.host()
        self.wait_index(host)
        self.assertIn("Automobile",self.recall(host,session="reader")["packet"])

    def test_cli_missing_model_degrades_and_preserves_duplicate_key_rejection(self):
        request={"session":"chat-0","id":"normal","operation":"call",
            "arguments":{"name":"memory","arguments":{"recall":"automobile"}}}
        raw=json.dumps(request)+'\n'+ '{"session":"chat-0","id":"bad","id":"duplicate","operation":"ping","arguments":{}}\n'
        result=subprocess.run([sys.executable,"-B","-m","scripts.memory_host","--binary",str(self.h.binary),
            "--config",str(self.h.file),"--cache",str(self.h.root/"missing-cache")],input=raw.encode(),
            capture_output=True,env=self.fixture.env,timeout=60)
        self.assertEqual(result.returncode,0,result.stderr)
        responses=[json.loads(line) for line in result.stdout.splitlines()]
        self.assertFalse(responses[0]["embedding_model_loaded"])
        self.assertIn({"error":"invalid_request"},responses)
        response=next(r for r in responses if r.get("id")=="normal")
        body=json.loads(response["result"]["result"]["content"][0]["text"])
        self.assertIn("Automobile",body["packet"])
        self.assertEqual(response["semantic_host"]["retrieval"]["vector_state"],"unavailable")

    def test_secret_query_is_rejected_before_local_inference(self):
        host=self.host()
        self.wait_index(host)
        before=dict(host.embeddings.metrics)
        result=self.recall(host,"api_key=synthetic-secret-value")
        self.assertIn("error",result)
        self.assertEqual(host.embeddings.metrics,before)

    def test_cold_recall_is_bounded_and_recovers_without_restart(self):
        host=self.host()
        self.wait_index(host,warm=False)
        start=time.perf_counter()
        self.recall(host)
        self.assertLess(time.perf_counter()-start,1)
        self.assertIn(self.last_telemetry["retrieval"]["vector_state"],{"ready","warming"})
        self.wait_index(host)
        self.assertIn("Automobile",self.recall(host)["packet"])
