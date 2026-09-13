"""Local session planning and recoverable synthetic transition outbox.

No hooks or application changes are installed. Production dispatch requires a
host that can prove placement, idle-state atomicity and idempotent delivery.
"""
from dataclasses import dataclass, asdict
import argparse
import json
import re
import time

from . import memorycore_ai as bm
from .knowledge_layer import checked, canonical, digest, source_binding
from .memory_policy import check_text, bounded_integer
from .memory_packets import token_counter
from .fresh_context import selected_context, delivery_request

STATE_FIELDS = ("goal", "requirements", "decisions", "constraints", "progress", "dependencies", "unresolved", "unfinished", "references")


@dataclass(frozen=True)
class Placement:
    host: str
    project_id: str | None
    surface: str  # project_chat or ordinary_chat; a working directory is not a project identity.

    def __post_init__(self):
        if not isinstance(self.host, str) or not self.host.strip() or self.surface not in {"project_chat", "ordinary_chat"}:
            raise ValueError("invalid placement")
        if self.project_id is not None and (not isinstance(self.project_id, str) or not self.project_id.strip()):
            raise ValueError("invalid project identity")
        if (self.project_id is None) != (self.surface == "ordinary_chat"):
            raise ValueError("project identity and surface disagree")
        checked(asdict(self))


@dataclass(frozen=True)
class Session:
    id: str
    task_key: str
    placement: Placement
    history_tokens: int
    status: str = "unknown"
    revision: str = ""
    observed_at: float = 0
    turns_since_transition: int = 0

    def __post_init__(self):
        if not isinstance(self.placement, Placement) or not self.id or not self.task_key or self.status not in {"idle", "running", "unknown"}:
            raise ValueError("invalid session")
        checked(asdict(self))
        bounded_integer(self.history_tokens, "history tokens", minimum=0, maximum=10_000_000)
        bounded_integer(self.turns_since_transition, "turns", minimum=0, maximum=1_000_000)


@dataclass(frozen=True)
class Boundary:
    task_key: str
    meaningful_change: bool = False
    returning: bool = False
    related: bool = False
    tangent: bool = False
    needs_current_history: bool = True
    selective_context_complete: bool = False
    keep_here: bool = False
    explicit_fresh: bool = False

    def __post_init__(self):
        check_text(self.task_key, maximum=128)
        if not self.task_key.strip() or any(type(value) is not bool for key,value in asdict(self).items() if key != "task_key"):
            raise ValueError("invalid boundary evidence")


def detect_boundary(message, current_task, known_tasks):
    """Conservative deterministic recognition of explicit task markers.

    Unmarked paraphrases, length and low word overlap never establish a split.
    The host may supply richer structured Boundary evidence independently.
    """
    check_text(message, maximum=65536)
    if re.search(r"\b(?:keep (?:this|it) (?:here|in (?:this|the current) chat)|stay in this chat)\b", message, re.I):
        return Boundary(current_task, keep_here=True)
    match = re.match(r"\s*(New task|Return to task|Resume task):\s*([a-zA-Z0-9_.-]{1,80})(?:\s|$)", message)
    if match:
        key = match[2]
        returning = match[1] != "New task"
        if returning and key not in known_tasks:
            return Boundary(current_task)  # unresolved reference is not a new task.
        return Boundary(key, meaningful_change=key != current_task, returning=returning, needs_current_history=False)
    return Boundary(current_task, related=True)


@dataclass(frozen=True)
class Costs:
    remaining_turns: int
    fresh_context_tokens: int
    classification_tokens: int = 0
    memory_write_tokens: int = 0
    retrieval_tokens: int = 0
    decoding_tokens: int = 0
    retry_rework_tokens: int = 0
    extra_output_tokens: int = 0
    uncertainty_tokens: int = 0
    continue_retrieval_tokens: int = 0
    fresh_verification_tokens: int = 0
    evidence: str = "estimate"

    def __post_init__(self):
        bounded_integer(self.remaining_turns, "remaining turns", maximum=100)
        for key, value in asdict(self).items():
            if key not in {"remaining_turns", "evidence"}:
                bounded_integer(value, key, minimum=0, maximum=10_000_000)
        if self.evidence not in {"estimate", "calibrated_observation"}:
            raise ValueError("invalid cost evidence")

    def transition_overhead(self):
        return sum(value for key, value in asdict(self).items() if key not in {"remaining_turns", "fresh_context_tokens", "evidence", "continue_retrieval_tokens", "fresh_verification_tokens"})


def plan(current, boundary, sessions, costs, *, desired_placement=None, placement_authorised=False, now=None):
    now = time.time() if now is None else now
    target_placement = desired_placement or current.placement
    base = {"action": "continue", "source": current.id, "target": current.id,
            "placement": asdict(current.placement), "reason": "continuity", "cost_evidence": costs.evidence}
    def stay(reason):
        return {**base, "reason": reason}
    if boundary.keep_here:
        return stay("user_keep_here")
    if target_placement != current.placement:
        return stay("automatic_transition_preserves_exact_placement")
    if current.status != "idle" or not current.revision or not 0 <= now-current.observed_at <= 30:
        return stay("active_or_unverified_operations")
    if boundary.tangent or boundary.related:
        return stay("related_work_or_tangent")
    if boundary.needs_current_history or not boundary.selective_context_complete:
        return stay("required_context_not_yet_portable")
    if not (boundary.meaningful_change or boundary.returning or boundary.explicit_fresh):
        return stay("no_meaningful_boundary")
    if current.turns_since_transition < 3 and not (boundary.returning or boundary.explicit_fresh):
        return stay("fragmentation_cooldown")
    overhead = costs.transition_overhead()
    estimates = {"continue": (current.history_tokens + costs.continue_retrieval_tokens) * costs.remaining_turns}
    candidates = []
    for session in sessions:
        if session.id == current.id or session.task_key != boundary.task_key or session.placement != target_placement:
            continue
        if session.status != "idle" or not session.revision or not 0 <= now-session.observed_at <= 30:
            continue
        cost = session.history_tokens * costs.remaining_turns + overhead
        estimates["resume:" + session.id] = cost
        candidates.append((cost, "resume", session.id))
    # Starting a history-free conversation for the same task needs explicit
    # direction or sufficiently stronger cost evidence than resuming it.
    fresh = costs.fresh_context_tokens * costs.remaining_turns + overhead + costs.fresh_verification_tokens
    estimates["fresh"] = fresh
    candidates.append((fresh, "fresh", None))
    candidates.sort(key=lambda item: (item[0], item[1] != "resume", item[2] or ""))
    best_cost, action, target = candidates[0]
    margin = max(256, round(estimates["continue"] * 0.15))
    if estimates["continue"] - best_cost < margin and not boundary.explicit_fresh:
        return {**stay("transition_does_not_repay_total_cost"), "estimated_tokens": estimates}
    # Existing suitable chat wins when fresh savings are too small to justify
    # another copy of the task. This also prevents fragmentation on tiny ties.
    resumes = [c for c in candidates if c[1] == "resume"]
    if action == "fresh" and resumes and not boundary.explicit_fresh:
        resumed = resumes[0]
        if resumed[0] - best_cost < max(256, round(resumed[0] * 0.15)):
            best_cost, action, target = resumed
    if boundary.explicit_fresh:
        action, target, best_cost = "fresh", None, fresh
    return {**base, "action": action, "target": target, "placement": asdict(target_placement),
            "task_key": boundary.task_key, "reason": "explicit_fresh" if boundary.explicit_fresh else "meaningful_boundary_with_cost_advantage",
            "estimated_tokens": estimates, "estimated_selected_tokens": best_cost,
            "source_revision": current.revision, "source_observed_at": current.observed_at,
            "source_placement": asdict(current.placement), "placement_authorised": placement_authorised is True,
            "target_revision": next((s.revision for s in sessions if s.id == target), None)}


def validate_state(state):
    state = checked(state)
    if not isinstance(state, dict) or set(state) != set(STATE_FIELDS):
        raise ValueError("handoff must explicitly cover all continuity categories")
    if not isinstance(state["goal"], str) or not state["goal"].strip():
        raise ValueError("handoff needs a goal")
    for category in STATE_FIELDS[1:]:
        if not isinstance(state[category], list) or len(state[category]) > 100:
            raise ValueError("invalid handoff category")
        for item in state[category]:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("handoff values must be complete strings")
    return state


def save_checkpoint(knowledge, task_key, source_session, state, *, source_paths=()):
    state = validate_state(state)
    checked({"task_key": task_key, "source_session": source_session})
    bindings = [source_binding(path, knowledge.sources.read(path)) for path in source_paths]
    package = {"format": "session-checkpoint/1", "scope": knowledge.scope, "task_key": task_key,
               "source_session": source_session, "state": state, "source_bindings": bindings}
    with bm.transaction(knowledge.conn):
        subject = "Session state: " + task_key
        previous = [item for item in knowledge._candidates() if item["subject"] == subject]
        if len(previous) > 1:
            raise ValueError("ambiguous checkpoint head requires review")
        args = bm.build_parser().parse_args(["remember", "--scope", knowledge.scope, "--type", "semantic", "--subject", "Session state: " + task_key,
            "--summary", state["goal"], "--detail", canonical(state), "--source", source_session, "--keywords", task_key])
        args.supersedes = previous[0]["memory_id"] if previous else None
        saved = bm.remember(knowledge.conn, args)
        exact = bm.build_parser().parse_args(["store-exact", "--scope", knowledge.scope, "--text", canonical(package), "--source", source_session,
            "--media-type", "application/json", "--user-confirmed", "--linked-memory-id", saved["memory_id"]])
        receipt = bm.store_exact(knowledge.conn, exact)
        return {"memory_id": saved["memory_id"], "archive_id": receipt["archive_id"], "sha256": digest(package)}


def load_checkpoint(knowledge, receipt, *, categories=None, max_tokens=1400, count=None):
    categories = list(STATE_FIELDS) if categories is None else categories
    if not set(categories).issubset(STATE_FIELDS) or len(set(categories)) != len(categories):
        raise ValueError("unknown or duplicate continuity categories")
    count = count or token_counter()
    args = argparse.Namespace(archive_id=receipt["archive_id"], scope=knowledge.scope, offset=0, length=None)
    package = json.loads(bm.recall_exact(knowledge.conn, args))
    if digest(package) != receipt["sha256"] or package["scope"] != knowledge.scope:
        raise ValueError("checkpoint receipt mismatch")
    head = knowledge.conn.execute("SELECT * FROM cortex_memory WHERE scope=? AND memory_id=? AND status=0 AND (expires_at IS NULL OR expires_at>?)",
        (knowledge.scope, bm.parse_id(receipt["memory_id"]), bm.now_utc())).fetchone()
    exact = knowledge.conn.execute("SELECT linked_memory_id FROM cortex_verbatim WHERE scope=? AND archive_id=?",
        (knowledge.scope, bm.parse_id(receipt["archive_id"]))).fetchone()
    if not head or not exact or exact[0] != head["memory_id"]:
        raise ValueError("checkpoint is no longer the current active version")
    bm.verify_record(head)
    bindings = package.get("source_bindings", [])
    if any(knowledge.sources.inspect(binding) != "fresh" for binding in bindings):
        raise ValueError("checkpoint supporting source changed or is unavailable")
    validate_state(package["state"])
    selected = {key:package["state"][key] for key in categories}
    result = {"task_key": package["task_key"], "data_only": True, "state": selected,
              "unloaded_categories": [k for k in STATE_FIELDS if k not in categories], "source_material_still_authoritative": True,
              "source_status": "fresh" if bindings else "unverified", "source_bindings": bindings}
    if count(canonical(result)) > max_tokens:
        # Never truncate a constraint or an unfinished task to make a split fit.
        raise ValueError("required transition context exceeds budget; continue or select fewer independent categories")
    return result


@dataclass(frozen=True)
class Capabilities:
    capture_pending_message: bool = False
    project_chat_placement: bool = False
    ordinary_chat_placement: bool = False
    resume: bool = False
    atomic_idle_guard: bool = False
    idempotent_delivery: bool = False
    reconcile_delivery: bool = False
    fresh_history_isolation: bool = False

    def __post_init__(self):
        if any(type(value) is not bool for value in asdict(self).values()):
            raise ValueError("capabilities require verified boolean values")

    def supports(self, route):
        placement = route["placement"]
        target_ok = self.project_chat_placement if placement["project_id"] is not None else self.ordinary_chat_placement
        return (self.capture_pending_message and target_ok and self.atomic_idle_guard and self.idempotent_delivery
                and self.reconcile_delivery and (route["action"] != "resume" or self.resume)
                and (route["action"] != "fresh" or self.fresh_history_isolation))


class RouteOutbox:
    """No external action until checkpoint+pending message have committed.

    Hosts must provide their own verified gateway. Missing capabilities yield a
    handoff; a topic classifier cannot grant placement or delivery capabilities.
    """
    def __init__(self, knowledge, *, create=False):
        self.k, self.conn = knowledge, knowledge.conn
        if create:
            with bm.transaction(self.conn):
                self.conn.execute("""CREATE TABLE IF NOT EXISTS session_route (
                    scope TEXT NOT NULL, message_id TEXT NOT NULL, payload TEXT NOT NULL, checksum TEXT NOT NULL,
                    PRIMARY KEY(scope,message_id))""")

    def _read(self, message_id):
        row = self.conn.execute("SELECT * FROM session_route WHERE scope=? AND message_id=?", (self.k.scope, message_id)).fetchone()
        if not row:
            raise ValueError("route intent not found")
        item = json.loads(row["payload"])
        if digest(item) != row["checksum"] or item["scope"] != self.k.scope or item["message_id"] != message_id:
            raise ValueError("route intent integrity mismatch")
        checked(item)
        return item

    def _write(self, item):
        checked(item)
        self.conn.execute("INSERT INTO session_route VALUES(?,?,?,?) ON CONFLICT(scope,message_id) DO UPDATE SET payload=excluded.payload,checksum=excluded.checksum",
                          (self.k.scope, item["message_id"], canonical(item), digest(item)))

    def prepare(self, message_id, message, route, current_task, state, *, target_packet):
        check_text(message_id, maximum=128)
        check_text(message, maximum=65536)
        if not message_id or not message or route["action"] not in {"fresh", "resume"}:
            raise ValueError("only real transition proposals enter the outbox")
        checked(route)
        selected_context(target_packet, route["task_key"])
        placement, source = Placement(**route["placement"]), Placement(**route["source_placement"])
        if placement != source:
            raise ValueError("automatic routing must preserve exact project placement")
        if not route.get("source") or not route.get("source_revision"):
            raise ValueError("source identity and revision are required")
        if route["action"] == "resume" and (not route.get("target") or route["target"] == route["source"] or not route.get("target_revision")):
            raise ValueError("resumption requires a distinct verified target")
        if route["action"] == "fresh" and route.get("target") is not None:
            raise ValueError("fresh routes cannot name an existing target")
        signature = digest({"message": message, "route": route, "target_packet": target_packet,
                            "current_task": current_task, "state": validate_state(state)})
        with bm.transaction(self.conn):
            exists = self.conn.execute("SELECT 1 FROM session_route WHERE scope=? AND message_id=?", (self.k.scope, message_id)).fetchone()
            if exists:
                item = self._read(message_id)
                if item["signature"] != signature:
                    raise ValueError("message ID reused for different routing content")
                return item
            checkpoint = save_checkpoint(self.k, current_task, route["source"], state)
            item = {"scope": self.k.scope, "message_id": message_id, "signature": signature, "message": message,
                    "message_sha256": digest(message), "route": route, "checkpoint": checkpoint,
                    "target_packet": target_packet, "state": "PREPARED", "attempts": 0, "receipt": None}
            self._write(item)
            return item

    def handoff(self, message_id):
        item = self._read(message_id)
        action = "Preserve the current conversation. Use a verified thread/start destination in the exact same project or ordinary-chat surface for fresh routing; resume remains a separate operation. Deliver the exact pending message once."
        if item["state"] in {"DISPATCHING", "RECONCILE"}:
            action = "Delivery is uncertain. Reconcile before any resend or destination change."
        elif item["state"] == "DELIVERED":
            action = "Already delivered. Open the confirmed destination; do not resend."
        elif item["state"] == "CANCELLED":
            action = "Routing was cancelled. Continue in the original task."
        return {"occurred": item["state"] == "DELIVERED", "state": item["state"], "target_placement": item["route"]["placement"],
                "target_session": item["receipt"]["destination_id"] if item["state"] == "DELIVERED" else item["route"]["target"], "checkpoint": item["checkpoint"],
                "pending_message": item["message"], "selective_context": item["target_packet"],
                "source_preserved": True,
                "limitation": "Automatic delivery requires verified placement, history isolation and reliable pending-message delivery; no fork or copied-history fallback is allowed.",
                "user_action": action}

    def cancel(self, message_id):
        with bm.transaction(self.conn):
            item = self._read(message_id)
            if item["state"] not in {"PREPARED", "HANDOFF"}:
                raise ValueError("reconcile any dispatched transition before changing its destination")
            item["state"] = "CANCELLED"
            self._write(item)
        return {"continue_original": True, "message": item["message"], "source_preserved": True}

    def dispatch(self, message_id, gateway):
        with bm.transaction(self.conn):
            item = self._read(message_id)
            if item["state"] == "DELIVERED":
                return item["receipt"]
            if item["state"] in {"DISPATCHING", "RECONCILE"}:
                raise ValueError("reconcile uncertain delivery before another attempt")
            if item["state"] == "CANCELLED":
                raise ValueError("routing was cancelled")
            if not gateway.capabilities.supports(item["route"]):
                item["state"] = "HANDOFF"
                self._write(item)
                return self.handoff(message_id)
            # Verify durable source state can be decoded before touching a chat.
            load_checkpoint(self.k, item["checkpoint"], max_tokens=8000)
            observation = gateway.observe(item["route"]["source"])
            if observation != {"status": "idle", "revision": item["route"]["source_revision"]}:
                raise ValueError("source has active or changed work")
            if item["route"]["action"] == "resume":
                target = gateway.observe(item["route"]["target"])
                if target != {"status": "idle", "revision": item["route"]["target_revision"]}:
                    raise ValueError("target has active or changed work")
            item["state"] = "DISPATCHING"
            item["attempts"] += 1
            self._write(item)
        try:
            # The gateway must atomically repeat the idle check and deduplicate
            # globally by scope+message ID, not just within the destination.
            receipt = gateway.deliver_once(delivery_request(item))
            return self._accept_receipt(message_id, receipt)
        except BaseException:
            with bm.transaction(self.conn):
                latest = self._read(message_id)
                if latest["state"] != "DELIVERED":
                    latest["state"] = "RECONCILE"
                    self._write(latest)
            raise

    def _accept_receipt(self, message_id, receipt):
        receipt = checked(receipt)
        with bm.transaction(self.conn):
            item = self._read(message_id)
            if item["state"] == "DELIVERED":
                if item["receipt"] != receipt:
                    raise ValueError("conflicting delivery acknowledgement")
                return item["receipt"]
            if item["state"] not in {"DISPATCHING", "RECONCILE"}:
                raise ValueError("no uncertain delivery to acknowledge")
            if (receipt.get("message_id") != message_id or receipt.get("scope") != self.k.scope
                    or receipt.get("message_sha256") != item["message_sha256"] or receipt.get("delivered") is not True
                    or receipt.get("placement") != item["route"]["placement"] or receipt.get("source_preserved") is not True
                    or not receipt.get("destination_id")):
                raise ValueError("delivery acknowledgement failed verification")
            if item["route"]["action"] == "resume" and receipt["destination_id"] != item["route"]["target"]:
                raise ValueError("message reached the wrong resumed session")
            if item["route"]["action"] == "fresh" and (
                    receipt["destination_id"] == item["route"]["source"]
                    or receipt.get("operation") != "thread/start"
                    or receipt.get("history_imported") is not False):
                raise ValueError("fresh delivery lacks verified history isolation")
            item["state"], item["receipt"] = "DELIVERED", receipt
            self._write(item)
            return receipt

    def reconcile(self, message_id, gateway):
        with bm.transaction(self.conn, write=False):
            item = self._read(message_id)
        if item["state"] == "DELIVERED":
            return item["receipt"]
        if item["state"] not in {"DISPATCHING", "RECONCILE"} or not gateway.capabilities.reconcile_delivery:
            raise ValueError("delivery cannot be reconciled by this gateway")
        receipt = gateway.lookup(self.k.scope, message_id)
        if receipt is not None:
            return self._accept_receipt(message_id, receipt)
        # A confirmed absence plus a global idempotency contract permits another
        # attempt; lookup exceptions/timeouts never reset the outbox state.
        if not gateway.capabilities.idempotent_delivery:
            raise ValueError("absence does not make retry safe")
        with bm.transaction(self.conn):
            latest = self._read(message_id)
            if latest["state"] == "DELIVERED":
                return latest["receipt"]
            if latest != item:
                raise ValueError("delivery state changed during reconciliation")
            latest["state"] = "PREPARED"
            self._write(latest)
        return {"delivered": False, "retry_safe": True}

    def export(self):
        with bm.transaction(self.conn, write=False):
            ids = [r[0] for r in self.conn.execute("SELECT message_id FROM session_route WHERE scope=? ORDER BY message_id", (self.k.scope,))]
            if len(ids) > 2000:
                raise ValueError("routing export exceeds supported size")
            result = {"format": "session-routing-audit/1", "knowledge": self.k.export(_allow_routing=True),
                      "intents": [self._read(mid) for mid in ids], "automatic_delivery_restore": False}
            return {**result, "sha256": digest(result)}
