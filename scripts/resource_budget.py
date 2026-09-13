"""Shared admission and model reservations; OS containment is reported separately."""
import math
import os
import threading
import time
import psutil


def sqlite_cache_budget(memory_limit, readers):
    connections=2*readers+2  # Reader/warmer pairs, writer and checkpoint connection.
    return max(2*1024**2,min(16*1024**2,memory_limit//50//connections))


def evaluate(snapshot, cpu_percent=50, memory_percent=15, free_percent=15,
             model_bytes=192*1024*1024):
    limit = int(snapshot["total"] * memory_percent / 100)
    reserve = max(1024*1024*1024, int(snapshot["total"] * free_percent / 100))
    resident = max(snapshot["rss"],snapshot.get("private_bytes") or 0)
    memory_pressure = resident >= limit or snapshot["available"] < reserve
    cpu_pressure = snapshot["system_cpu"] >= 80 or snapshot["host_cpu"] >= cpu_percent
    slots = max(1, math.floor(snapshot["cpus"] * cpu_percent / 100))
    return {
        "pressured": memory_pressure or cpu_pressure,
        "memory_pressure": memory_pressure,
        "cpu_pressure":cpu_pressure,
        "host_cpu_pressure":snapshot["host_cpu"]>=cpu_percent,
        "system_cpu_pressure":snapshot["system_cpu"]>=80,
        "system_cpu_percent":snapshot["system_cpu"],
        "host_cpu_percent":snapshot["host_cpu"],
        "can_grow": not (memory_pressure or cpu_pressure)
        and resident + model_bytes <= limit
        and snapshot["available"] - model_bytes >= reserve,
        "worker_ceiling": max(4, min(32, slots * 2)),
        "inference_limit": slots,
        "inference_cpu_percent": cpu_percent * .8,
        "memory_limit_bytes": limit,
        "free_reserve_bytes": reserve,
    }


class ResourceBudget:
    def __init__(self, cpu_percent=50, memory_percent=15, free_percent=15,
                 snapshot_provider=None, monitor=True, adaptive_priority=True):
        if not 10 <= cpu_percent <= 80 or not 5 <= memory_percent <= 40 or not 10 <= free_percent <= 50:
            raise ValueError("Invalid resource budget")
        self.cpu_percent = cpu_percent
        self.memory_percent = memory_percent
        self.free_percent = free_percent
        # The adaptive policy is the default; a fixed-policy switch keeps
        # matched synthetic comparisons reproducible without changing safety
        # limits or durable storage behaviour.
        self.adaptive_priority = bool(adaptive_priority)
        self.model_bytes = 192*1024*1024
        self.lock = threading.RLock()
        self.previous = {}
        self.clock = time.perf_counter()
        self.last_cpu={"system_cpu":0.0,"host_cpu":0.0}
        self.snapshot_provider = snapshot_provider
        self.snapshot = None
        self.models = {}
        self.active = {"foreground": 0, "background": 0}
        self.waiting = {"foreground": 0, "background": 0}
        self.last_role = None
        self.sequence = 0
        self.stop = threading.Event()
        self.sample()
        self.monitor = None
        if monitor:
            self.monitor = threading.Thread(target=self.watch, daemon=True)
            self.monitor.start()

    def watch(self):
        while not self.stop.wait(.5):
            self.sample()

    def sample(self):
        with self.lock:
            try:
                if self.snapshot_provider:
                    snapshot = self.snapshot_provider()
                else:
                    root = psutil.Process(os.getpid())
                    now = time.perf_counter()
                    current = {}
                    rss = private = delta = 0
                    private_known=True
                    for process in [root] + root.children(recursive=True):
                        try:
                            times = process.cpu_times()
                            key = (process.pid, process.create_time())
                            cpu = times.user + times.system
                            current[key] = cpu
                            delta += max(0, cpu - self.previous.get(key, cpu))
                            info=process.memory_info()
                            rss += info.rss
                            committed=getattr(info,"private",None)
                            private_known=private_known and committed is not None
                            private+=committed or 0
                        except psutil.Error:
                            pass
                    try:
                        cpus = len(root.cpu_affinity())
                    except (AttributeError, psutil.Error):
                        cpus = psutil.cpu_count() or 1
                    memory = psutil.virtual_memory()
                    # CPU counters have coarse resolution; memory-only refreshes must not
                    # turn one scheduler tick into an apparent machine-wide CPU spike.
                    if now-self.clock>=.25:
                        self.last_cpu={"system_cpu":psutil.cpu_percent(),
                            "host_cpu":100*delta/(now-self.clock)/max(1,cpus)}
                        self.previous=current
                        self.clock=now
                    snapshot = {"total": memory.total, "available": memory.available,
                                "rss": rss, "private_bytes":private if private_known else None,"cpus": max(1, cpus),
                                **self.last_cpu}
                self.snapshot = dict(snapshot)
                self.state = evaluate(snapshot, self.cpu_percent, self.memory_percent,
                                      self.free_percent, self.model_bytes)
            except (psutil.Error, OSError):
                self.state = {"pressured": True, "memory_pressure": False, "can_grow": False,
                              "worker_ceiling": 4, "inference_limit": 1}
            return self.current()

    def current(self):
        with self.lock:
            pending=sum(v or 0 for v in self.models.values())
            snapshot=self.snapshot or {}
            resident=max(snapshot.get("rss",0),snapshot.get("private_bytes") or 0)
            needed=pending+self.model_bytes
            blockers=[]
            if self.state.get("pressured"):
                blockers.append("resource_pressure")
            if resident+needed>self.state.get("memory_limit_bytes",0):
                blockers.append("host_memory_headroom")
            if snapshot.get("available",0)-needed<self.state.get("free_reserve_bytes",0):
                blockers.append("system_free_headroom")
            if len(self.models)>=self.state.get("worker_ceiling",0):
                blockers.append("worker_ceiling")
            return dict(self.state, adaptive_priority=self.adaptive_priority,
                        active=dict(self.active), waiting=dict(self.waiting), resident_models=len(self.models),
                        loading_models=sum(v is not None for v in self.models.values()),
                        model_reservation_bytes=self.model_bytes,pending_model_bytes=pending,
                        observed_resident_bytes=resident,available_memory_bytes=snapshot.get("available"),
                        model_growth_blockers=blockers)

    def reserve_model(self):
        with self.lock:
            self.sample()
            if not self.state["can_grow"] or len(self.models) >= self.state["worker_ceiling"]:
                return None
            pending = sum(v or 0 for v in self.models.values())
            needed = pending + self.model_bytes
            if (max(self.snapshot["rss"],self.snapshot.get("private_bytes") or 0) + needed > self.state["memory_limit_bytes"]
                    or self.snapshot["available"] - needed < self.state["free_reserve_bytes"]):
                return None
            self.sequence += 1
            self.models[self.sequence] = self.model_bytes
            return self.sequence

    def model_started(self, ticket, pid=None):
        with self.lock:
            if pid is not None:
                try:
                    self.model_bytes = max(self.model_bytes, int(psutil.Process(pid).memory_info().rss*1.3))
                except psutil.Error:
                    pass
            # Refresh observed RSS before releasing the pending-load reservation.
            self.sample()
            self.models[ticket] = None

    def release_model(self, ticket):
        with self.lock:
            self.models.pop(ticket, None)

    def acquire(self, role):
        with self.lock:
            if self.stop.is_set() or self.state["pressured"]:
                return False
            limit = self.state["inference_limit"]
            if sum(self.active.values()) >= limit:
                return False
            if role=="foreground" and self.waiting["background"]:
                if self.adaptive_priority:
                    # Preserve a background share when there is no interactive
                    # queue, but give queued foreground work one extra lane
                    # under contention. The background pool remains resident
                    # at its normal minimum and continues to make progress;
                    # this only changes active admission.
                    foreground_waiting = self.waiting["foreground"] > 0
                    reserved = 1 if foreground_waiting and limit > 1 else min(2,max(1,limit//2))
                else:
                    foreground_waiting = False
                    reserved = min(2,max(1,limit//2))
                if (limit==1 and self.last_role=="foreground") or (limit>1 and self.active[role]>=limit-reserved):
                    return False
            if role=="background" and limit==1 and self.waiting["foreground"] and self.last_role=="background":
                return False
            foreground_waiting = self.waiting["foreground"] > 0
            background_limit = (max(1, limit-3) if self.adaptive_priority and foreground_waiting and limit > 1
                                else max(1, limit-2))
            if role == "background" and self.active[role] >= background_limit:
                return False
            self.active[role] += 1
            self.last_role=role
            return True

    def queue(self,role,delta):
        with self.lock:
            if self.waiting[role]+delta<0:
                raise RuntimeError("Inference queue underflow")
            self.waiting[role]+=delta

    def release(self, role):
        with self.lock:
            if self.active[role] <= 0:
                raise RuntimeError("Inference permit underflow")
            self.active[role] -= 1

    def close(self):
        self.stop.set()
        if self.monitor:
            self.monitor.join(timeout=5)
