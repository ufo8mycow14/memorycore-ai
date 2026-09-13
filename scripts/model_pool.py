"""Recoverable foreground/background model pools sharing one resource budget."""
import threading
import time
from collections import Counter


class ModelAdmissionTimeout(TimeoutError):
    """Payload-free resource state captured at the failed admission, not later."""
    def __init__(self, state, idle):
        super().__init__("Model admission deadline")
        self.diagnostic={"reason":"admission_timeout", "idle_models":idle,
                         "resource_budget":state}


class ModelPool:
    def __init__(self, factory, budget, role, minimum=2, *, monitor=True, idle_seconds=20, shared=None):
        if shared is not None and shared.budget is not budget:
            raise ValueError("Shared runtime must own the same resource budget")
        self.factory = factory
        self.shared = shared
        self.budget = budget
        self.role = role
        self.minimum = minimum
        self.idle_seconds = idle_seconds
        self.models = {}
        self.idle = {}
        self.identity = None
        self.reranker_identity = None
        self.loading = 0
        self.lock = threading.Condition()
        self.stopped = threading.Event()
        self.demand = threading.Event()
        self.retry_after = 0
        self.metrics = Counter()
        self.controller = None
        for _ in range(minimum):
            if not self.grow():
                break
        if monitor:
            self.controller = threading.Thread(target=self.control, daemon=True)
            self.controller.start()

    def grow(self):
        with self.lock:
            if self.stopped.is_set() or self.loading or time.monotonic() < self.retry_after:
                return False
            self.loading += 1
        ticket = self.budget.reserve_model() if self.shared is None else None
        if ticket is None and self.shared is None:
            with self.lock:
                self.metrics["growth_denied"]+=1
                for reason in self.budget.current().get("model_growth_blockers",[]):
                    self.metrics["growth_denied_"+reason]+=1
                self.loading -= 1
                self.lock.notify_all()
            return False
        model = None
        try:
            model = self.shared.borrow(self.role) if self.shared is not None else self.factory()
            if model is None:
                with self.lock:
                    self.metrics["growth_denied"]+=1
                    for reason in self.budget.current().get("model_growth_blockers",[]):
                        self.metrics["growth_denied_"+reason]+=1
                return False
            if self.shared is not None:
                ticket=id(model)
            with self.lock:
                if self.stopped.is_set() or self.identity not in {None, model.identity}:
                    raise ValueError("Model stopped or identity changed")
                reranker = getattr(model, "reranker_identity", None)
                if self.reranker_identity not in {None, reranker}:
                    raise ValueError("Reranker identity changed")
                process = getattr(model, "process", None)
                if self.shared is None:
                    self.budget.model_started(ticket, getattr(process, "pid", None))
                self.identity = model.identity
                self.reranker_identity = reranker
                self.models[ticket] = model
                self.idle[ticket] = time.monotonic()
                self.metrics["started"] += 1
                self.lock.notify_all()
            return True
        except Exception:
            if model is not None:
                getattr(model, "close", lambda: None)()
            if self.shared is None:
                self.budget.release_model(ticket)
            with self.lock:
                self.retry_after = time.monotonic()+2
                self.metrics["start_failed"] += 1
            return False
        finally:
            with self.lock:
                self.loading -= 1
                self.lock.notify_all()

    def state(self):
        with self.lock:
            return {"resident": len(self.models), "idle": len(self.idle),
                    "minimum": self.minimum, "minimum_met": len(self.models) >= self.minimum,
                    "metrics": dict(self.metrics)}

    def tick(self):
        state = self.budget.current()
        retired = None
        with self.lock:
            ceiling = max(self.minimum, state["inference_limit"]-(2 if self.role == "background" else 0))
            if self.budget.adaptive_priority and self.role=="foreground" and state.get("waiting",{}).get("background",0):
                queued_foreground=state.get("waiting",{}).get("foreground",0)>0
                reserved=1 if queued_foreground and state["inference_limit"]>1 else min(2,max(1,state["inference_limit"]//2))
                ceiling=max(self.minimum,state["inference_limit"]-reserved)
            if len(self.models) >= ceiling:
                self.demand.clear()
            if self.idle:
                dead=next((ticket for ticket in self.idle
                           if getattr(self.models[ticket],"process",None) is not None
                           and self.models[ticket].process.poll() is not None),None)
                ticket = dead if dead is not None else min(self.idle, key=self.idle.get)
                expired = time.monotonic()-self.idle[ticket] >= self.idle_seconds
                if dead is not None or state["memory_pressure"] or (len(self.models) > self.minimum and expired
                                                 and not self.demand.is_set()):
                    self.idle.pop(ticket)
                    retired = (ticket, self.models.pop(ticket))
                    if dead is not None:
                        self.metrics["dead_idle_retired"] += 1
            grow = (len(self.models) < self.minimum or (self.demand.is_set() and len(self.models)<ceiling)) and not state["pressured"]
        if retired:
            ticket, model = retired
            getattr(model, "close", lambda: None)()
            if self.shared is None:
                self.budget.release_model(ticket)
        if grow and self.grow():
            self.demand.clear()

    def control(self):
        while not self.stopped.wait(.5):
            try:
                self.tick()
            except Exception:
                with self.lock:
                    self.metrics["controller_error"] += 1

    def invoke(self, method, value):
        deadline = time.monotonic() + (.1 if self.role == "foreground" else .05)
        self.budget.queue(self.role,1)
        try:
            with self.lock:
                while True:
                    if self.stopped.is_set():
                        raise ValueError("Model pool closed")
                    if self.idle and self.budget.acquire(self.role):
                        ticket = next(iter(self.idle))
                        self.idle.pop(ticket)
                        model = self.models[ticket]
                        break
                    if not self.idle:
                        self.demand.set()
                    if time.monotonic() >= deadline:
                        state=self.budget.current()
                        self.metrics["resource_limited" if state["pressured"] else "busy"] += 1
                        self.metrics["admission_timeout_"+method]+=1
                        for reason in ("memory_pressure","host_cpu_pressure","system_cpu_pressure"):
                            if state.get(reason):
                                self.metrics[reason]+=1
                        raise ModelAdmissionTimeout(state,len(self.idle))
                    self.lock.wait(.005)
        finally:
            self.budget.queue(self.role,-1)
        healthy = False
        try:
            result = getattr(model, method)(value)
            healthy = True
            return result
        finally:
            self.budget.release(self.role)
            with self.lock:
                if healthy:
                    self.idle[ticket] = time.monotonic()
                    self.metrics["completed"] += 1
                else:
                    self.models.pop(ticket, None)
                    self.metrics["failed"] += 1
                    self.demand.set()
                self.lock.notify_all()
            if not healthy:
                getattr(model, "close", lambda: None)()
                if self.shared is None:
                    self.budget.release_model(ticket)

    def query(self, value):
        return self.invoke("query", value)

    def passages(self, value):
        return self.invoke("passages", value)

    def rerank(self, value):
        return self.invoke("rerank", value)

    def close(self):
        self.stopped.set()
        with self.lock:
            self.lock.notify_all()
        if self.controller:
            self.controller.join()
        with self.lock:
            self.lock.wait_for(lambda: not self.loading and len(self.idle) == len(self.models))
            models = list(self.models.items())
            self.models.clear()
            self.idle.clear()
        for ticket, model in models:
            getattr(model, "close", lambda: None)()
            if self.shared is None:
                self.budget.release_model(ticket)
