"""Pair foreground/background lanes over a shared, supervised model process."""
import threading
from collections import Counter


class SharedModels:
    def __init__(self,factory,max_lanes=2,*,budget=None):
        if not 2<=max_lanes<=8:
            raise ValueError("Shared runtime lane bound")
        self.factory=factory
        self.budget=budget
        self.tickets={}
        self.max_lanes=max_lanes
        self.lock=threading.Condition()
        self.engines={}
        self.loading=0
        self.closed=False

    def borrow(self,role):
        if role not in {"foreground","background"}:
            raise ValueError("Unknown inference role")
        with self.lock:
            while True:
                if self.closed:
                    raise ValueError("Shared models closed")
                engine=next((model for model,roles in self.engines.items()
                             if roles[role]<max(1,self.max_lanes//2) and sum(roles.values())<self.max_lanes and model.process.poll() is None),None)
                if engine is None and sum(roles[role] for model,roles in self.engines.items() if model.process.poll() is None)>=2:
                    spare=[model for model,roles in self.engines.items() if sum(roles.values())<self.max_lanes and model.process.poll() is None]
                    engine=min(spare,key=lambda model:sum(self.engines[model].values()),default=None)
                if engine is not None:
                    self.engines[engine][role]+=1
                    return Lane(self,engine,role)
                if not self.loading:
                    self.loading+=1
                    break
                self.lock.wait()
        engine=None
        ticket=None
        try:
            if self.budget is not None:
                ticket=self.budget.reserve_model()
                if ticket is None:
                    return None
            engine=self.factory()
            with self.lock:
                if self.closed:
                    raise ValueError("Shared models closed during load")
                if self.budget is not None:
                    self.budget.model_started(ticket,getattr(engine.process,"pid",None))
                    self.tickets[engine]=ticket
                self.engines[engine]=Counter({role:1})
            return Lane(self,engine,role)
        except Exception:
            if engine is not None:
                engine.close()
            if self.budget is not None and ticket is not None:
                self.budget.release_model(ticket)
            raise
        finally:
            with self.lock:
                self.loading-=1
                self.lock.notify_all()

    def release(self,lane):
        closing=None
        ticket=None
        with self.lock:
            roles=self.engines.get(lane.engine)
            if roles is not None:
                roles[lane.role]-=1
                if not roles[lane.role]:
                    roles.pop(lane.role)
                if not roles:
                    self.engines.pop(lane.engine)
                    ticket=self.tickets.pop(lane.engine,None)
                    closing=lane.engine
        if closing:
            try:
                closing.close()
            finally:
                if ticket is not None:
                    self.budget.release_model(ticket)

    def state(self):
        with self.lock:
            return {"processes":len(self.engines),"loading":self.loading,"execution_lanes":sum(sum(r.values()) for r in self.engines.values()),
                    "max_parallel_per_process":self.max_lanes,"model_weights_shared_between_roles":True}

    def close(self):
        with self.lock:
            self.closed=True
            self.lock.notify_all()
            self.lock.wait_for(lambda:not self.loading)
            engines=list(self.engines)
            tickets=dict(self.tickets)
            self.engines.clear()
            self.tickets.clear()
        for engine in engines:
            try:
                engine.close()
            finally:
                if engine in tickets:
                    self.budget.release_model(tickets[engine])


class Lane:
    def __init__(self,owner,engine,role):
        self.owner=owner
        self.engine=engine
        self.role=role
        self.identity=engine.identity
        self.reranker_identity=engine.reranker_identity
        self.process=engine.process
        self.closed=False

    def query(self,value):
        return self.engine.query(value)

    def passages(self,value):
        return self.engine.passages(value)

    def rerank(self,value):
        return self.engine.rerank(value)

    def close(self):
        if not self.closed:
            self.closed=True
            self.owner.release(self)
