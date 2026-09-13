"""Routing policy, durable state and lost-acknowledgement recovery contracts."""
import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts import memorycore_ai as bm
from scripts.knowledge_layer import Knowledge
from scripts.knowledge_layer import SourceRoot
from scripts.session_routing import (Placement, Session, Boundary, Costs, Capabilities, RouteOutbox,
                                    plan, detect_boundary, save_checkpoint, load_checkpoint, STATE_FIELDS)


NOW = 1000.0
PLACE = Placement("local", "project-synthetic", "project_chat")


def session(sid="current", task="alpha", history=30000, **overrides):
    fields = dict(id=sid, task_key=task, placement=PLACE, history_tokens=history,
                  status="idle", revision="r1", observed_at=NOW, turns_since_transition=5)
    return Session(**(fields | overrides))


def state():
    return {"goal": "Finish the synthetic deployment checker", "requirements": ["Retain ID A-17 exactly."],
            "decisions": ["Retry transient failures three times."], "constraints": ["Never retry invalid input."],
            "progress": ["Parser implemented."], "dependencies": ["Fixture source version 2."],
            "unresolved": ["Approval still required for publication."], "unfinished": ["Round-trip verification is unfinished."],
            "references": ["synthetic-spec.md lines 1-8"]}


class FakeGateway:
    capabilities = Capabilities(True, True, True, True, True, True, True, True)

    def __init__(self):
        self.deliveries = {}
        self.calls = 0
        self.observation = {"status": "idle", "revision": "r1"}
        self.lose_ack = False
        self.race = False

    def observe(self, source):
        return dict(self.observation)

    def deliver_once(self, request):
        if self.race:
            raise RuntimeError("atomic idle check rejected concurrent work")
        key = (request["scope"], request["message_id"])
        if key not in self.deliveries:
            self.calls += 1
            route = request["route"]
            self.deliveries[key] = {"scope": request["scope"], "message_id": request["message_id"],
                "message_sha256": request["message_sha256"], "delivered": True, "source_preserved": True,
                "placement": route["placement"], "destination_id": route["target"] or "new-synthetic-session",
                "operation": request["operation"], "history_imported": False}
        if self.lose_ack:
            raise TimeoutError("acknowledgement lost")
        return self.deliveries[key]

    def lookup(self, scope, message_id):
        return self.deliveries.get((scope, message_id))


class RoutingPolicyTests(unittest.TestCase):
    def fresh(self, **changes):
        return Boundary(**(dict(task_key="beta", meaningful_change=True, needs_current_history=False, selective_context_complete=True) | changes))

    def route(self, boundary=None, current=None, others=(), costs=None, **kwargs):
        return plan(current or session(), boundary or self.fresh(), others, costs or Costs(3, 1200, memory_write_tokens=200, retrieval_tokens=100), now=NOW, **kwargs)

    def test_long_chat_without_meaningful_change_continues(self):
        self.assertEqual(self.route(Boundary("alpha", needs_current_history=False, selective_context_complete=True))["action"], "continue")

    def test_related_topic_and_tangent_do_not_fragment(self):
        for field in ("related", "tangent", "keep_here", "needs_current_history"):
            self.assertEqual(self.route(self.fresh(**{field:True}))["action"], "continue")

    def test_expensive_transition_is_rejected_including_rework(self):
        result = self.route(costs=Costs(2, 1000, retry_rework_tokens=60000))
        self.assertEqual(result["reason"], "transition_does_not_repay_total_cost")

    def test_measured_fresh_verification_cost_can_make_continuation_cheaper(self):
        result = self.route(current=session(history=27000), costs=Costs(1, 19000, fresh_verification_tokens=19500))
        self.assertEqual(result["action"], "continue")
        self.assertEqual(result["estimated_tokens"]["fresh"], 38500)
        resumed = self.route(current=session(history=40000), others=[session("prior", "beta", 15000)],
            costs=Costs(1, 10000, fresh_verification_tokens=19500))
        self.assertEqual(resumed["action"], "resume")

    def test_fresh_is_selected_only_with_complete_relevant_context(self):
        self.assertEqual(self.route()["action"], "fresh")
        self.assertEqual(self.route(self.fresh(selective_context_complete=False))["action"], "continue")

    def test_small_existing_relevant_session_is_resumed(self):
        result = self.route(self.fresh(returning=True), others=[session("prior", "beta", 800)])
        self.assertEqual((result["action"], result["target"]), ("resume", "prior"))

    def test_large_old_session_does_not_force_full_history_reload(self):
        result = self.route(self.fresh(returning=True), others=[session("prior", "beta", 25000)])
        self.assertEqual(result["action"], "fresh")

    def test_near_tie_prefers_existing_session(self):
        result = self.route(others=[session("prior", "beta", 1220)])
        self.assertEqual(result["action"], "resume")

    def test_active_unknown_and_stale_observations_prevent_transition(self):
        for s in (session(status="running"), session(status="unknown"), session(observed_at=0), session(revision="")):
            self.assertEqual(self.route(current=s)["reason"], "active_or_unverified_operations")

    def test_new_chat_remains_in_current_project(self):
        other = Placement("local", "other-project", "project_chat")
        self.assertEqual(self.route(desired_placement=other)["action"], "continue")
        self.assertEqual(self.route(desired_placement=other, placement_authorised=True)["action"], "continue")

    def test_outside_project_requires_ordinary_chat(self):
        p = Placement("local", None, "ordinary_chat")
        self.assertEqual(self.route(current=session(placement=p))["placement"]["surface"], "ordinary_chat")
        with self.assertRaises(ValueError):
            Placement("local", None, "project_chat")

    def test_wrong_project_and_running_candidate_are_not_resumed(self):
        wrong = Placement("local", "wrong", "project_chat")
        result = self.route(others=[session("prior", "beta", 1, placement=wrong), session("busy", "beta", 1, status="running")])
        self.assertEqual(result["action"], "fresh")

    def test_cooldown_limits_fragmentation_but_user_can_override(self):
        current = session(turns_since_transition=1)
        self.assertEqual(self.route(current=current)["action"], "continue")
        self.assertEqual(self.route(current=current, boundary=self.fresh(explicit_fresh=True))["action"], "fresh")

    def test_detector_does_not_treat_word_changes_as_task_changes(self):
        self.assertFalse(detect_boundary("Next question: how does that module work?", "alpha", {"alpha"}).meaningful_change)
        self.assertTrue(detect_boundary("New task: beta\nBuild a different fixture", "alpha", {"alpha"}).meaningful_change)
        self.assertTrue(detect_boundary("Resume task: beta", "alpha", {"beta"}).returning)
        self.assertFalse(detect_boundary("Resume task: missing", "alpha", {"beta"}).returning)
        self.assertTrue(detect_boundary("Keep this in the current chat", "alpha", {}).keep_here)


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="brain-router-synthetic-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "fixture.sqlite3"
        self.conn = bm.connect(str(self.path))
        bm.initialize(self.conn)
        self.k = Knowledge(self.conn, scope="synthetic:routing", sources=SourceRoot(self.tmp.name), synthetic=True, create=True)
        self.box = RouteOutbox(self.k, create=True)
        self.addCleanup(lambda: self.conn.close())
        self.route = plan(session(), Boundary("beta", meaningful_change=True, needs_current_history=False, selective_context_complete=True), [], Costs(3, 1200), now=NOW)
        self.gateway = FakeGateway()

    def prepared(self, mid="event-1"):
        return self.box.prepare(mid, "New task: beta\nPreserve exact ID B-18.", self.route, "alpha", state(), target_packet={"task_key": "beta", "state": {"goal": "Build beta", "requirements": ["ID B-18"]}})

    def test_checkpoint_exact_values_and_all_categories_survive_restart(self):
        item = self.prepared()
        self.conn.close()
        self.conn = bm.connect(str(self.path))
        self.k = Knowledge(self.conn, scope="synthetic:routing", sources=SourceRoot(self.tmp.name), synthetic=True)
        self.box = RouteOutbox(self.k)
        loaded = load_checkpoint(self.k, item["checkpoint"], count=len, max_tokens=3000)
        self.assertEqual(loaded["state"], state())
        selected = load_checkpoint(self.k, item["checkpoint"], categories=["constraints", "unfinished"], count=len, max_tokens=1000)
        self.assertEqual(set(selected["state"]), {"constraints", "unfinished"})
        self.assertIn("requirements", selected["unloaded_categories"])

    def test_incomplete_or_overbudget_handoff_cannot_silently_drop_requirements(self):
        invalid = state()
        invalid.pop("unfinished")
        with self.assertRaises(ValueError):
            self.box.prepare("event-1", "pending", self.route, "alpha", invalid, target_packet={"task_key": "beta", "state": {}})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM session_route").fetchone()[0], 0)
        item = self.prepared()
        with self.assertRaises(ValueError):
            load_checkpoint(self.k, item["checkpoint"], count=len, max_tokens=50)

    def test_pending_message_repetition_is_idempotent_but_collision_rejected(self):
        self.assertEqual(self.prepared(), self.prepared())
        with self.assertRaises(ValueError):
            self.box.prepare("event-1", "different", self.route, "alpha", state(), target_packet={"task_key": "beta", "state": {}})

    def test_missing_capability_produces_handoff_without_creating_chat(self):
        self.prepared()
        self.gateway.capabilities = Capabilities(resume=True)
        result = self.box.dispatch("event-1", self.gateway)
        self.assertFalse(result["occurred"])
        self.assertEqual(result["pending_message"], "New task: beta\nPreserve exact ID B-18.")
        self.assertEqual(self.gateway.calls, 0)

    def test_delivered_message_is_never_sent_twice(self):
        self.prepared()
        a = self.box.dispatch("event-1", self.gateway)
        self.assertEqual(self.box.dispatch("event-1", self.gateway), a)
        self.assertEqual(self.gateway.calls, 1)
        handoff = self.box.handoff("event-1")
        self.assertEqual(handoff["target_session"], a["destination_id"])
        self.assertIn("do not resend", handoff["user_action"])

    def test_fresh_delivery_does_not_expose_saved_source_state(self):
        item = self.prepared()
        captured = []
        deliver = self.gateway.deliver_once
        def spy(request):
            captured.append(copy.deepcopy(request))
            return deliver(request)
        self.gateway.deliver_once = spy
        self.box.dispatch("event-1", self.gateway)
        request = captured[0]
        self.assertEqual(request["operation"], "thread/start")
        self.assertEqual(set(request), {"scope", "message_id", "message_sha256", "route", "model_input", "operation"})
        self.assertEqual(request["model_input"][0]["text"], item["message"])
        self.assertNotIn("A-17", str(request["model_input"]))
        self.assertNotIn(item["checkpoint"]["archive_id"], str(request))
        self.conn.close()
        self.conn = bm.connect(str(self.path))
        self.k = Knowledge(self.conn, scope="synthetic:routing", sources=SourceRoot(self.tmp.name), synthetic=True)
        self.assertEqual(load_checkpoint(self.k, item["checkpoint"], count=len, max_tokens=3000)["state"], state())

    def test_fresh_capability_is_required_even_with_all_old_capabilities(self):
        self.prepared()
        self.gateway.capabilities = Capabilities(True, True, True, True, True, True, True)
        self.assertFalse(self.box.dispatch("event-1", self.gateway)["occurred"])
        self.assertEqual(self.gateway.calls, 0)

    def test_unrelated_or_transcript_packet_is_rejected_before_checkpoint_write(self):
        for packet in ({"task_key": "alpha", "state": state()},
                       {"task_key": "beta", "state": {"transcript": ["old chat"]}},
                       {"task_key": "beta", "state": {}, "rollout": "old"}):
            with self.assertRaises(ValueError):
                self.box.prepare("bad", "pending", self.route, "alpha", state(), target_packet=packet)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM session_route").fetchone()[0], 0)

    def test_copied_history_or_resumed_source_receipts_never_pass_as_fresh(self):
        for index, overrides in enumerate(({"operation": "thread/fork"}, {"history_imported": True},
                                          {"destination_id": "current"}, {"operation": "thread/resume"})):
            mid = "bad-" + str(index)
            self.prepared(mid)
            request = {"scope": self.k.scope, "message_id": mid, "message_sha256": self.box._read(mid)["message_sha256"],
                       "delivered": True, "source_preserved": True, "placement": self.route["placement"],
                       "destination_id": "new", "operation": "thread/start", "history_imported": False}
            with self.assertRaises(ValueError):
                self.box._accept_receipt(mid, request | overrides)

    def test_fresh_configuration_rejects_history_copying_inputs(self):
        from scripts.fresh_context import start_params
        for key in ("history", "rollout", "threadId", "forkedFromId", "path", "input", "config"):
            with self.assertRaises(ValueError):
                start_params({key: "source"})
        self.assertEqual(start_params({"cwd": "synthetic", "approvalPolicy": "never"}),
                         {"cwd": "synthetic", "approvalPolicy": "never"})

    def test_lost_ack_is_reconciled_after_restart_without_duplicate_delivery(self):
        self.prepared()
        self.gateway.lose_ack = True
        with self.assertRaises(TimeoutError):
            self.box.dispatch("event-1", self.gateway)
        with self.assertRaises(ValueError):
            self.box.dispatch("event-1", self.gateway)
        restarted = RouteOutbox(self.k)
        self.assertIn("Reconcile before any resend", self.box.handoff("event-1")["user_action"])
        self.assertTrue(restarted.reconcile("event-1", self.gateway)["delivered"])
        self.assertEqual(self.gateway.calls, 1)

    def test_observation_failure_does_not_mean_no_delivery(self):
        self.prepared()
        self.gateway.lose_ack = True
        with self.assertRaises(TimeoutError):
            self.box.dispatch("event-1", self.gateway)
        self.gateway.lookup = lambda *args: (_ for _ in ()).throw(TimeoutError())
        with self.assertRaises(TimeoutError):
            self.box.reconcile("event-1", self.gateway)
        self.assertEqual(self.box._read("event-1")["state"], "RECONCILE")

    def test_busy_source_and_race_do_not_interrupt_original_work(self):
        self.prepared()
        self.gateway.observation = {"status": "running", "revision": "r1"}
        with self.assertRaises(ValueError):
            self.box.dispatch("event-1", self.gateway)
        self.assertEqual(self.gateway.calls, 0)
        self.gateway.observation["status"] = "idle"
        self.gateway.race = True
        with self.assertRaises(RuntimeError):
            self.box.dispatch("event-1", self.gateway)
        self.assertEqual(self.gateway.calls, 0)

    def test_wrong_placement_acknowledgement_is_not_accepted(self):
        self.prepared()
        self.gateway.deliver_once = lambda item: {"message_id": item["message_id"], "scope": item["scope"],
            "message_sha256": item["message_sha256"], "delivered": True, "source_preserved": True,
            "placement": {"host": "local", "project_id": None, "surface": "ordinary_chat"}, "destination_id": "wrong"}
        with self.assertRaises(ValueError):
            self.box.dispatch("event-1", self.gateway)
        self.assertEqual(self.box._read("event-1")["state"], "RECONCILE")

    def test_user_can_keep_task_here_before_dispatch(self):
        self.prepared()
        self.assertTrue(self.box.cancel("event-1")["continue_original"])
        with self.assertRaises(ValueError):
            self.box.dispatch("event-1", self.gateway)

    def test_exports_do_not_omit_pending_message_or_checkpoint(self):
        self.prepared()
        with self.assertRaises(SystemExit):
            bm.export_scope(self.conn, self.k.scope)
        with self.assertRaises(ValueError):
            self.k.export()
        package = self.box.export()
        self.assertEqual(len(package["intents"]), 1)
        self.assertFalse(package["automatic_delivery_restore"])

    def test_foreign_scope_cannot_recover_pending_message(self):
        self.prepared()
        other = Knowledge(self.conn, scope="synthetic:other", sources=self.k.sources, synthetic=True)
        with self.assertRaises(ValueError):
            RouteOutbox(other)._read("event-1")

    def test_return_to_updated_checkpoint_uses_latest_claim_version(self):
        a = save_checkpoint(self.k, "alpha", "thread-a", state())
        updated = state()
        updated["unfinished"] = []
        b = save_checkpoint(self.k, "alpha", "thread-b", updated)
        self.assertNotEqual(a["memory_id"], b["memory_id"])
        self.assertEqual(load_checkpoint(self.k, b, count=len, max_tokens=3000)["state"]["unfinished"], [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM cortex_memory WHERE status=0").fetchone()[0], 1)
        with self.assertRaises(ValueError):
            load_checkpoint(self.k, a, count=len, max_tokens=3000)

    def test_checkpoint_freshness_and_exact_link_are_checked(self):
        source = Path(self.tmp.name) / "contract.json"
        source.write_text('{"identifier":"A-17"}', encoding="utf-8")
        a = save_checkpoint(self.k, "alpha", "current", state(), source_paths=["contract.json"])
        self.assertEqual(load_checkpoint(self.k, a, count=len, max_tokens=3000)["source_status"], "fresh")
        wrong = dict(a, memory_id="0"*32)
        with self.assertRaises(ValueError):
            load_checkpoint(self.k, wrong, count=len, max_tokens=3000)
        source.write_text('{"identifier":"A-18"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_checkpoint(self.k, a, count=len, max_tokens=3000)

    def test_reconciliation_cannot_reset_concurrently_confirmed_delivery(self):
        self.prepared()
        self.gateway.lose_ack = True
        with self.assertRaises(TimeoutError):
            self.box.dispatch("event-1", self.gateway)
        receipt = self.gateway.deliveries[(self.k.scope, "event-1")]
        def stale_lookup(*args):
            self.box._accept_receipt("event-1", receipt)
            return None
        self.gateway.lookup = stale_lookup
        self.assertEqual(self.box.reconcile("event-1", self.gateway), receipt)
        self.assertEqual(self.box._read("event-1")["state"], "DELIVERED")
        self.assertEqual(self.box.dispatch("event-1", self.gateway), receipt)
        self.assertEqual(self.gateway.calls, 1)

    def test_forged_placement_change_and_bad_capabilities_rejected(self):
        route = copy.deepcopy(self.route)
        route["placement"]["project_id"] = "other-project"
        with self.assertRaises(ValueError):
            self.box.prepare("event", "pending", route, "alpha", state(), target_packet={"task_key":"beta", "state":{}})
        route["placement_authorised"] = True
        with self.assertRaises(ValueError):
            self.box.prepare("event", "pending", route, "alpha", state(), target_packet={"task_key":"beta", "state":{}})
        with self.assertRaises(ValueError):
            Capabilities(capture_pending_message="false")

    def test_changed_source_state_cannot_reuse_pending_message_identity(self):
        self.prepared()
        updated = state()
        updated["unfinished"] = []
        with self.assertRaises(ValueError):
            self.box.prepare("event-1", "New task: beta\nPreserve exact ID B-18.", self.route, "alpha", updated,
                target_packet={"task_key":"beta", "state":{"goal":"Build beta", "requirements":["ID B-18"]}})


if __name__ == "__main__":
    unittest.main()
