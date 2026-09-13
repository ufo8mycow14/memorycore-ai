"""Benchmark fixture failures must release their owned database and host."""
from pathlib import Path
from collections import Counter
from itertools import count
import queue
import tempfile
import unittest
from unittest.mock import Mock, patch
from scripts import benchmark_memory_host as benchmark
from scripts.index_telemetry import IndexLatency


class FixtureCleanupTests(unittest.TestCase):
    def test_maintenance_verification_waits_for_terminal_success(self):
        wire=Mock()
        wire.call.side_effect=[{"result":{"job_id":"job","state":"queued","verified":False}},
                               {"result":{"job_id":"job","state":"complete","verified":True}}]
        with patch.object(benchmark.time,"sleep"):
            result=benchmark.verify_scope(wire,"chat-0","maintenance")
        self.assertTrue(result["result"]["verified"])
        self.assertEqual(wire.call.call_count,2)
        self.assertIn("start_ms",result["maintenance"])

    def test_maintenance_verification_retains_failure(self):
        wire=Mock()
        wire.call.return_value={"result":{"job_id":"job","state":"failed","verified":False,"error":"native_rejected"}}
        result=benchmark.verify_scope(wire,"chat-0","maintenance")
        self.assertEqual(result["error"],"native_rejected")
        self.assertFalse(result["maintenance"]["verified"])

    def test_maintenance_verification_retains_polled_failure(self):
        wire=Mock()
        wire.call.side_effect=[{"result":{"job_id":"job","state":"queued"}},
                               {"result":{"job_id":"job","state":"failed","verified":False,"error":"deadline"}}]
        with patch.object(benchmark.time,"sleep"):
            result=benchmark.verify_scope(wire,"chat-0","maintenance")
        self.assertEqual(result["error"],"deadline")
        self.assertEqual(result["maintenance"]["job_id"],"job")
        self.assertIn("start_ms",result["maintenance"])

    def test_maintenance_verification_preserves_transport_errors(self):
        for response in ({"error":"transport_closed"},{"result":{"error":"admin_required"}}):
            with self.subTest(response=response):
                wire=Mock()
                wire.call.return_value=response
                self.assertEqual(benchmark.verify_scope(wire,"chat-0","maintenance"),response)

    def test_encryption_remains_the_default_benchmark_mode(self):
        self.assertEqual(benchmark.Scenario.storage,"sqlcipher")

    def test_plaintext_fixture_validates_format_and_does_not_pass_a_key(self):
        with tempfile.TemporaryDirectory() as root:
            harness=Mock()
            harness.root=Path(root)
            harness.file=Path(root)/"config.json"
            harness.config={"python":"unused","backend_root":"unused","sessions":[{}]}
            def initialise(*args,**kwargs):
                (Path(root)/"plaintext-native.sqlite3").write_bytes(b"SQLite format 3\0")
                self.assertNotIn("MEMORYCORE_AI_SYNTHETIC_TEST_KEY",kwargs["env"])
                return Mock(returncode=0)
            with patch.object(benchmark.fixtures,"Harness",return_value=harness), \
                 patch.dict(benchmark.os.environ,{"MEMORYCORE_AI_SYNTHETIC_TEST_KEY":"synthetic-only"}), \
                 patch.object(benchmark.subprocess,"run",side_effect=initialise):
                fixture=benchmark.PlaintextBenchmarkFixture()
                fixture.setUp()
            self.assertEqual(harness.config["backend"],"native")
            self.assertTrue(harness.config["sessions"][0]["allow_admin"])
            harness.close.assert_not_called()
            fixture.doCleanups()
            harness.close.assert_called_once()

    def test_plaintext_fixture_rejects_non_sqlite_output(self):
        with tempfile.TemporaryDirectory() as root:
            harness=Mock()
            harness.root=Path(root)
            harness.file=Path(root)/"config.json"
            harness.config={"python":"unused","backend_root":"unused","sessions":[]}
            (Path(root)/"plaintext-native.sqlite3").write_bytes(b"not a SQLite database")
            with patch.object(benchmark.fixtures,"Harness",return_value=harness), \
                 patch.object(benchmark.subprocess,"run",return_value=Mock(returncode=0)):
                with self.assertRaisesRegex(RuntimeError,"ordinary SQLite"):
                    benchmark.PlaintextBenchmarkFixture().setUp()
            harness.close.assert_called_once()

    def test_unknown_storage_fails_before_fixture_creation(self):
        with patch.object(benchmark.Scenario,"storage","typo"), \
             patch.object(benchmark.fixtures,"EncryptedNativeTests") as fixture:
            with self.assertRaisesRegex(ValueError,"storage"):
                benchmark.Scenario()
        fixture.assert_not_called()

    def test_plaintext_fixture_cleans_up_failed_initialisation(self):
        with tempfile.TemporaryDirectory() as root:
            harness=Mock()
            harness.root=Path(root)
            harness.file=Path(root)/"config.json"
            harness.config={"python":"unused","backend_root":"unused","sessions":[{}]}
            with patch.object(benchmark.fixtures,"Harness",return_value=harness), \
                 patch.object(benchmark.subprocess,"run",return_value=Mock(returncode=1,stderr=b"synthetic")):
                with self.assertRaisesRegex(RuntimeError,"initialisation"):
                    benchmark.PlaintextBenchmarkFixture().setUp()
            harness.close.assert_called_once()
            self.assertTrue(harness.config["allow_plaintext"])
            self.assertNotIn("key_env",harness.config)

    def test_growth_preserves_stage_results_when_integrity_worker_fails(self):
        scenario=Mock()
        scenario.ids=["gold"]*12
        scenario.project_ids={0:["gold"],1:["second-scope"]}
        scenario.grow.return_value={"total_seconds":1,"catchup":{"drained":True}}
        scenario.wire.recall.return_value={"semantic_host":{"retrieval":{"index_cache_hit":True,"vector_state":"ready"}}}
        scenario.wire.call.side_effect=[{"error":"worker_failed_no_automatic_retry",
            "worker_failure":{"kind":"deadline","deadline_ms":10000},"timing":{"service_ms":10000}},
            {"result":{"verified":True}}]
        resources=Mock()
        saved=[]
        with patch.object(benchmark,"Scenario",return_value=scenario), \
             patch.object(benchmark,"ResourcePeaks",return_value=resources), \
             patch.object(benchmark,"unpack",return_value=({},[{"id":"gold"}])),patch("builtins.print"):
            result=benchmark.growth(source_bound=True,sizes=(24,),checkpoint=saved.append)
        self.assertFalse(result["integrity_verified"])
        self.assertEqual(result["integrity_by_project"],{"0":False,"1":True})
        self.assertEqual(result["integrity_errors"]["0"]["error"],"worker_failed_no_automatic_retry")
        self.assertEqual(result["integrity_errors"]["0"]["worker_failure"]["kind"],"deadline")
        self.assertEqual(saved,result["results"])
        self.assertEqual(len(saved[0]["queries"]),24)
        resources.close.assert_called_once()
        scenario.close.assert_called_once()

    def test_growth_verifies_all_seeded_scopes_and_closes_owned_resources(self):
        scenario=Mock()
        scenario.ids=["gold"]*12
        scenario.project_ids={0:["gold"],1:["second-scope"]}
        scenario.grow.return_value={"total_seconds":1,"catchup":{"drained":True}}
        scenario.wire.recall.return_value={"semantic_host":{"retrieval":{"index_cache_hit":True,"vector_state":"ready"}}}
        scenario.wire.call.return_value={"result":{"verified":True}}
        resources=Mock()
        with patch.object(benchmark,"Scenario",return_value=scenario), \
             patch.object(benchmark,"ResourcePeaks",return_value=resources), \
             patch.object(benchmark,"unpack",return_value=({},[{"id":"gold"}])),patch("builtins.print"):
            result=benchmark.growth(source_bound=True,sizes=(24,))
        self.assertTrue(result["integrity_verified"])
        self.assertEqual(result["integrity_by_project"],{"0":True,"1":True})
        self.assertEqual(scenario.wire.call.call_args_list,[
            unittest.mock.call("chat-0","admin",{"action":"verify","arguments":{}}),
            unittest.mock.call("chat-1","admin",{"action":"verify","arguments":{}})])
        resources.close.assert_called_once()
        scenario.close.assert_called_once()

    def test_growth_retains_failed_recall_attempts_instead_of_dropping_the_stage(self):
        scenario=Mock()
        scenario.ids=["gold"]*12
        scenario.project_ids={0:["gold"]}
        scenario.grow.return_value={"total_seconds":1,"catchup":{"drained":True}}
        scenario.wire.recall.return_value={"error":"worker_unavailable"}
        scenario.wire.call.return_value={"result":{"verified":True}}
        with patch.object(benchmark,"Scenario",return_value=scenario), \
             patch.object(benchmark,"ResourcePeaks"),patch("builtins.print"):
            result=benchmark.growth(source_bound=True,sizes=(24,))
        row=result["results"][0]
        self.assertEqual(row["hit_at_8"],0)
        self.assertEqual(row["vector_states"],{"request_failed":24})
        self.assertTrue(all(query["error"]=="worker_unavailable" for query in row["queries"]))

    def test_growth_peak_sampler_is_bounded_and_discloses_missing_samples(self):
        wire=Mock()
        wire.sample.side_effect=[{"rss_bytes":4,"private_bytes":6},{"rss_bytes":3,"private_bytes":5},
                                 {"rss_bytes":7,"private_bytes":None},OSError("synthetic failure")]
        sampler=benchmark.ResourcePeaks(wire)
        for _ in range(4):
            sampler.sample()
        row=sampler.snapshot()
        self.assertEqual(row["samples"],3)
        self.assertEqual(row["sample_failures"],1)
        self.assertEqual(row["unknown_private_samples"],1)
        self.assertEqual(row["peak_private_bytes"],6)
        self.assertEqual(row["peak_tree_rss_bytes"],7)
        self.assertIsNone(sampler.thread)
        sampler.close()

    def test_growth_peak_sampler_joins_before_fixture_cleanup(self):
        wire=Mock()
        wire.sample.return_value={"rss_bytes":4,"private_bytes":6}
        sampler=benchmark.ResourcePeaks(wire,interval=.001)
        sampler.start()
        sampler.close()
        self.assertFalse(sampler.thread.is_alive())
        self.assertGreaterEqual(sampler.snapshot()["samples"],1)

    def test_slow_request_diagnostics_are_bounded_and_exclude_payloads(self):
        samples=benchmark.SlowRequests(limit=3)
        response={"timing":{"queue_ms":4},"native_timing":{"commit_ms":2},
                  "result":{"text":"not diagnostic metadata"},
                  "semantic_host":{"timing":{"candidates_ms":1},"query":"excluded"}}
        for category in ("recall","insert"):
            for elapsed in (9,3,8,2,9,1,4):
                samples.record(category,elapsed,12,response)
            self.assertEqual([row["latency_ms"] for row in samples.rows[category]],[9,9,8])
            self.assertEqual(samples.rows[category][0]["timing"],{"queue_ms":4})
            self.assertEqual(samples.rows[category][0]["foreground_timing"],{"candidates_ms":1})
            self.assertNotIn("result",samples.rows[category][0])
            self.assertNotIn("query",samples.rows[category][0])

    def test_streaming_quantiles_are_conservative_and_constant_in_size(self):
        values=[0,.001,.019,.1,1.01,5.751,10.001,49.91,149.01,150.0,60000.1]
        histogram=benchmark.BoundedLatency()
        size=len(histogram.buckets)
        self.assertIsNone(benchmark.summary(histogram)["p99"])
        for value in values:
            histogram.append(value)
        exact=benchmark.summary(values)
        bounded=benchmark.summary(histogram)
        self.assertEqual(bounded["count"],len(values))
        self.assertEqual(bounded["max"],max(values))
        for key in ("p50","p95","p99"):
            self.assertGreaterEqual(bounded[key],exact[key])
        for _ in range(10000):
            histogram.append(.019)
        self.assertEqual(len(histogram.buckets),size)
        self.assertEqual(sum(histogram.buckets),10000+len(values))
        self.assertEqual(benchmark.summary(histogram)["p99"],.02)
        for invalid in (-1,float("nan"),float("inf"),None):
            with self.assertRaises(ValueError):
                histogram.append(invalid)

    def test_last_arrival_is_offered_when_sampling_crosses_duration_boundary(self):
        scenario=Mock()
        scenario.projects=1
        scenario.catchup.return_value={"drained":True,"pending":0}
        scenario.project_ids={0:["gold"]}
        scenario.fixture.target=Path("nonexistent-synthetic-benchmark.sqlite3")
        wire=scenario.wire
        wire.admin.return_value={"memory_id":"fixture","verified":True}
        wire.recall.return_value={"semantic_host":{"index":{"latency":IndexLatency().snapshot()}}}
        wire.sample.return_value={"cpu_seconds":0,"rss_bytes":1,"private_bytes":1}
        wire.send.return_value="final-arrival"
        wire.queue.get_nowait.side_effect=queue.Empty
        wire.queue.get.return_value={"session":"chat-0","id":"final-arrival","result":{},
            "timing":{"queue_ms":0,"service_ms":0},
            "semantic_host":{"retrieval":{"vector_state":"ready","reranker_state":"ready"}}}
        with patch.object(benchmark.time,"perf_counter",side_effect=count(0,1.1)), \
             patch.object(benchmark,"unpack",return_value=({},[{"id":"gold"}])):
            result=benchmark.workload(scenario,clients=1,rate=1,seconds=1,label="synthetic-boundary")
        self.assertEqual(result["offered"],result["expected_offered"])
        self.assertEqual(result["counts"]["completed"],1)
        self.assertEqual(result["counts"]["retrieval_hit"],1)
        wire.send.assert_called_once()

    def stalled_driver(self,host_busy):
        scenario=Mock()
        scenario.projects=1
        scenario.catchup.return_value={"drained":True,"pending":0}
        scenario.project_ids={0:["gold"]*len(benchmark.CORPUS)}
        scenario.fixture.target=Path("nonexistent-synthetic-benchmark.sqlite3")
        wire=scenario.wire
        wire.admin.return_value={"memory_id":"fixture","verified":True}
        wire.recall.return_value={"semantic_host":{"index":{"latency":IndexLatency().snapshot()}}}
        wire.sample.return_value={"cpu_seconds":0,"rss_bytes":1,"private_bytes":1}
        clock=[0.0]
        waiting=[]

        class StalledQueue(queue.Queue):
            stalled=False

            def get(self,block=True,timeout=None):
                if block and not self.stalled:
                    self.stalled=True
                    clock[0]=1.1
                    if not host_busy:
                        self.put(waiting.pop())
                    raise queue.Empty
                if block and host_busy and waiting:
                    clock[0]=2.1
                    self.put(waiting.pop())
                return super().get(block=block,timeout=timeout)

        wire.queue=StalledQueue()

        def send(session,operation,arguments):
            ident=str(wire.send.call_count)
            response={"session":session,"id":ident,"result":{},
                "timing":{"queue_ms":0,"service_ms":0},
                "semantic_host":{"retrieval":{"vector_state":"ready","reranker_state":"ready"}}}
            if wire.send.call_count==1:
                waiting.append(response)
            else:
                clock[0]=2.1
                wire.queue.put(response)
            return ident

        wire.send.side_effect=send
        with patch.object(benchmark.time,"perf_counter",side_effect=lambda:clock[0]), \
             patch.object(benchmark,"unpack",return_value=({},[{"id":"gold"}])):
            return benchmark.workload(scenario,clients=1,rate=1,seconds=2,label="synthetic-driver-stall")

    def test_queued_response_is_drained_before_next_arrival(self):
        result=self.stalled_driver(host_busy=False)
        self.assertEqual(result["offered"],2)
        self.assertEqual(result["counts"]["completed"],2)
        self.assertEqual(result["counts"].get("client_backpressure",0),0)
        self.assertGreater(result["scheduled_to_response_ms"]["p99"],1000)
        self.assertGreater(result["scheduling_lag_ms"]["p99"],90)

    def test_outstanding_host_response_still_counts_as_backpressure(self):
        result=self.stalled_driver(host_busy=True)
        self.assertEqual(result["offered"],2)
        self.assertEqual(result["counts"]["completed"],1)
        self.assertEqual(result["counts"]["client_backpressure"],1)
        self.assertEqual(result["backpressure_examples"][0]["offered_sequence"],1)
        self.assertGreater(result["backpressure_examples"][0]["outstanding_ms"],1000)

    def test_offered_chat_workload_is_independent_of_response_order(self):
        observed=[Counter() for _ in range(100)]
        for n in range(1000):
            client,kind=benchmark.offered_operation(n,100)
            observed[client][kind]+=1
        self.assertTrue(all(counts==Counter(range(10)) for counts in observed))
        self.assertEqual(Counter(kind for n in range(12000) for _,kind in [benchmark.offered_operation(n,100)]),
                         Counter({kind:1200 for kind in range(10)}))

    def test_nearest_rank_keeps_small_sample_tails(self):
        self.assertEqual(benchmark.summary(list(range(1,25)))["p99"],24)
        self.assertEqual(benchmark.summary(list(range(1,101)))["p99"],99)
        self.assertIsNone(benchmark.summary([])["p99"])

    def test_growth_reports_native_stage_timings_after_real_insert_batches(self):
        scenario=benchmark.Scenario.__new__(benchmark.Scenario)
        scenario.noise=0
        scenario.wire=Mock()
        scenario.wire.call.return_value={"result":{},"native_timing":{"execute_ms":2.0}}
        scenario.wire.admin.return_value={"pending":0}
        scenario.catchup=Mock(return_value={"drained":True})
        result=scenario.grow(16)
        self.assertEqual(scenario.wire.call.call_count,2)
        self.assertEqual(result["native_write_stages_ms"]["execute_ms"]["count"],2)
        self.assertEqual(result["native_write_stages_ms"]["execute_ms"]["p99"],2.0)

    def fixture(self,root):
        fixture=Mock()
        fixture.h.root=Path(root)
        fixture.h.file=Path(root)/"host.json"
        fixture.h.config={"sessions":[{"id":"original","scope":"synthetic:original"}]}
        return fixture

    def test_host_constructor_failure_closes_fixture(self):
        with tempfile.TemporaryDirectory() as root:
            fixture=self.fixture(root)
            with patch.object(benchmark.fixtures,"EncryptedNativeTests",return_value=fixture), \
                 patch.object(benchmark,"Wire",side_effect=RuntimeError("fixture startup failure")):
                with self.assertRaisesRegex(RuntimeError,"startup failure"):
                    benchmark.Scenario(1)
            fixture.doCleanups.assert_called_once_with()

    def test_tokenizer_failure_closes_host_before_fixture(self):
        with tempfile.TemporaryDirectory() as root:
            fixture=self.fixture(root)
            wire=Mock()
            order=[]
            wire.close.side_effect=lambda:order.append("host")
            fixture.doCleanups.side_effect=lambda:order.append("fixture")
            with patch.object(benchmark.fixtures,"EncryptedNativeTests",return_value=fixture), \
                 patch.object(benchmark,"Wire",return_value=wire), \
                 patch.object(benchmark.tiktoken,"get_encoding",side_effect=RuntimeError("fixture tokenizer failure")):
                with self.assertRaisesRegex(RuntimeError,"tokenizer failure"):
                    benchmark.Scenario(1)
            self.assertEqual(order,["host","fixture"])
