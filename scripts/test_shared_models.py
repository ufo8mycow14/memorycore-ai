"""Shared weights retain distinct concurrent roles and deterministic responses."""
import concurrent.futures
import os
import threading
import unittest
from scripts.shared_models import SharedModels
from scripts.model_pool import ModelPool
from scripts.resource_budget import ResourceBudget


class Engine:
    identity="synthetic-embedding"
    reranker_identity="synthetic-reranker"
    def __init__(self):
        self.process=self
        self.closed=False
        self.close_count=0
    def poll(self):
        return 0 if self.closed else None
    def query(self,value):
        if self.closed:
            raise RuntimeError("Synthetic process exited")
        return [1.0]
    def close(self):
        self.closed=True
        self.close_count+=1


class SharingTests(unittest.TestCase):
    def test_four_slot_runtime_packs_two_reserved_lanes_per_role(self):
        shared=SharedModels(Engine,max_lanes=4)
        self.addCleanup(shared.close)
        lanes=[shared.borrow(role) for role in ("foreground","foreground","background","background")]
        self.assertEqual(shared.state()["processes"],1)
        self.assertEqual(shared.state()["execution_lanes"],4)
        self.assertEqual(len({id(lane.engine) for lane in lanes}),1)
        extra=shared.borrow("foreground")
        self.assertEqual(shared.state()["processes"],2)
        for lane in lanes:
            lane.close()
        self.assertFalse(extra.engine.closed)
        extra.close()
        self.assertEqual(shared.state()["processes"],0)

    def test_two_pairs_share_two_processes_without_early_close(self):
        shared=SharedModels(Engine)
        self.addCleanup(shared.close)
        foreground=[shared.borrow("foreground") for _ in range(2)]
        background=[shared.borrow("background") for _ in range(2)]
        self.assertEqual(shared.state()["processes"],2)
        self.assertEqual(shared.state()["execution_lanes"],4)
        for front,back in zip(foreground,background):
            self.assertIs(front.engine,back.engine)
            front.close()
            self.assertFalse(back.engine.closed)
            back.close()
            self.assertTrue(back.engine.closed)

    def pools(self,factory=Engine):
        budget=ResourceBudget(snapshot_provider=lambda:{"total":32*1024**3,"available":24*1024**3,
            "rss":128*1024**2,"cpus":8,"system_cpu":0,"host_cpu":0},monitor=False)
        self.addCleanup(budget.close)
        shared=SharedModels(factory,budget=budget)
        self.addCleanup(shared.close)
        pools=[ModelPool(None,budget,role,monitor=False,shared=shared)
               for role in ("foreground","background")]
        for pool in pools:
            self.addCleanup(pool.close)
        return budget,shared,pools

    def test_shared_pairs_execute_two_of_each_role(self):
        entered=threading.Barrier(5)
        release=threading.Event()
        class Blocking(Engine):
            def query(self,value):
                entered.wait(timeout=3)
                release.wait(3)
                return [1.0]
        budget,shared,(front,back)=self.pools(Blocking)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            jobs=[executor.submit(pool.query,"q") for pool in (front,front,back,back)]
            try:
                entered.wait(timeout=3)
                self.assertEqual(budget.current()["active"],{"foreground":2,"background":2})
                self.assertEqual(shared.state()["processes"],2)
                self.assertEqual(budget.current()["resident_models"],2)
            finally:
                release.set()
            self.assertEqual([job.result(timeout=3) for job in jobs],[[1.0]]*4)

    def test_reuses_resident_weights_when_new_process_has_no_headroom(self):
        snapshot={"total":32*1024**3,"available":24*1024**3,"rss":128*1024**2,
                  "cpus":8,"system_cpu":0,"host_cpu":0}
        budget=ResourceBudget(snapshot_provider=lambda:snapshot,monitor=False)
        self.addCleanup(budget.close)
        shared=SharedModels(Engine,budget=budget)
        self.addCleanup(shared.close)
        front=ModelPool(None,budget,"foreground",monitor=False,shared=shared)
        self.addCleanup(front.close)
        snapshot["available"]=budget.current()["free_reserve_bytes"]+1
        budget.sample()
        self.assertFalse(budget.current()["can_grow"])
        back=ModelPool(None,budget,"background",monitor=False,shared=shared)
        self.addCleanup(back.close)
        self.assertTrue(front.state()["minimum_met"])
        self.assertTrue(back.state()["minimum_met"])
        self.assertEqual(shared.state()["processes"],2)
        self.assertEqual(budget.current()["resident_models"],2)
        self.assertEqual(back.query("synthetic"),[1.0])
        front.close()
        self.assertEqual(budget.current()["resident_models"],2)
        back.close()
        self.assertEqual(budget.current()["resident_models"],0)

    def test_budgeted_failed_load_releases_physical_reservation(self):
        budget=ResourceBudget(snapshot_provider=lambda:{"total":32*1024**3,"available":24*1024**3,
            "rss":128*1024**2,"cpus":8,"system_cpu":0,"host_cpu":0},monitor=False)
        self.addCleanup(budget.close)
        def fail():
            raise RuntimeError("Synthetic model load failed")
        shared=SharedModels(fail,budget=budget)
        self.addCleanup(shared.close)
        with self.assertRaisesRegex(RuntimeError,"model load failed"):
            shared.borrow("foreground")
        self.assertEqual(budget.current()["resident_models"],0)
        self.assertEqual(shared.state()["loading"],0)

    def test_dead_process_retires_both_idle_lanes_and_restores_pairs(self):
        budget,shared,pools=self.pools()
        failed=next(iter(shared.engines))
        failed.closed=True
        for pool in pools:
            pool.tick()
            self.assertTrue(pool.state()["minimum_met"])
            self.assertEqual(pool.state()["metrics"]["dead_idle_retired"],1)
            self.assertEqual(pool.query("q"),[1.0])
        self.assertEqual(shared.state()["processes"],2)
        self.assertEqual(shared.state()["execution_lanes"],4)
        self.assertNotIn(failed,shared.engines)
        self.assertEqual(failed.close_count,1)
        self.assertEqual(budget.current()["active"],{"foreground":0,"background":0})

    def test_failed_borrow_does_not_lose_surviving_runtime(self):
        shared=SharedModels(Engine)
        self.addCleanup(shared.close)
        lane=shared.borrow("foreground")
        def fail():
            raise RuntimeError("Synthetic load failure")
        shared.factory=fail
        with self.assertRaises(RuntimeError):
            shared.borrow("foreground")
        paired=shared.borrow("background")
        self.assertIs(paired.engine,lane.engine)
        shared.close()
        lane.close()
        paired.close()
        self.assertEqual(lane.engine.close_count,1)
        with self.assertRaises(ValueError):
            shared.borrow("foreground")

    def test_loading_does_not_block_status_or_duplicate_opposite_role_runtime(self):
        entered=threading.Event()
        release=threading.Event()
        def slow():
            entered.set()
            release.wait(3)
            return Engine()
        shared=SharedModels(slow)
        self.addCleanup(shared.close)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            first=executor.submit(shared.borrow,"foreground")
            self.assertTrue(entered.wait(1))
            second=executor.submit(shared.borrow,"background")
            try:
                status=executor.submit(shared.state).result(timeout=.2)
                self.assertEqual(status["loading"],1)
            finally:
                release.set()
            self.assertIs(first.result(timeout=2).engine,second.result(timeout=2).engine)

    def test_extra_lanes_use_resident_weights_before_loading_another_process(self):
        shared=SharedModels(Engine,max_lanes=4)
        self.addCleanup(shared.close)
        front=[shared.borrow("foreground") for _ in range(2)]
        back=[shared.borrow("background") for _ in range(2)]
        extra=[shared.borrow("foreground") for _ in range(2)]
        self.assertEqual(shared.state()["processes"],2)
        self.assertEqual(shared.state()["execution_lanes"],6)
        for lane in front+back:
            lane.close()
        self.assertFalse(any(lane.engine.closed for lane in extra))
        for lane in extra:
            lane.close()
        self.assertEqual(shared.state()["processes"],0)

    @unittest.skipUnless(os.environ.get("MEMORYCORE_AI_MODEL_CACHE"),"pinned local models required")
    def test_shared_onnx_query_passage_and_rerank_keep_identity(self):
        from scripts.memory_host import ModelProcess
        from scripts.resource_limits import InferenceLimits
        limits=InferenceLimits(50,2*1024**3)
        self.addCleanup(limits.close)
        engine=ModelProcess(os.environ["MEMORYCORE_AI_MODEL_CACHE"],dict(os.environ),threads=1,containment=limits,rerank=True,parallel_slots=4)
        self.addCleanup(engine.close)
        query="When does the synthetic backup run?"
        passages=["Synthetic backups run at midnight."]
        rerank={"query":query,"documents":passages}
        expected=[engine.query(query),engine.passages(passages),engine.rerank(rerank)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            for repeat in range(6):
                barrier=threading.Barrier(4)
                def run(kind):
                    barrier.wait(timeout=2)
                    return [engine.query,engine.passages,engine.rerank][kind]([query,passages,rerank][kind])
                kinds=tuple((repeat+i)%3 for i in range(4))
                jobs=[pool.submit(run,kind) for kind in kinds]
                for kind,job in zip(kinds,jobs):
                    self.assertEqual(job.result(timeout=5),expected[kind])
