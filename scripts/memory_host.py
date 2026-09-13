"""Standard synthetic memory host: automatic local semantic enhancement, bounded work."""
import argparse
import concurrent.futures
import copy
import hashlib
import json
import math
import queue
import sys
import threading
import uuid
import os
import subprocess
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from scripts.vector_pipeline import BrokerClient, RERANKER_NAME, FAST_RERANKER_NAME, DEFAULT_RERANKER_NAME
from scripts.resource_budget import ResourceBudget, sqlite_cache_budget
from scripts.model_pool import ModelPool
from scripts.index_telemetry import IndexLatency, IndexStages
from scripts.shared_models import SharedModels
from scripts.verification_jobs import VerificationJobs
from scripts.resource_limits import InferenceLimits
from scripts.memory_policy import check_text


def unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key]=value
    return result


class ScopePolling:
    """Back off idle scopes without losing a mutation that races with a poll."""
    def __init__(self,clock=time.monotonic):
        self.clock=clock
        self.lock=threading.Lock()
        self.scopes={}

    def due(self,scope):
        with self.lock:
            next_poll,generation=self.scopes.get(scope,(0,0))
            return generation if self.clock()>=next_poll else None

    def defer(self,scope,generation,seconds=.5):
        with self.lock:
            if self.scopes.get(scope,(0,0))[1]==generation:
                self.scopes[scope]=(self.clock()+seconds,generation)

    def wake(self,scope):
        with self.lock:
            self.scopes[scope]=(0,self.scopes.get(scope,(0,0))[1]+1)


def vector_write_batches(items):
    batch=[]
    size=2
    for item in items:
        cost=len(json.dumps(item,separators=(",",":"),allow_nan=False).encode())+1
        if cost>60000:
            raise ValueError("Vector item exceeds maintenance frame budget")
        if batch and (len(batch)>=8 or size+cost>60000):
            yield batch
            batch=[]
            size=2
        batch.append(item)
        size+=cost
    if batch:
        yield batch


_READ_ADMIN_ACTIONS = {
    "export",
    "verify",
    "inspect",
    "list",
    "stats",
    "recall-exact",
    "recall",
    "knowledge-page",
    "vector-jobs",
    "vector-status",
    "vector-recall",
    "archive-retention-status",
}
_READ_CALL_ACTIONS = {
    "recall",
    "review",
    "freshness",
    "relations",
    "graph",
    "review_forget",
    "code_context",
}
_READ_ROUTING_OPERATIONS = {
    "routing-plan",
    "routing-recall",
    "routing-export",
    "routing-calibrate",
}


def interactive_request_is_write(request):
    """Return whether a host request can mutate the durable broker state.

    The Rust broker assigns read/write lanes from the same operation/action
    contract.  The host uses this predicate only to pause background index
    writes, so unknown or malformed requests fail conservatively as writes.
    """
    if not isinstance(request, dict):
        return False
    operation = request.get("operation")
    if operation in {"ping", "catalogue", *_READ_ROUTING_OPERATIONS}:
        return False
    if operation == "admin":
        outer = request.get("arguments")
        if not isinstance(outer, dict):
            return True
        action = outer.get("action")
        if action in _READ_ADMIN_ACTIONS:
            return False
        if action == "prune":
            arguments = outer.get("arguments")
            if not isinstance(arguments, dict):
                return True
            return arguments.get("apply") is True
        return True
    if operation == "call":
        outer = request.get("arguments")
        if not isinstance(outer, dict):
            return True
        name = outer.get("name")
        arguments = outer.get("arguments")
        if name == "memory":
            if not isinstance(arguments, dict):
                return True
            actions = [key for key in {
                "recall",
                "propose",
                "review",
                "accept",
                "reject",
                "freshness",
                "relations",
                "graph",
                "review_forget",
                "forget",
                "code_context",
            } if key in arguments]
            return len(actions) != 1 or actions[0] not in _READ_CALL_ACTIONS
        if isinstance(name, str):
            return name.removeprefix("memory_") not in _READ_CALL_ACTIONS
        return True
    # background-propose, routing mutations and future operations are writes
    # unless explicitly listed above as read-only.
    return True


def foreground_pool_sizes(inference_limit):
    """Return bounded read/write executor sizes for foreground requests.

    Reads get the existing inference-scaled capacity.  Writes share one
    SQLite writer and therefore need only a smaller feeder lane; limiting that
    lane keeps durable writer waiters from consuming all read capacity.
    """
    if not isinstance(inference_limit,int) or inference_limit < 1:
        raise ValueError("Invalid foreground inference limit")
    return min(24,max(8,inference_limit*4)), min(8,max(2,inference_limit))


class ModelProcess:
    """A stalled native inference can be killed without stopping the database host."""
    def __init__(self,cache,environment,threads=2,containment=None,rerank=False,parallel_slots=2,reranker_name=DEFAULT_RERANKER_NAME,embedding_profile="bge-int8"):
        if not 1<=parallel_slots<=8:
            raise ValueError("Model concurrency bound")
        if embedding_profile not in {"bge-int8","bge-legacy"}:
            raise ValueError("Unreviewed embedding profile")
        self.parallel_slots=parallel_slots
        command=[sys.executable,"-B","-m","scripts.vector_pipeline","embedding-server","--cache",str(cache),"--threads",str(threads),"--supervised","--parallel-slots",str(parallel_slots)]
        if rerank:
            command.extend(["--rerank","--reranker-name",reranker_name])
        command.extend(["--embedding-profile",embedding_profile])
        self.process=subprocess.Popen(command,
            cwd=Path(__file__).resolve().parents[1],env=environment,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
        self.responses=queue.Queue(maxsize=1)
        self.pending={}
        self.pending_lock=threading.Lock()
        self.write_lock=threading.Lock()
        self.close_lock=threading.Lock()
        self.failed=threading.Event()
        def read():
            try:
                while True:
                    raw=self.process.stdout.readline(1024*1024+1)
                    if not raw or len(raw)>1024*1024 or not raw.endswith(b"\n"):
                        return
                    response=json.loads(raw,object_pairs_hook=unique_object)
                    ident=response.get("id")
                    if ident is None:
                        self.responses.put_nowait(response)
                    else:
                        with self.pending_lock:
                            target=self.pending.get(ident)
                        if target is None:
                            raise ValueError("Unexpected model response identity")
                        target.put_nowait(response)
            except (OSError,ValueError,queue.Full,AttributeError):
                return
            finally:
                self.failed.set()
                with self.pending_lock:
                    for target in list(self.pending.values())+[self.responses]:
                        try:
                            target.put_nowait(None)
                        except queue.Full:
                            pass
        self.reader=threading.Thread(target=read,daemon=True)
        self.reader.start()
        try:
            if containment is not None:
                containment.assign(self.process.pid)
            self.process.stdin.write(b'{"start":true}\n')
            self.process.stdin.flush()
            ready=self.read(45)
            if ready.get("ready") is not True:
                raise ValueError("Model unavailable")
            self.identity=ready["model"]
            self.reranker_identity=ready.get("reranker")
        except Exception:
            self.close()
            raise

    def read(self,timeout):
        response=self.responses.get(timeout=timeout)
        if not isinstance(response,dict):
            raise ValueError("Invalid model frame")
        return response

    def invoke(self,method,value):
        ident=uuid.uuid4().hex
        raw=json.dumps({"id":ident,"method":method,"value":value},ensure_ascii=False,allow_nan=False).encode()+b"\n"
        if len(raw)>65536:
            raise ValueError("Model input frame bound")
        target=queue.Queue(maxsize=1)
        with self.pending_lock:
            if self.failed.is_set() or len(self.pending)>=self.parallel_slots:
                raise ValueError("Model unavailable or lane bound exceeded")
            self.pending[ident]=target
        try:
            with self.write_lock:
                self.process.stdin.write(raw)
                self.process.stdin.flush()
            response=target.get(timeout=2)
            if not isinstance(response,dict) or response.get("id")!=ident or "error" in response:
                raise ValueError("Model operation failed")
            return response["vector"]
        except Exception:
            self.close()
            raise
        finally:
            with self.pending_lock:
                self.pending.pop(ident,None)

    def query(self,value):
        return self.invoke("query",value)

    def passages(self,value):
        return self.batches("passages",value)

    def rerank(self,value):
        return self.batches("rerank",value["documents"],query=value["query"])

    def batches(self,method,documents,query=None):
        results=[]
        pending=[]
        size=len((query or "").encode())+128
        for document in documents:
            cost=len(json.dumps(document,ensure_ascii=False).encode())+2
            if pending and (size+cost>60000 or len(pending)>=8):
                results.extend(self.invoke(method,{"query":query,"documents":pending} if query is not None else pending))
                pending=[]
                size=len((query or "").encode())+128
            pending.append(document)
            size+=cost
        if pending:
            results.extend(self.invoke(method,{"query":query,"documents":pending} if query is not None else pending))
        return results

    def close(self):
        with self.close_lock:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)
            self.reader.join(timeout=1)
            if not self.process.stdin.closed:
                self.process.stdin.close()
            if not self.process.stdout.closed:
                self.process.stdout.close()


class QueryModels(ModelPool):
    """Compatibility name for the adaptive foreground pool."""
    def __init__(self,cache,environment,budget):
        super().__init__(lambda:ModelProcess(cache,environment,threads=1),budget,"foreground")


class MultiplexBroker:
    def __init__(self,binary,config,environment=None,maintenance=False):
        self.temporary=None
        if maintenance:
            self.temporary=tempfile.TemporaryDirectory(prefix="memory-index-host-")
            host=json.loads(Path(config).read_text(encoding="utf-8"),object_pairs_hook=unique_object)
            host["read_workers"]=1
            config=Path(self.temporary.name)/"host.json"
            config.write_text(json.dumps(host),encoding="utf-8")
        try:
            broker_environment=dict(environment or os.environ)
            broker_environment["RAYON_NUM_THREADS"]="1"
            self.client=BrokerClient(binary,config,"chat-0",broker_environment)
        except Exception:
            if self.temporary:
                self.temporary.cleanup()
            raise
        self.pending={}
        self.submitted=set()
        self.lock=threading.Lock()
        self.write_lock=threading.Lock()
        self.session_locks={s["id"]:threading.Lock() for s in json.loads(Path(config).read_text(encoding="utf-8"))["sessions"]}
        self.stopped=threading.Event()
        self.reader=threading.Thread(target=self.pump,daemon=True)
        self.reader.start()

    def pump(self):
        while not self.stopped.is_set():
            try:
                raw=self.client.responses.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                if not raw.endswith(b"\n") or len(raw)>1024*1024:
                    raise ValueError("Invalid broker frame")
                response=json.loads(raw)
                key=(response.get("session"),response.get("id"))
                with self.lock:
                    target=self.pending.get(key)
                if target is not None:
                    target.put_nowait(response)
            except (ValueError,queue.Full,TypeError):
                self.stopped.set()
        with self.lock:
            for target in self.pending.values():
                try:
                    target.put_nowait(None)
                except queue.Full:
                    pass

    def exchange(self,request):
        lock=self.session_locks.get(request.get("session"))
        if lock is None:
            return {"session":request.get("session"),"id":request.get("id"),"error":"invalid_request_or_session","outcome_unknown":False}
        key=(request["session"],request["id"])
        with self.lock:
            if key in self.submitted:
                raise ValueError("Duplicate in-flight request")
            self.submitted.add(key)
        try:
            if not lock.acquire(timeout=5):
                return {"session":request["session"],"id":request["id"],"error":"session_queue_timeout","outcome_unknown":False}
            try:
                return self.exchange_locked(request)
            finally:
                lock.release()
        finally:
            with self.lock:
                self.submitted.discard(key)

    def exchange_locked(self,request):
        key=(request["session"],request["id"])
        raw=json.dumps(request,separators=(",",":"),allow_nan=False).encode()+b"\n"
        if len(raw)>65536:
            raise ValueError("Request bound exceeded")
        target=queue.Queue(maxsize=1)
        with self.lock:
            if self.stopped.is_set() or key in self.pending:
                raise ValueError("Broker unavailable or duplicate request")
            self.pending[key]=target
        try:
            with self.write_lock:
                self.client.process.stdin.write(raw)
                self.client.process.stdin.flush()
            response=target.get(timeout=45)
            if response is None:
                raise ValueError("Broker stopped")
            return response
        finally:
            with self.lock:
                self.pending.pop(key,None)

    def admin(self,session,action,arguments):
        response=self.exchange({"session":session,"id":uuid.uuid4().hex,"operation":"admin",
                                "arguments":{"action":action,"arguments":arguments}})
        if "error" in response:
            raise ValueError("Index maintenance rejected")
        return response["result"]

    def close(self):
        self.stopped.set()
        self.reader.join(timeout=1)
        self.client.close()
        if self.temporary:
            self.temporary.cleanup()


class Embeddings:
    """Bound inference and deduplicate queries; timed-out work retains its permit."""
    def __init__(self,model,workers=1,cache_bytes=4*1024**2,max_entries=256):
        self.model=model
        self.capacity=threading.Semaphore(workers)
        self.pool=concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        self.cache=OrderedDict()
        self.cache_bytes=cache_bytes
        self.max_entries=max_entries
        self.cached_bytes=0
        self.memory_pressure=False
        self.cache_records={}
        self.inflight_records={}
        self.revoked=set()
        self.inflight={}
        self.lock=threading.Lock()
        self.metrics={"cache_hit":0,"busy":0,"unavailable":0,"deadline":0}

    def run(self,method,value,timeout,partition="",record_ids=(),diagnostics=None):
        diagnostics={} if diagnostics is None else diagnostics
        diagnostics.clear()
        diagnostics.update(reason="ready")
        if method=="rerank":
            return self.rerank(value,timeout,partition,record_ids,diagnostics)
        identity=getattr(self.model,"reranker_identity",None) if method=="rerank" else self.model.identity
        # Cache scores, never candidate payloads; partition also prevents cross-project reuse.
        digest=hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=True).encode()).hexdigest()
        created=False
        with self.lock:
            key=(identity,partition,method,digest) if method in {"query","rerank"} else None
            if key in self.revoked:
                diagnostics.update(reason="revoked")
                self.metrics["busy"]+=1
                return None
            if identity is None:
                diagnostics.update(reason="model_not_ready")
                self.metrics["unavailable"]+=1
                return None
            if key is not None and key in self.cache:
                # Identical scores can serve changed record IDs, but invalidation must track them.
                self.cache_records[key]=frozenset(record_ids)
                self.metrics["cache_hit"]+=1
                self.metrics[method+"_cache_hit"]=self.metrics.get(method+"_cache_hit",0)+1
                self.cache.move_to_end(key)
                return list(self.cache[key])
            future=self.inflight.get(key) if key is not None else None
            if future is None:
                if not self.capacity.acquire(blocking=False):
                    diagnostics.update(reason="executor_busy")
                    self.metrics["busy"]+=1
                    return None
                try:
                    future=self.pool.submit(getattr(self.model,method),value)
                except Exception:
                    self.capacity.release()
                    raise
                if key is not None:
                    self.inflight[key]=future
                    self.inflight_records[key]=frozenset(record_ids)
                created=True
        if created:
            def complete(done):
                try:
                    result=done.result()
                except Exception:
                    result=None
                with self.lock:
                    if key is not None:
                        self.inflight.pop(key,None)
                        dependencies=self.inflight_records.pop(key,frozenset())
                        if result is not None and key not in self.revoked:
                            self.cache[key]=tuple(result)
                            self.cache_records[key]=dependencies
                            self.cached_bytes+=1024+32*len(result)
                            self.cache.move_to_end(key)
                            self.trim_locked()
                        self.revoked.discard(key)
                self.capacity.release()
            future.add_done_callback(complete)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            diagnostics.update(getattr(exc,"diagnostic",{"reason":"inference_deadline"}))
            with self.lock:
                self.metrics["deadline"]+=1
            return None
        except Exception:
            diagnostics.update(reason="model_error")
            with self.lock:
                self.metrics["unavailable"]+=1
            return None

    def rerank(self,value,timeout,partition,record_ids,diagnostics=None):
        diagnostics={} if diagnostics is None else diagnostics
        identity=getattr(self.model,"reranker_identity",None)
        documents=value["documents"]
        if identity is None or not documents:
            diagnostics.update(reason="model_not_ready" if identity is None else "no_documents")
            return None
        if record_ids and len(record_ids)!=len(documents):
            raise ValueError("Reranker record identity count mismatch")
        keys=[(identity,partition,"rerank",hashlib.sha256(json.dumps(
            [value["query"],document,record_ids[i] if record_ids else None],ensure_ascii=True).encode()).hexdigest())
            for i,document in enumerate(documents)]
        waiting={}
        missing={}
        with self.lock:
            if any(key in self.revoked for key in keys):
                diagnostics.update(reason="revoked")
                self.metrics["busy"]+=1
                return None
            for i,key in enumerate(keys):
                if key in self.cache:
                    done=concurrent.futures.Future()
                    done.set_result(self.cache[key][0])
                    waiting[key]=done
                    self.cache.move_to_end(key)
                    self.metrics["rerank_score_cache_hit"]=self.metrics.get("rerank_score_cache_hit",0)+1
                elif key in self.inflight:
                    waiting[key]=self.inflight[key]
                else:
                    missing[key]=i
            if missing:
                budget=getattr(self.model,"budget",None)
                if waiting and budget is not None and budget.current().get("pressured"):
                    partial=[waiting[key].result() if key in waiting and waiting[key].done() else None for key in keys]
                    if any(score is not None for score in partial):
                        # Native selection still revalidates every returned version and source.
                        self.metrics["pressure_partial"]=self.metrics.get("pressure_partial",0)+1
                        diagnostics.update(reason="pressure_partial")
                        return partial
                if not self.capacity.acquire(blocking=False):
                    diagnostics.update(reason="executor_busy")
                    self.metrics["busy"]+=1
                    return [waiting[key].result() if key in waiting and waiting[key].done() else None for key in keys]
                for key,i in missing.items():
                    waiting[key]=concurrent.futures.Future()
                    self.inflight[key]=waiting[key]
                    self.inflight_records[key]=frozenset([record_ids[i]]) if record_ids else frozenset()
                try:
                    future=self.pool.submit(self.model.rerank,{"query":value["query"],
                        "documents":[documents[i] for i in missing.values()]})
                except Exception:
                    for key in missing:
                        self.inflight.pop(key,None)
                        self.inflight_records.pop(key,None)
                    self.capacity.release()
                    raise
            else:
                self.metrics["cache_hit"]+=1
                self.metrics["rerank_cache_hit"]=self.metrics.get("rerank_cache_hit",0)+1
        if missing:
            def complete(done):
                failure=None
                try:
                    result=done.result()
                    if len(result)!=len(missing) or any(not isinstance(v,(int,float)) or not math.isfinite(v) for v in result):
                        raise ValueError("Reranker score count mismatch")
                except Exception as exc:
                    failure=getattr(exc,"diagnostic",{"reason":"model_error"})
                    result=[None]*len(missing)
                delivered=[]
                with self.lock:
                    for key,score in zip(missing,result):
                        self.inflight.pop(key,None)
                        dependencies=self.inflight_records.pop(key,frozenset())
                        if key in self.revoked:
                            score=None
                        if score is not None:
                            self.cache[key]=(score,)
                            self.cache_records[key]=dependencies
                            self.cached_bytes+=1056
                            self.cache.move_to_end(key)
                        self.revoked.discard(key)
                        delivered.append((waiting[key],score))
                    self.trim_locked()
                for target,score in delivered:
                    target.memory_failure=failure
                    target.set_result(score)
                self.capacity.release()
            future.add_done_callback(complete)
        deadline=time.monotonic()+timeout
        try:
            results=[waiting[key].result(timeout=max(0,deadline-time.monotonic())) for key in keys]
            for key in keys:
                failure=getattr(waiting[key],"memory_failure",None)
                if failure:
                    diagnostics.update(failure)
                    break
            return results
        except concurrent.futures.TimeoutError:
            diagnostics.update(reason="inference_deadline")
            with self.lock:
                self.metrics["deadline"]+=1
            return [waiting[key].result() if waiting[key].done() else None for key in keys]

    def trim_locked(self):
        limit=min(self.cache_bytes,1024**2) if self.memory_pressure else self.cache_bytes
        while self.cache and (len(self.cache)>self.max_entries or self.cached_bytes>limit):
            key,value=self.cache.popitem(last=False)
            self.cache_records.pop(key,None)
            self.cached_bytes-=1024+32*len(value)
            if self.memory_pressure:
                self.metrics["pressure_evictions"]=self.metrics.get("pressure_evictions",0)+1

    def set_memory_pressure(self,pressured):
        # These bounded entries contain vectors/scores, not remembered payloads.
        # Keep a small hot set usable when model replicas must be unloaded.
        with self.lock:
            self.memory_pressure=bool(pressured)
            self.trim_locked()

    def invalidate(self,partition,record_ids=None,methods=None):
        with self.lock:
            def matches(key,dependencies):
                return key[1]==partition and (methods is None or key[2] in methods) and (record_ids is None or bool(dependencies.intersection(record_ids)))
            for key in [k for k in self.cache if matches(k,self.cache_records.get(k,frozenset()))]:
                self.cached_bytes-=1024+32*len(self.cache.pop(key))
                self.cache_records.pop(key,None)
            self.revoked.update(k for k in self.inflight if matches(k,self.inflight_records.get(k,frozenset())))
    def close(self):
        self.pool.shutdown(wait=True,cancel_futures=True)
        with self.lock:
            self.cache.clear()
            self.cache_records.clear()
            self.cached_bytes=0
        if isinstance(self.model,(ModelProcess,ModelPool)):
            self.model.close()


class MemoryHost:
    def __init__(self,binary,config,cache,environment=None,model=None,resource_policy=None):
        configuration=json.loads(Path(config).read_text(encoding="utf-8"),object_pairs_hook=unique_object)
        self.sessions={s["id"]:s for s in configuration["sessions"]}
        reranker_name=(environment or os.environ).get("MEMORYCORE_AI_RERANKER",DEFAULT_RERANKER_NAME)
        embedding_profile=(environment or os.environ).get("MEMORYCORE_AI_EMBEDDING_PROFILE","bge-int8")
        if model is None:
            if reranker_name not in {RERANKER_NAME,FAST_RERANKER_NAME,"mixedbread-ai/mxbai-rerank-xsmall-v1"}:
                raise ValueError("Unreviewed host reranker")
            if embedding_profile not in {"bge-int8","bge-legacy"}:
                raise ValueError("Unreviewed host embedding profile")
        policy=dict(resource_policy or {})
        self.defer_index_on_interactive=bool(policy.pop("defer_index_on_interactive",True))
        self.rerank_deadline_seconds=float(policy.pop("rerank_deadline_seconds",.25))
        if not 0 < self.rerank_deadline_seconds <= .25:
            raise ValueError("Invalid rerank deadline")
        self.interactive_lock=threading.Lock()
        self.interactive_inflight=0
        self.interactive_writes=0
        self.budget=ResourceBudget(**policy)
        # Inference must leave headroom for the broker and host inside the total budget.
        self.containment=InferenceLimits(self.budget.cpu_percent*.8,self.budget.current().get("memory_limit_bytes",0),
            memory_provider=lambda:self.budget.current().get("memory_limit_bytes",0))
        self.broker=None
        try:
            broker_environment=dict(environment or os.environ)
            readers=configuration.get("read_workers",2)
            index_budget=max(8*1024**2,min(512*1024**2,self.budget.current().get("memory_limit_bytes",256*1024**2)//5//max(1,readers)))
            broker_environment["MEMORYCORE_AI_INDEX_CACHE_BYTES"]=str(index_budget)
            self.sqlite_cache_bytes=sqlite_cache_budget(self.budget.current().get("memory_limit_bytes",0),readers)
            broker_environment["MEMORYCORE_AI_SQLITE_CACHE_BYTES"]=str(self.sqlite_cache_bytes)
            self.broker=MultiplexBroker(binary,config,broker_environment)
            self.maintenance=self.broker
        except Exception:
            if self.broker:
                self.broker.close()
            self.budget.close()
            self.containment.close()
            raise
        self.stop=threading.Event()
        self.index_wake=threading.Event()
        self.scope_polling=ScopePolling()
        self.index_sessions=[]
        self.index_errors=0
        self.embeddings=None
        self.background=None
        self.shared_models=None
        self.configured_scopes=set()
        self.retention_inactive_scopes=set()
        self.index_metrics={"batches":0,"stored":0,"stale":0,"failed":0,"deferred":0,
                            "status_polls":0,"claims":0,"commit_batches":0,"interactive_pauses":0}
        self.index_latency=IndexLatency()
        self.index_stages=IndexStages()
        self.pending_batches={}
        self.passage_cache=OrderedDict()
        self.passage_generation={}
        self.passage_lock=threading.Lock()
        self.index_executor=concurrent.futures.ThreadPoolExecutor(max_workers=self.budget.current()["worker_ceiling"])
        self.verification=VerificationJobs(binary,configuration,broker_environment,self.budget,self.containment)
        try:
            allowed={"PATH","PYTHONPATH","SYSTEMROOT","WINDIR","TEMP","TMP","USERPROFILE","HOME","HOMEDRIVE","HOMEPATH","LOCALAPPDATA","APPDATA"}
            model_environment={key:value for key,value in (environment or os.environ).items() if key.upper() in allowed}
            model_environment["HF_HUB_DISABLE_IMPLICIT_TOKEN"]="1"
            if model is None:
                parallel_slots=max(2,min(8,self.budget.current()["inference_limit"]))
                self.shared_models=SharedModels(lambda:ModelProcess(cache,model_environment,threads=1,
                    containment=self.containment,rerank=True,parallel_slots=parallel_slots,reranker_name=reranker_name,
                    embedding_profile=embedding_profile),max_lanes=parallel_slots,budget=self.budget)
            foreground=ModelPool(lambda: model,self.budget,"foreground",shared=self.shared_models)
            self.embeddings=Embeddings(foreground,workers=self.budget.current()["worker_ceiling"] if model is None else 1,
                cache_bytes=max(4*1024**2,min(32*1024**2,self.budget.current().get("memory_limit_bytes",0)//100)),max_entries=4096)
            if any(s.get("allow_admin") and s.get("generate_memories",True) for s in self.sessions.values()):
                self.background=ModelPool(lambda: model,self.budget,"background",shared=self.shared_models)
            self.configure_scopes()
        except Exception:
            self.index_errors+=1
        self.indexer=threading.Thread(target=self.index_loop,daemon=True)
        self.indexer.start()
        self.retention_errors=0
        self.retention_ticks=0
        self.retention_worker=threading.Thread(target=self.retention_loop,daemon=True)
        self.retention_worker.start()

    def invalidate_scope(self,scope):
        if self.embeddings:
            self.embeddings.invalidate(scope,methods={"rerank"})
        with self.passage_lock:
            self.passage_generation[scope]=self.passage_generation.get(scope,0)+1
            for key in [k for k in self.passage_cache if k[0]==scope]:
                self.passage_cache.pop(key)

    def retention_loop(self):
        # Separate from inference: quota, disabled generation and model pressure
        # must not prevent deletion. One authorised session per project scope.
        sessions={}
        for session in self.sessions.values():
            if session.get("allow_admin"):
                sessions.setdefault(session["scope"],session["id"])
        due={scope:0.0 for scope in sessions}
        while not self.stop.is_set():
            for scope,session in sessions.items():
                if self.stop.is_set():
                    break
                if time.monotonic()<due[scope]:
                    continue
                try:
                    receipt=self.maintenance.admin(session,"archive-cleanup",{})
                    self.retention_ticks+=1
                    if receipt.get("project_state") in {"archived","deleted","unknown"}:
                        self.retention_inactive_scopes.add(scope)
                    elif receipt.get("project_state")=="active":
                        self.retention_inactive_scopes.discard(scope)
                    if receipt.get("pending") or receipt.get("removed"):
                        self.invalidate_scope(scope)
                    due[scope]=time.monotonic()+(.1 if receipt.get("pending") else 60.0)
                except Exception:
                    self.retention_errors+=1
                    due[scope]=time.monotonic()+5.0
            delay=max(.1,min((when-time.monotonic() for when in due.values()),default=60.0))
            self.stop.wait(delay)

    def configure_scopes(self):
        identity=self.embeddings.model.identity if self.embeddings else None
        if identity is None or self.background is None or self.background.identity!=identity:
            return
        for session in self.sessions.values():
            if session["scope"] in self.retention_inactive_scopes:
                continue
            if session.get("allow_admin") and session.get("generate_memories",True) and session["scope"] not in self.configured_scopes:
                status=self.maintenance.admin(session["id"],"vector-status",{})
                if status["configured"] and (status["model"]!=identity or status["dimensions"]!=384):
                    self.index_errors+=1
                    self.configured_scopes.add(session["scope"])
                    continue
                # Idempotent configure also upgrades the derived-index schema.
                self.maintenance.admin(session["id"],"vector-configure",{"model":identity,"dimensions":384})
                self.index_sessions.append(session["id"])
                self.configured_scopes.add(session["scope"])

    def finish_batches(self):
        for future,(session,jobs) in list(self.pending_batches.items()):
            if not future.done():
                continue
            self.pending_batches.pop(future)
            batch=jobs["jobs"]
            failed=False
            try:
                vectors=future.result()
                if len(vectors)!=len(batch):
                    raise ValueError("Embedding count mismatch")
                items=[{key:job[key] for key in ("id","checksum","lease")} | {"vector":vector}
                       for job,vector in zip(batch,vectors)]
                for group in vector_write_batches(items):
                    with self.index_stages.measure("put"):
                        receipt=self.maintenance.admin(session,"vector-put",{"model":jobs["model"],"items":group})
                    self.index_metrics["commit_batches"]+=1
                    self.index_metrics["stored"]+=receipt["stored"]
                    self.index_metrics["stale"]+=receipt["stale"]
                    self.index_latency.observe(receipt.get("indexing_enqueued_ms", [None]*receipt["stored"]),
                                               time.time_ns()//1_000_000)
                    if receipt["stale"]:
                        stale_ids={item["id"] for item in group}
                        with self.passage_lock:
                            for key in [k for k in self.passage_cache if k[3] in stale_ids]:
                                self.passage_cache.pop(key)
                self.index_metrics["batches"]+=1
                continue
            except TimeoutError:
                self.index_metrics["deferred"]+=1
            except Exception:
                self.index_errors+=1
                self.index_metrics["failed"]+=1
                failed=True
            try:
                with self.index_stages.measure("release"):
                    self.maintenance.admin(session,"vector-release",{"items":[{"id":job["id"],"lease":job["lease"]} for job in batch],"failed":failed})
            except Exception:
                self.index_errors+=1

    def encode_batch(self,session,jobs):
        scope=self.sessions[session]["scope"]
        keys=[(scope,jobs["model"],hashlib.sha256(j["text"].encode()).hexdigest(),j["id"]) for j in jobs["jobs"]]
        with self.passage_lock:
            generation=self.passage_generation.get(scope,0)
            cached=[self.passage_cache.get(key) for key in keys]
        missing=[i for i,value in enumerate(cached) if value is None]
        if missing:
            with self.index_stages.measure("encode"):
                vectors=self.background.passages([jobs["jobs"][i]["text"] for i in missing])
            if len(vectors)!=len(missing):
                raise ValueError("Embedding count mismatch")
            with self.passage_lock:
                for i,vector in zip(missing,vectors):
                    cached[i]=vector
                    if self.passage_generation.get(scope,0)==generation:
                        self.passage_cache[keys[i]]=vector
                        self.passage_cache.move_to_end(keys[i])
                while len(self.passage_cache)>256:
                    self.passage_cache.popitem(last=False)
        return cached

    def _interactive_enter(self,request=None):
        lock=getattr(self,"interactive_lock",None)
        if lock is None:
            return False
        is_write=interactive_request_is_write(request)
        with lock:
            self.interactive_inflight+=1
            if is_write:
                self.interactive_writes+=1
        return is_write

    def _interactive_leave(self,is_write=False):
        lock=getattr(self,"interactive_lock",None)
        if lock is None:
            return
        with lock:
            self.interactive_inflight=max(0,self.interactive_inflight-1)
            if is_write and self.interactive_writes:
                self.interactive_writes-=1

    def _interactive_busy(self):
        lock=getattr(self,"interactive_lock",None)
        return bool(lock and self.interactive_writes)

    def index_loop(self):
        cursor=0
        active_scopes=set()
        while not self.stop.is_set():
            # Clear before observing durable polling state and completed futures,
            # so a completion during this iteration can wake the following wait.
            self.index_wake.clear()
            self.finish_batches()
            state=self.budget.current()
            if self.embeddings:
                self.embeddings.set_memory_pressure(state["memory_pressure"])
            # Index writes share the single durable writer with foreground
            # requests. Let an in-flight interactive exchange finish first so
            # maintenance cannot extend its writer queue; pending leases stay
            # durable and catch-up resumes as soon as the request burst ends.
            if getattr(self,"defer_index_on_interactive",True) and self._interactive_busy():
                self.index_metrics["interactive_pauses"]=self.index_metrics.get("interactive_pauses",0)+1
                self.stop.wait(.01)
                continue
            if state["pressured"]:
                if state["memory_pressure"] and self.embeddings:
                    with self.passage_lock:
                        self.passage_cache.clear()
                with self.index_stages.measure("pressure_wait"):
                    self.stop.wait(.1)
                continue
            try:
                self.configure_scopes()
                capacity=self.background.state()["resident"] if self.background else 0
                # Round-robin scopes; leases prevent duplicate batches within a scope.
                checked=0
                while self.index_sessions and len(self.pending_batches)<capacity and checked<max(capacity,len(self.index_sessions)):
                    session=self.index_sessions[cursor%len(self.index_sessions)]
                    cursor+=1
                    checked+=1
                    scope=self.sessions[session]["scope"]
                    if scope in getattr(self,"retention_inactive_scopes",set()):
                        continue
                    generation=self.scope_polling.due(scope)
                    if generation is None:
                        continue
                    # A successful claim is enough to keep draining this scope;
                    # count the durable queue again only after it becomes idle.
                    if scope not in active_scopes:
                        with self.index_stages.measure("status"):
                            status=self.maintenance.admin(session,"vector-status",{})
                        self.index_metrics["status_polls"]+=1
                        if status.get("pending",0)<=status.get("quarantined",0):
                            self.scope_polling.defer(scope,generation)
                            continue
                    with self.index_stages.measure("claim"):
                        jobs=self.maintenance.admin(session,"vector-claim",{})
                    self.index_metrics["claims"]+=1
                    if jobs["model"]!=self.background.identity:
                        raise ValueError("Index model mismatch")
                    if not jobs["jobs"]:
                        active_scopes.discard(scope)
                        self.scope_polling.defer(scope,generation,.05)
                        continue
                    active_scopes.add(scope)
                    self.submit_index_batch(session,jobs)
                if self.pending_batches and len(self.pending_batches)>=capacity:
                    self.background.demand.set()
            except Exception:
                self.index_errors+=1
            self.index_wake.wait(.01 if self.pending_batches else .5)
        self.index_executor.shutdown(wait=True,cancel_futures=True)
        self.finish_batches()

    def submit_index_batch(self,session,jobs):
        future=self.index_executor.submit(self.encode_batch,session,jobs)
        self.pending_batches[future]=(session,jobs)
        future.add_done_callback(lambda _:self.index_wake.set())

    def exchange(self,request):
        is_write=self._interactive_enter(request)
        try:
            return self._exchange(request)
        finally:
            self._interactive_leave(is_write)

    def _exchange(self,request):
        if (request.get("operation")=="admin" and isinstance(request.get("arguments"),dict)
                and request["arguments"].get("action") in ("verify-start","verify-status","verify-forget")):
            return self.verification.exchange(request)
        request=copy.deepcopy(request)
        session=self.sessions.get(request.get("session"),{})
        outer=request.get("arguments",{})
        args=outer.get("arguments",{}) if isinstance(outer,dict) else {}
        query=None
        if request.get("operation")=="call" and isinstance(args,dict):
            if outer.get("name")=="memory":
                query=args.get("recall")
            elif outer.get("name")=="memory_recall":
                query=args.get("query")
        enhanced=bool(session) and isinstance(query,str) and bool(query.strip()) and session.get("use_memories",True)
        if enhanced:
            try:
                check_text(query,maximum=4096)
            except (ValueError,SystemExit):
                enhanced=False
        # Metadata is owned by this host, never by the external caller.
        if isinstance(outer,dict) and isinstance(outer.get("_meta"),dict):
            outer["_meta"].pop("memory_embedding",None)
        telemetry={}
        timings={}
        admission={"query":{"reason":"model_not_ready"},"rerank":{"reason":"not_attempted"}}
        if enhanced:
            meta=outer.setdefault("_meta",{})
            if not isinstance(meta,dict):
                raise ValueError("Invalid host metadata")
            embedding={"phase":"candidates","reranker":getattr(self.embeddings.model,"reranker_identity",None) if self.embeddings else None}
            start=time.perf_counter()
            if self.embeddings and len(query.encode())<=4096:
                vector=self.embeddings.run("query",query,.15,session["scope"],diagnostics=admission["query"])
                if vector is not None:
                    embedding.update(model=self.embeddings.model.identity,vector=vector)
            timings["encode_ms"]=(time.perf_counter()-start)*1000
            meta["memory_embedding"]=embedding
            # The native layer normalises/validates this as a single read-only recall.
            preview=copy.deepcopy(request)
            preview["id"]=uuid.uuid4().hex
            start=time.perf_counter()
            response=self.broker.exchange(preview)
            deadline=time.perf_counter()+.3
            while response.get("result",{}).get("memory_candidates",{}).get("telemetry",{}).get("vector_state")=="warming" and time.perf_counter()<deadline:
                time.sleep(.01)
                preview["id"]=uuid.uuid4().hex
                response=self.broker.exchange(preview)
            timings["candidates_ms"]=(time.perf_counter()-start)*1000
            candidates=response.get("result",{}).get("memory_candidates")
            if candidates is None:
                # Do not repeat a failed read through a weaker retrieval path.
                response["id"]=request["id"]
                if isinstance(response.get("result"),dict):
                    response["result"]["id"]=request["id"]
                return response
            else:
                telemetry=candidates["telemetry"]
                scores=None
                start=time.perf_counter()
                documents=candidates["documents"]
                eligible=[i for i,item in enumerate(candidates["context"]["items"])
                          if item["similarity"] is None or item["similarity"]>=.55]
                if eligible and self.embeddings:
                    bounded=[documents[i] for i in eligible]
                    result=self.embeddings.run("rerank",{"query":query,"documents":bounded},self.rerank_deadline_seconds,session["scope"],
                        record_ids=[candidates["context"]["items"][i]["id"] for i in eligible],diagnostics=admission["rerank"])
                    if result is not None and len(result)==len(eligible):
                        partial=[None]*len(documents)
                        for i,score in zip(eligible,result):
                            partial[i]=score
                        if any(score is not None for score in partial):
                            scores=partial
                timings["rerank_ms"]=(time.perf_counter()-start)*1000
                scored=sum(score is not None for score in scores) if scores is not None else 0
                telemetry["reranker_state"]="ready" if scored==len(eligible) and eligible else "partial" if scored else "no_eligible_candidates" if not eligible else "unavailable_exact_only"
                telemetry["reranker_scored"]=scored
                telemetry["reranker_candidates"]=len(eligible)
                telemetry["admission"]=admission
                meta["memory_embedding"]={"phase":"select","selection":{key:candidates[key] for key in ("context","binding")} | {"scores":scores}}
        start=time.perf_counter()
        response=self.broker.exchange(request)
        if request.get("operation")=="admin" and outer.get("action")=="project-event" and response.get("result",{}).get("applied"):
            scope=session.get("scope")
            if args.get("state")=="active":
                self.retention_inactive_scopes.discard(scope)
            else:
                self.retention_inactive_scopes.add(scope)
        if request.get("operation")=="admin" and outer.get("action") in {"purge","lifecycle","chat-event","project-event","archive-cleanup"} and "error" not in response:
            scope=session.get("scope")
            affected={outer.get("arguments",{}).get("memory_id")} if outer.get("action") in {"purge","lifecycle"} else None
            if scope and self.embeddings:
                self.embeddings.invalidate(scope,record_ids=affected,methods={"rerank"})
            with self.passage_lock:
                self.passage_generation[scope]=self.passage_generation.get(scope,0)+1
                for key in [k for k in self.passage_cache if k[0]==scope and (affected is None or k[3] in affected)]:
                    self.passage_cache.pop(key)
        if response.get("lane")=="write" and "error" not in response and session:
            self.scope_polling.wake(session["scope"])
            self.index_wake.set()
        timings["final_ms"]=(time.perf_counter()-start)*1000
        if enhanced:
            response["semantic_host"]={"indexing_scopes":len(self.index_sessions),"index_errors":self.index_errors,
                "resource_limited":self.budget.current()["pressured"],
                "foreground":self.embeddings.model.state() if self.embeddings else None,
                "background":self.background.state() if self.background else None,
                "embedding":dict(self.embeddings.metrics) if self.embeddings else {},
                "index":dict(self.index_metrics,latency=self.index_latency.snapshot(),stages=self.index_stages.snapshot()),
                "sqlite_cache_bytes_per_connection":self.sqlite_cache_bytes,
                "retention":{"ticks":self.retention_ticks,"errors":self.retention_errors},
                "resource_budget":self.budget.current(),
                "model_runtimes":self.shared_models.state() if self.shared_models else None,
                "retrieval":telemetry,"timing":timings}
        return response

    def close(self):
        self.stop.set()
        self.verification.close()
        self.index_wake.set()
        self.indexer.join()
        self.retention_worker.join()
        self.broker.close()
        if self.embeddings:
            self.embeddings.close()
        if self.background:
            self.background.close()
        if self.shared_models:
            self.shared_models.close()
        self.budget.close()
        self.containment.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--binary",required=True,type=Path)
    p.add_argument("--config",required=True,type=Path)
    p.add_argument("--cache",required=True,type=Path)
    p.add_argument("--cpu-budget-percent",type=float,default=50)
    p.add_argument("--memory-budget-percent",type=float,default=15)
    p.add_argument("--min-free-memory-percent",type=float,default=15)
    p.add_argument("--adaptive-priority",dest="adaptive_priority",action="store_true",default=True)
    p.add_argument("--no-adaptive-priority",dest="adaptive_priority",action="store_false")
    p.add_argument("--defer-index-on-interactive",dest="defer_index_on_interactive",action="store_true",default=True,
                   help="Pause maintenance index writes while a durable interactive write is in flight (default).")
    p.add_argument("--no-defer-index-on-interactive",dest="defer_index_on_interactive",action="store_false",
                   help="Allow maintenance index writes during interactive writes for comparison.")
    p.add_argument("--rerank-deadline-seconds",type=float,default=.25,
                   help="Maximum foreground reranker wait before falling back to source-checked candidates.")
    args=p.parse_args()
    host=MemoryHost(args.binary,args.config,args.cache,resource_policy={"cpu_percent":args.cpu_budget_percent,"memory_percent":args.memory_budget_percent,"free_percent":args.min_free_memory_percent,"adaptive_priority":args.adaptive_priority,"defer_index_on_interactive":args.defer_index_on_interactive,"rerank_deadline_seconds":args.rerank_deadline_seconds})
    output_lock=threading.Lock()
    state_lock=threading.Lock()
    # Maximum frame size times this bounded count is an 8 MiB input queue ceiling.
    max_pending=128
    capacity=threading.Semaphore(max_pending)
    busy=set()
    def emit(value):
        with output_lock:
            print(json.dumps(value,separators=(",",":")),flush=True)
    def handle(request):
        try:
            try:
                response=host.exchange(request)
            except Exception:
                response={"session":request["session"],"id":request["id"],"error":"host_request_failed","outcome_unknown":True}
            with state_lock:
                busy.discard(request["session"])
            emit(response)
        finally:
            capacity.release()
    try:
        emit({"event":"ready","runtime":"rust-with-local-embedding-host","vector_search":True,
              "embedding_model_loaded":bool(host.embeddings and host.embeddings.model.identity),"indexing_scopes":len(host.index_sessions),
              "resource_budget":host.budget.current(),
              "index_errors":host.index_errors,"max_pending":max_pending,"max_queued_input_bytes":max_pending*65536,"synthetic_only":True,"native_chat_capture":False,
              "resource_limits":host.containment.state()})
        # Keep durable mutations from occupying every foreground slot while
        # they wait behind SQLite's single writer.  A busy write lane must not
        # turn an otherwise fast read/recall into an unbounded host-side wait.
        # Both lanes remain bounded; the write lane is deliberately smaller so
        # it applies natural backpressure before requests reach the broker's
        # writer queue.  Unknown operations are classified as writes by the
        # same conservative predicate used for index deferral.
        inference_limit=host.budget.current()["inference_limit"]
        read_pool_workers,write_pool_workers=foreground_pool_sizes(inference_limit)
        read_pool=concurrent.futures.ThreadPoolExecutor(
            max_workers=read_pool_workers,thread_name_prefix="memory-foreground-read")
        write_pool=concurrent.futures.ThreadPoolExecutor(
            max_workers=write_pool_workers,thread_name_prefix="memory-foreground-write")
        try:
            while True:
                raw=sys.stdin.buffer.readline(65537)
                if not raw:
                    break
                if len(raw)>65536 or not raw.endswith(b"\n"):
                    raise ValueError("Input frame bound exceeded")
                try:
                    request=json.loads(raw,object_pairs_hook=unique_object)
                    if not isinstance(request.get("id"),str) or not isinstance(request.get("session"),str):
                        raise ValueError("Invalid request")
                except (ValueError,AttributeError):
                    emit({"error":"invalid_request"})
                    continue
                if not capacity.acquire(blocking=False):
                    emit({"session":request["session"],"id":request["id"],"error":"queue_full","outcome_unknown":False})
                    continue
                with state_lock:
                    duplicate=request["session"] in busy
                    if not duplicate:
                        busy.add(request["session"])
                if duplicate:
                    capacity.release()
                    emit({"session":request["session"],"id":request["id"],"error":"session_busy","outcome_unknown":False})
                    continue
                pool=(write_pool if interactive_request_is_write(request) else read_pool)
                pool.submit(handle,request)
        finally:
            # Wait for accepted requests before closing the broker.  This is
            # equivalent to the previous single executor's context manager,
            # but keeps the two admission lanes independent during the run.
            read_pool.shutdown(wait=True)
            write_pool.shutdown(wait=True)
    finally:
        host.close()


if __name__=="__main__":
    main()
