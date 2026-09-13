"""Deterministic resource pressure, recovery and adaptive capacity contracts."""
import concurrent.futures
import threading
import unittest
from unittest.mock import Mock, patch
from scripts.model_pool import ModelPool
from scripts.resource_budget import ResourceBudget, evaluate


class Model:
    identity="fixture-model"
    def __init__(self):
        self.closed=False
    def query(self, value):
        return [1.0]
    def passages(self, values):
        return [[1.0] for _ in values]
    def close(self):
        self.closed=True


class ResourceTests(unittest.TestCase):
    def test_admission_exception_preserves_failure_snapshot(self):
        from scripts.model_pool import ModelAdmissionTimeout
        pool=self.pool()
        self.snapshot["available"]=100*1024**2
        self.budget.sample()
        with self.assertRaises(ModelAdmissionTimeout) as failure:
            pool.query("synthetic")
        self.snapshot["available"]=24*1024**3
        self.budget.sample()
        self.assertTrue(failure.exception.diagnostic["resource_budget"]["memory_pressure"])
        self.assertFalse(self.budget.current()["memory_pressure"])
        self.assertEqual(self.budget.current()["waiting"],{"foreground":0,"background":0})
        self.assertEqual(pool.query("recovered"),[1.0])

    def test_telemetry_explains_reload_deadband_without_relaxing_reserve(self):
        reserve=int(self.snapshot["total"]*.15)
        self.snapshot["available"]=reserve+self.budget.model_bytes-1
        self.budget.sample()
        state=self.budget.current()
        self.assertFalse(state["memory_pressure"])
        self.assertFalse(state["can_grow"])
        self.assertIn("system_free_headroom",state["model_growth_blockers"])
        self.assertEqual(state["available_memory_bytes"],self.snapshot["available"])
        self.assertIsNone(self.budget.reserve_model())
        self.snapshot["available"]+=1
        self.budget.sample()
        self.assertNotIn("system_free_headroom",self.budget.current()["model_growth_blockers"])
        self.assertIsNotNone(self.budget.reserve_model())

    def setUp(self):
        self.snapshot={"total":32*1024**3,"available":24*1024**3,"rss":128*1024**2,
                       "cpus":16,"system_cpu":0,"host_cpu":0}
        self.budget=ResourceBudget(snapshot_provider=lambda:self.snapshot,monitor=False)
        self.addCleanup(self.budget.close)

    def pool(self, role="foreground", factory=Model, **kwargs):
        pool=ModelPool(factory,self.budget,role,monitor=False,**kwargs)
        self.addCleanup(pool.close)
        return pool

    def test_memory_refresh_does_not_shorten_cpu_sampling_window(self):
        process=Mock()
        process.pid=123
        process.create_time.return_value=1
        process.children.return_value=[]
        process.cpu_affinity.return_value=list(range(8))
        process.memory_info.return_value=Mock(rss=128*1024**2,private=128*1024**2)
        process.cpu_times.return_value=Mock(user=1.0,system=0.0)
        memory=Mock(total=32*1024**3,available=24*1024**3)
        with patch("scripts.resource_budget.psutil.Process",return_value=process), \
             patch("scripts.resource_budget.psutil.virtual_memory",return_value=memory), \
             patch("scripts.resource_budget.psutil.cpu_percent",return_value=4.0) as cpu, \
             patch("scripts.resource_budget.time.perf_counter",return_value=10.0) as clock:
            budget=ResourceBudget(monitor=False)
            self.addCleanup(budget.close)
            clock.return_value=10.5
            budget.sample()
            baseline=dict(budget.previous)
            process.cpu_times.return_value=Mock(user=1.016,system=0.0)
            clock.return_value=10.501
            budget.sample()
            self.assertEqual(budget.previous,baseline)
            self.assertEqual(budget.clock,10.5)
            self.assertEqual(budget.current()["host_cpu_percent"],0)
            self.assertEqual(cpu.call_count,1)
            clock.return_value=11.0
            budget.sample()
            self.assertAlmostEqual(budget.current()["host_cpu_percent"],.4)
            self.assertFalse(budget.current()["cpu_pressure"])

    def test_two_foreground_and_two_background_execute_concurrently(self):
        entered=threading.Barrier(5)
        release=threading.Event()
        class Blocking(Model):
            def query(self, value):
                entered.wait(timeout=3)
                release.wait(3)
                return [1.0]
        foreground=self.pool(factory=Blocking)
        background=self.pool("background",factory=Blocking)
        self.assertTrue(foreground.state()["minimum_met"])
        self.assertTrue(background.state()["minimum_met"])
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            jobs=[executor.submit(pool.query,"q") for pool in (foreground,foreground,background,background)]
            try:
                entered.wait(timeout=3)
                self.assertEqual(self.budget.current()["active"],{"foreground":2,"background":2})
            finally:
                release.set()
            self.assertEqual([f.result() for f in jobs],[[1.0]]*4)

    def test_pressure_sheds_then_recovers_minimum(self):
        pool=self.pool()
        old=list(pool.models.values())
        self.snapshot["available"]=100*1024**2
        self.budget.sample()
        pool.tick()
        pool.tick()
        self.assertEqual(pool.state()["resident"],0)
        self.assertTrue(all(m.closed for m in old))
        with self.assertRaises(TimeoutError):
            pool.query("q")
        self.snapshot["available"]=24*1024**3
        self.budget.sample()
        pool.tick()
        pool.tick()
        self.assertTrue(pool.state()["minimum_met"])
        self.assertEqual(pool.query("q"),[1.0])

    def test_low_resource_startup_can_recover(self):
        self.snapshot["available"]=100*1024**2
        self.budget.sample()
        pool=self.pool()
        self.assertIsNone(pool.identity)
        self.snapshot["available"]=24*1024**3
        self.budget.sample()
        pool.tick()
        pool.tick()
        self.assertTrue(pool.state()["minimum_met"])

    def test_demand_grows_and_idle_shrinks_without_fixed_worker_count(self):
        pool=self.pool(idle_seconds=0)
        pool.demand.set()
        pool.tick()
        self.assertEqual(pool.state()["resident"],3)
        pool.tick()
        self.assertEqual(pool.state()["resident"],2)

    def test_failed_slot_is_replaced_and_permit_released(self):
        class Failed(Model):
            def query(self, value):
                raise RuntimeError("fixture inference error")
        first=Failed()
        models=iter([first,Model(),Model()])
        pool=self.pool(factory=lambda:next(models))
        with self.assertRaises(RuntimeError):
            pool.query("q")
        self.assertTrue(first.closed)
        self.assertEqual(self.budget.current()["active"]["foreground"],0)
        pool.tick()
        self.assertTrue(pool.state()["minimum_met"])

    def test_model_load_failure_releases_reservation_and_recovers(self):
        def fail():
            raise ValueError("fixture missing assets")
        pool=self.pool(factory=fail)
        self.assertEqual(self.budget.current()["resident_models"],0)
        pool.factory=Model
        pool.retry_after=0
        pool.tick()
        pool.tick()
        self.assertTrue(pool.state()["minimum_met"])

    def test_pending_model_loads_cannot_spend_same_headroom(self):
        self.snapshot["rss"]=int(self.snapshot["total"]*.15)-self.budget.model_bytes-1
        self.budget.sample()
        ticket=self.budget.reserve_model()
        self.assertIsNotNone(ticket)
        self.assertIsNone(self.budget.reserve_model())
        self.budget.release_model(ticket)
        self.assertIsNotNone(self.budget.reserve_model())

    def test_cpu_pressure_pauses_without_unloading_minimum(self):
        pool=self.pool()
        self.snapshot["system_cpu"]=95
        self.budget.sample()
        pool.tick()
        self.assertEqual(pool.state()["resident"],2)
        self.assertFalse(self.budget.acquire("foreground"))

    def test_background_cannot_consume_foreground_reserved_permits(self):
        self.snapshot["cpus"]=8
        self.budget.sample()
        self.assertEqual(self.budget.current()["inference_cpu_percent"],40)
        self.assertLess(self.budget.current()["inference_cpu_percent"],self.budget.cpu_percent)
        self.assertTrue(self.budget.acquire("background"))
        self.assertTrue(self.budget.acquire("background"))
        self.assertFalse(self.budget.acquire("background"))
        self.assertTrue(self.budget.acquire("foreground"))
        self.assertTrue(self.budget.acquire("foreground"))
        self.assertFalse(self.budget.acquire("foreground"))

    def test_continuous_foreground_demand_cannot_starve_queued_background(self):
        self.snapshot["cpus"]=8
        self.budget.sample()
        self.budget.queue("background",2)
        self.assertTrue(self.budget.acquire("foreground"))
        self.assertTrue(self.budget.acquire("foreground"))
        self.assertFalse(self.budget.acquire("foreground"))
        self.assertTrue(self.budget.acquire("background"))
        self.assertTrue(self.budget.acquire("background"))

    def test_queued_foreground_gets_one_extra_lane_without_stalling_background(self):
        self.snapshot["cpus"]=8
        self.budget.sample()
        self.budget.queue("foreground",1)
        self.budget.queue("background",2)
        self.assertTrue(self.budget.acquire("foreground"))
        self.assertTrue(self.budget.acquire("foreground"))
        self.assertTrue(self.budget.acquire("foreground"))
        self.assertFalse(self.budget.acquire("foreground"))
        self.assertTrue(self.budget.acquire("background"))
        self.assertFalse(self.budget.acquire("background"))

    def test_fixed_priority_switch_keeps_previous_two_lane_reservation(self):
        budget=ResourceBudget(snapshot_provider=lambda:self.snapshot,monitor=False,adaptive_priority=False)
        self.addCleanup(budget.close)
        self.snapshot["cpus"]=8
        budget.sample()
        budget.queue("foreground",1)
        budget.queue("background",2)
        self.assertTrue(budget.acquire("foreground"))
        self.assertTrue(budget.acquire("foreground"))
        self.assertFalse(budget.acquire("foreground"))
        budget.release("foreground")
        budget.release("foreground")
        self.assertTrue(budget.acquire("background"))
        self.assertTrue(budget.acquire("background"))

    def test_single_slot_alternates_when_both_roles_wait(self):
        self.snapshot["cpus"]=1
        self.budget.sample()
        self.budget.queue("foreground",1)
        self.budget.queue("background",1)
        self.assertTrue(self.budget.acquire("foreground"))
        self.budget.release("foreground")
        self.assertFalse(self.budget.acquire("foreground"))
        self.assertTrue(self.budget.acquire("background"))
        self.budget.release("background")
        self.assertFalse(self.budget.acquire("background"))
        self.assertTrue(self.budget.acquire("foreground"))

    def test_policy_limits_and_close(self):
        with self.assertRaises(ValueError):
            ResourceBudget(cpu_percent=99)
        state=evaluate(dict(self.snapshot,cpus=1))
        self.assertEqual(state["inference_limit"],1)
        self.assertGreaterEqual(state["free_reserve_bytes"],1024**3)
        pool=self.pool()
        models=list(pool.models.values())
        pool.close()
        pool.close()
        self.assertTrue(all(m.closed for m in models))
        self.assertEqual(self.budget.current()["resident_models"],0)
        with self.assertRaises(ValueError):
            pool.query("q")

    def test_close_waits_for_owned_inference_and_releases_every_reservation(self):
        entered=threading.Event()
        release=threading.Event()
        class Blocking(Model):
            def query(self,value):
                entered.set()
                release.wait(3)
                return [1.0]
        pool=self.pool(factory=Blocking)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            inference=executor.submit(pool.query,"q")
            self.assertTrue(entered.wait(1))
            closing=executor.submit(pool.close)
            try:
                self.assertFalse(closing.done())
            finally:
                release.set()
            self.assertEqual(inference.result(timeout=3),[1.0])
            closing.result(timeout=3)
        self.assertEqual(self.budget.current()["resident_models"],0)
