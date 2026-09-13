"""Reproducible synthetic end-to-end host benchmarks; no real chat data."""
import argparse
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import queue
import random
import statistics
import subprocess
import sys
import threading
import time
import uuid

import psutil
import tiktoken
from scripts import test_rust_encryption as fixtures
from scripts.index_telemetry import latency_delta

ROOT=Path(__file__).resolve().parents[1]
HOST_ROOT=Path(os.environ.get("MEMORYCORE_AI_BENCHMARK_HOST_ROOT",ROOT)).resolve()
CORPUS=[
    ("Vehicle servicing","The automobile needs annual maintenance.","How should I look after my car?","When is motor vehicle upkeep due?"),
    ("Invoice approval","Supplier bills require authorisation from the finance manager.","Who signs off vendor invoices?","Who can approve payments to suppliers?"),
    ("Orchard irrigation","Fruit trees need deep watering twice a week during summer.","How often should apple plants be hydrated in hot weather?","Keeping fruit plants hydrated"),
    ("Backups","Database snapshots are copied to an independent encrypted disk every night.","How do we protect against losing stored records?","Where are nightly recovery copies kept?"),
    ("Fire assembly","Evacuated staff must meet at the eastern car park.","Where do people gather after leaving a burning building?","What is the emergency muster location?"),
    ("Password recovery","Resetting an account secret requires a verified recovery email.","How can I regain access after forgetting my login credential?","What is needed to reset a password?"),
    ("Meeting schedule","The project team meets each Tuesday at nine in the morning.","When is the weekly project catch-up?","What day and time is the team meeting?"),
    ("Leave requests","Annual holidays need supervisor approval two weeks before departure.","How early should I ask my boss for vacation?","Who authorises time away from work?"),
    ("Parcel returns","Unwanted purchases may be sent back within thirty days with a receipt.","Can I send an item back after buying it?","What is the deadline for returning shopping?"),
    ("Allergen handling","Meals containing peanuts must be prepared with separate utensils.","How do we prevent nut contamination in food?","Should peanut dishes share cooking tools?"),
    ("Library loans","Borrowed books must be brought back after twenty-one days.","How long can I keep a library book?","When is borrowed reading material due?"),
    ("Battery care","Rechargeable cells should be stored at half charge in a cool dry place.","How should unused batteries be kept?","What conditions extend stored battery life?"),
    ("Remote access","Off-site staff connect through the corporate virtual private network.","How do employees reach office systems from home?","What connection should remote workers use?"),
    ("Travel claims","Business transport expenses require original receipts and a trip purpose.","What evidence do I need for reimbursement after work travel?","How do I claim a business journey cost?"),
    ("Visitor entry","Guests sign the reception register and wear a temporary badge.","What must someone visiting the office do on arrival?","Do visitors need identification?"),
    ("Medicine storage","Insulin must be refrigerated between two and eight degrees Celsius.","How cold should insulin be kept?","Can injectable diabetes medicine be stored warm?"),
    ("Garden pruning","Rose bushes are cut back during late winter before new growth.","When should roses be trimmed?","What season is best for cutting back flowering shrubs?"),
    ("Network outage","Connection failures are reported to the service desk using a phone call.","Who should I contact when the internet stops working?","How do I report a disconnected network?"),
    ("Equipment checkout","Laptops leaving the building must be recorded in the asset register.","What is required before taking a work computer home?","How is borrowed portable hardware tracked?"),
    ("Document retention","Tax records are retained for seven years in the protected archive.","How long should taxation paperwork be kept?","Where do old tax documents belong?"),
    ("Pool safety","Children must remain within an adult's reach while swimming.","How closely should adults supervise young swimmers?","What is the supervision rule around a pool?"),
    ("Coffee machine","The espresso appliance is descaled monthly using its approved cleaner.","How often should we remove scale from the coffee maker?","How is the espresso machine maintained?"),
    ("Training records","Completed safety courses are recorded by the learning coordinator.","Who tracks staff safety qualifications?","Where should finished workplace courses be reported?"),
    ("Release approval","Software changes require passing tests and a reviewer before deployment.","What must happen before publishing a code update?","Can an unreviewed change go live?"),
]
NEGATIVES=["What is the population of Neptune?","Who won the 1823 lunar chess championship?",
           "How do I bake a sourdough loaf?","What is the exchange rate for the Brazilian real?",
           "Which symphony did Beethoven finish last?","Where is my missing blue umbrella?"]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class BoundedLatency:
    """Conservative streaming quantiles for long soaks, without retained samples."""
    bounds=tuple(i/100 for i in range(101))+tuple(i/10 for i in range(11,101))+tuple(range(11,1001))+tuple(range(1010,10001,10))+tuple(range(10100,60001,100))

    def __init__(self):
        self.buckets=[0]*(len(self.bounds)+1)
        self.count=0
        self.maximum=0.0

    def append(self,value):
        if not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:
            raise ValueError("Invalid benchmark latency")
        self.buckets[bisect_left(self.bounds,value)]+=1
        self.count+=1
        self.maximum=max(self.maximum,value)

    def snapshot(self):
        result={"count":self.count,"quantile_method":"nearest-rank-histogram-upper-bound",
                "histogram_version":"latency-ms/1","max":self.maximum if self.count else None}
        for name,q in (("p50",.5),("p95",.95),("p99",.99)):
            rank=math.ceil(self.count*q)
            cumulative=0
            result[name]=None
            for index,count in enumerate(self.buckets):
                cumulative+=count
                if self.count and cumulative>=rank:
                    result[name]=min(self.maximum,self.bounds[index]) if index<len(self.bounds) else self.maximum
                    break
        result["nonempty_buckets"]=[[index,n] for index,n in enumerate(self.buckets) if n]
        return result


def summary(values):
    if isinstance(values,BoundedLatency):
        return values.snapshot()
    ordered=sorted(values)
    return {"count":len(values),"quantile_method":"nearest-rank",**{name:ordered[max(0,math.ceil(len(ordered)*q)-1)] if ordered else None
            for name,q in [("p50",.5),("p95",.95),("p99",.99),("max",1)]}}


class SlowRequests:
    """Keep only the slowest metadata-only examples for each workload operation."""
    def __init__(self,limit=24):
        self.limit=limit
        self.rows=defaultdict(list)

    def record(self,category,elapsed,seconds,response):
        rows=self.rows[category]
        if len(rows)>=self.limit and elapsed<=rows[-1]["latency_ms"]:
            return
        rows.append({"seconds":seconds,"latency_ms":elapsed,
            "timing":response.get("timing",{}),"native_timing":response.get("native_timing",{}),
            "foreground_timing":response.get("semantic_host",{}).get("timing",{}),
            "checkpoint":response.get("checkpoint")})
        rows.sort(key=lambda row:row["latency_ms"],reverse=True)
        del rows[self.limit:]


class ResourcePeaks:
    """Bounded periodic process-tree sampling, including temporary index builds."""
    def __init__(self,wire,interval=.1):
        self.wire=wire
        self.interval=interval
        self.stopped=threading.Event()
        self.lock=threading.Lock()
        self.samples=0
        self.failures=0
        self.unknown_private=0
        self.peak_private=None
        self.peak_rss=0
        self.thread=None

    def sample(self):
        try:
            row=self.wire.sample()
        except (psutil.Error,OSError):
            with self.lock:
                self.failures+=1
            return
        with self.lock:
            self.samples+=1
            self.peak_rss=max(self.peak_rss,row["rss_bytes"])
            private=row.get("private_bytes")
            if private is None:
                self.unknown_private+=1
            else:
                self.peak_private=max(self.peak_private or 0,private)

    def start(self):
        self.sample()
        def run():
            while not self.stopped.wait(self.interval):
                self.sample()
        worker=threading.Thread(target=run,name="growth-resource-sampler")
        worker.start()
        self.thread=worker

    def snapshot(self):
        with self.lock:
            return {"samples":self.samples,"sample_failures":self.failures,
                "unknown_private_samples":self.unknown_private,"peak_private_bytes":self.peak_private,
                "peak_tree_rss_bytes":self.peak_rss,"sample_interval_seconds":self.interval,
                "coverage":"Cumulative since host readiness; sampled peaks are lower bounds on instantaneous maxima."}

    def close(self):
        self.stopped.set()
        if self.thread:
            self.thread.join()


def error(response):
    return response.get("error") or response.get("result",{}).get("error")


def unpack(response):
    if error(response):
        raise ValueError(str(error(response)))
    body=json.loads(response["result"]["result"]["content"][0]["text"])
    rows=[]
    for line in body["packet"].splitlines():
        if line.startswith("{"):
            row=json.loads(line)
            if "id" in row:
                rows.append(row)
    return body,rows


class Wire:
    def __init__(self,fixture,semantic=True,resource_policy=None):
        h=fixture.h
        command=([sys.executable,"-B","-m","scripts.memory_host","--binary",str(h.binary),"--cache",os.environ["MEMORYCORE_AI_MODEL_CACHE"]]
                 if semantic else [str(h.binary)])+["--config",str(h.file)]
        if semantic and resource_policy:
            command += ["--cpu-budget-percent",str(resource_policy["cpu_percent"]),
                        "--memory-budget-percent",str(resource_policy["memory_percent"]),
                        "--min-free-memory-percent",str(resource_policy["free_percent"]),
                        "--adaptive-priority" if resource_policy.get("adaptive_priority",True)
                        else "--no-adaptive-priority",
                        "--defer-index-on-interactive" if resource_policy.get("defer_index_on_interactive",True)
                        else "--no-defer-index-on-interactive",
                        "--rerank-deadline-seconds",str(resource_policy.get("rerank_deadline_seconds",.25))]
        self.stderr=(h.root/("host-errors-"+uuid.uuid4().hex+".txt")).open("wb")
        self.process=subprocess.Popen(command,cwd=HOST_ROOT if semantic else ROOT,env=fixture.env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.stderr,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
        self.queue=queue.Queue()
        self.sequence=0
        self.seen_processes={}
        def read():
            try:
                for raw in self.process.stdout:
                    response=json.loads(raw)
                    response["_benchmark_received_at"]=time.perf_counter()
                    self.queue.put(response)
            finally:
                self.queue.put(None)
        self.reader=threading.Thread(target=read,daemon=True)
        self.reader.start()
        start=time.perf_counter()
        try:
            self.ready=self.receive(60)
            assert self.ready.get("event")=="ready",self.ready
            self.model_readiness={"initially_loaded":self.ready.get("embedding_model_loaded"),"recovered":False}
            if semantic and not self.ready["embedding_model_loaded"]:
                deadline=time.monotonic()+60
                while not self.admin("vector-status").get("configured"):
                    if time.monotonic()>=deadline:
                        raise TimeoutError("Model readiness did not recover within 60 seconds; initial resource budget: "
                                           +json.dumps(self.ready.get("resource_budget",{}),sort_keys=True))
                    time.sleep(.1)
                self.model_readiness["recovered"]=True
            self.startup_ms=(time.perf_counter()-start)*1000
            self.sample()
        except Exception:
            self.close()
            raise

    def send(self,session,operation,arguments,recovery=None):
        self.sequence+=1
        request={"session":session,"id":str(self.sequence),"operation":operation,"arguments":arguments}
        if recovery is not None:
            request["recovery"]=recovery
        self.process.stdin.write(json.dumps(request,separators=(",",":"),allow_nan=False).encode()+b"\n")
        self.process.stdin.flush()
        return request["id"]

    def receive(self,timeout=60):
        response=self.queue.get(timeout=timeout)
        if response is None:
            raise RuntimeError("Host exited")
        return response

    def call(self,session,operation,arguments,recovery=None):
        ident=self.send(session,operation,arguments,recovery)
        response=self.receive()
        assert response["id"]==ident and response["session"]==session,response
        return response

    def admin(self,action,args=None,session="chat-0"):
        response=self.call(session,"admin",{"action":action,"arguments":args or {}})
        if error(response):
            raise ValueError(str(error(response)))
        return response["result"]

    def recall(self,query,session="chat-0"):
        return self.call(session,"call",{"name":"memory","arguments":{"recall":query}})

    def sample(self):
        root=psutil.Process(self.process.pid)
        processes=[root]+root.children(recursive=True)
        cpu=rss=private=0
        private_known=True
        groups={}
        for p in processes:
            try:
                times=p.cpu_times()
                cpu+=times.user+times.system
                info=p.memory_info()
                rss+=info.rss
                committed=getattr(info,"private",None)
                private_known=private_known and committed is not None
                private+=committed or 0
                command=p.cmdline()
                kind="host" if p.pid==root.pid else "foreground_model" if "--rerank" in command else "background_model" if "embedding-server" in command else "reader" if "--role" in command and "read" in command else "writer" if "--role" in command else "broker"
                group=groups.setdefault(kind,{"processes":0,"rss_bytes":0,"private_bytes":0})
                group["processes"]+=1
                group["rss_bytes"]+=info.rss
                group["private_bytes"]+=committed or 0
                self.seen_processes[p.pid]=p.create_time()
            except psutil.Error:
                pass
        return {"cpu_seconds":cpu,"rss_bytes":rss,"private_bytes":private if private_known else None,"processes":len(processes),"groups":groups}

    def close(self):
        if self.process.poll() is None:
            self.sample()
            self.process.stdin.close()
            try:
                self.process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process.stdout.close()
        self.stderr.close()
        self.reader.join(timeout=2)
        # Only processes created by this synthetic fixture are eligible for cleanup.
        for pid,created in self.seen_processes.items():
            try:
                p=psutil.Process(pid)
                if p.create_time()==created and p.is_running():
                    p.kill()
                    p.wait(timeout=5)
            except psutil.Error:
                pass


class PlaintextBenchmarkFixture:
    """Explicit disposable plaintext arm; never migrate an encrypted fixture."""
    def setUp(self):
        self.env=dict(os.environ)
        self.env.pop("MEMORYCORE_AI_SYNTHETIC_TEST_KEY",None)
        self.h=fixtures.Harness(0,binary=fixtures.BINARY,environment=self.env)
        try:
            self.target=self.h.root/"plaintext-native.sqlite3"
            self.h.config.update(backend="native",allow_plaintext=True,database=str(self.target))
            self.h.config.pop("python")
            self.h.config.pop("backend_root")
            for session in self.h.config["sessions"]:
                session["allow_admin"]=True
            self.h.file.write_text(json.dumps(self.h.config),encoding="utf-8")
            result=subprocess.run([str(self.h.binary),"--init","--config",str(self.h.file)],
                                  env=self.env,capture_output=True,timeout=30)
            if result.returncode:
                raise RuntimeError(f"Plaintext benchmark initialisation failed: {result.stderr!r}")
            with self.target.open("rb") as stream:
                if stream.read(16)!=b"SQLite format 3\0":
                    raise RuntimeError("Plaintext benchmark did not create an ordinary SQLite file")
        except Exception:
            self.h.close()
            raise

    def doCleanups(self):
        self.h.close()


class Scenario:
    read_workers=None
    storage="sqlcipher"
    resource_policy={"cpu_percent":50,"memory_percent":15,"free_percent":15,"adaptive_priority":True,
                     "defer_index_on_interactive":True,"rerank_deadline_seconds":.25}
    def __init__(self,clients=64,projects=1):
        if self.storage not in {"sqlcipher","plaintext"}:
            raise ValueError("Unknown benchmark storage mode")
        self.fixture=fixtures.EncryptedNativeTests() if self.storage=="sqlcipher" else PlaintextBenchmarkFixture()
        self.fixture.setUp()
        self.h=self.fixture.h
        if self.read_workers is not None:
            self.h.config["read_workers"]=self.read_workers
        self.projects=projects
        original=self.h.config["sessions"][0]
        self.h.config["sessions"]=[]
        for i in range(clients):
            project=i%projects
            source=self.h.root/f"project-{project}"
            source.mkdir(exist_ok=True)
            self.h.config["sessions"].append(dict(original,id=f"chat-{i}",scope=f"synthetic:project-{project}",source_root=str(source)))
        self.h.file.write_text(json.dumps(self.h.config),encoding="utf-8")
        self.wire=None
        try:
            self.wire=Wire(self.fixture,resource_policy=self.resource_policy)
            self.encoder=tiktoken.get_encoding("o200k_base")
        except Exception:
            if self.wire:
                self.wire.close()
            self.fixture.doCleanups()
            raise
        self.ids=[]
        self.project_ids={}
        self.paths=[]
        self.noise=0
        self.source_sequence=0

    def bound_arguments(self,session,subject,summary,**extra):
        self.source_sequence+=1
        path=Path(self.h.config["sessions"][session]["source_root"])/f"bound-{self.source_sequence}.md"
        raw=("Fact: "+summary).encode()
        path.write_bytes(raw)
        return {"type":"semantic","subject":subject,"summary":summary,"source":path.name,
                "source_hash":hashlib.sha256(raw).hexdigest(),**extra}

    def seed(self):
        for project in range(self.projects):
            self.project_ids[project]=[]
            for n,(subject,fact,*_) in enumerate(CORPUS):
                raw=("Fact: "+fact).encode()
                path=Path(self.h.config["sessions"][project]["source_root"])/f"doc{n}.md"
                path.write_bytes(raw)
                sha=hashlib.sha256(raw).hexdigest()
                ident=self.wire.admin("remember",{"type":"semantic","subject":f"DOC{n:03} {subject}","summary":fact,"source":path.name,"source_hash":sha},session=f"chat-{project}")["memory_id"]
                self.wire.admin("bind-source",{"memory_id":ident,"path":path.name,"sha256":sha},session=f"chat-{project}")
                self.project_ids[project].append(ident)
                if project==0:
                    self.ids.append(ident)
                    self.paths.append(path)
        return self.catchup(60)

    def catchup(self,limit=30):
        start=time.perf_counter()
        samples=[]
        while True:
            statuses=[self.wire.admin("vector-status",{},session=f"chat-{project}") for project in range(self.projects)]
            ready=all(status.get("configured") for status in statuses)
            pending=sum(status.get("pending",0) for status in statuses)
            samples.append({"seconds":time.perf_counter()-start,"pending":pending,
                "oldest_pending_age_ms":max((status.get("oldest_pending_age_ms") or 0 for status in statuses),default=0)})
            if (pending==0 and ready) or time.perf_counter()-start>=limit:
                return {"drained":pending==0 and ready,"configured":ready,"seconds":time.perf_counter()-start,"pending":pending,"samples":samples}
            time.sleep(.1)

    def grow(self,count,source_bound=False):
        start=time.perf_counter()
        next_progress=start+30
        write_stages={}
        while self.noise<count:
            items=[]
            for _ in range(min(8,count-self.noise)):
                n=self.noise
                subject=f"Synthetic distractor {n}"
                summary_text=f"Warehouse shelf {n} contains labelled synthetic packing materials. Inventory reference {n}."
                arguments=self.bound_arguments(0,subject,summary_text) if source_bound else {"type":"semantic","subject":subject,"summary":summary_text}
                items.append({"action":"remember-bound" if source_bound else "remember","arguments":arguments})
                self.noise+=1
            response=self.wire.call("chat-0","admin",{"action":"batch","arguments":{"items":items}})
            if error(response):
                raise ValueError(str(error(response)))
            for key,value in response.get("native_timing",{}).items():
                write_stages.setdefault(key,[]).append(value)
            if time.perf_counter()>=next_progress:
                print(json.dumps({"progress":"growth_ingestion","distractors":self.noise,"target":count,
                    "seconds":round(time.perf_counter()-start,1)}),flush=True)
                next_progress=time.perf_counter()+30
        inserted=time.perf_counter()-start
        catchup=self.catchup(180)
        return {"insert_seconds":inserted,"catchup":catchup,"total_seconds":time.perf_counter()-start,
                "source_bound":source_bound,"vector_status":self.wire.admin("vector-status"),
                "native_write_stages_ms":{key:summary(values) for key,values in write_stages.items()}}

    def close(self):
        self.wire.close()
        self.fixture.doCleanups()


def quality():
    s=Scenario(10)
    baseline=None
    try:
        catchup=s.seed()
        assert catchup["drained"]
        baseline=Wire(s.fixture,semantic=False)
        rows=[]
        cases=[(i,q) for i,item in enumerate(CORPUS) for q in item[2:]]+[(None,q) for q in NEGATIVES]
        random.Random(41).shuffle(cases)
        for expected,query in cases:
            pair={"query":query,"expected":s.ids[expected] if expected is not None else None}
            for name,wire in [("lexical",baseline),("automatic",s.wire)]:
                start=time.perf_counter()
                response=wire.recall(query)
                body,found=unpack(response)
                ids=[r["id"] for r in found]
                rank=ids.index(pair["expected"])+1 if pair["expected"] in ids else None
                retrieval=response.get("semantic_host",{}).get("retrieval",body)
                pair[name]={"rank":rank,"returned":len(ids),"ids":ids,"vector_state":retrieval.get("vector_state"),
                            "reranker_state":retrieval.get("reranker_state"),
                            "tool_text_tokens":len(s.encoder.encode(response["result"]["result"]["content"][0]["text"])),
                            "response_tokens":len(s.encoder.encode(json.dumps(response,separators=(",",":")))),
                            "packet_tokens":len(s.encoder.encode(body["packet"])),"latency_ms":(time.perf_counter()-start)*1000}
            rows.append(pair)
        aggregates={}
        for name in ["lexical","automatic"]:
            positives=[r[name] for r in rows if r["expected"] is not None]
            negatives=[r[name] for r in rows if r["expected"] is None]
            aggregates[name]={"queries":len(positives),"hit_at_1":sum(r["rank"]==1 for r in positives)/len(positives),
                "hit_at_3":sum(r["rank"] is not None and r["rank"]<=3 for r in positives)/len(positives),
                "hit_at_8":sum(r["rank"] is not None for r in positives)/len(positives),
                "mrr":sum(1/r["rank"] if r["rank"] else 0 for r in positives)/len(positives),
                "negative_queries":len(negatives),"negative_nonempty":sum(r["returned"]>0 for r in negatives),
                "mean_response_tokens":statistics.mean(r["response_tokens"] for r in positives),
                "mean_tool_text_tokens":statistics.mean(r["tool_text_tokens"] for r in positives),
                "mean_packet_tokens":statistics.mean(r["packet_tokens"] for r in positives),
                "successful_queries_per_1000_tool_text_tokens":1000*sum(r["rank"] is not None for r in positives)/sum(r["tool_text_tokens"] for r in positives),
                "successful_queries_per_1000_response_tokens":1000*sum(r["rank"] is not None for r in positives)/sum(r["response_tokens"] for r in positives)}
        return {"kind":"paired_quality","corpus_facts":len(CORPUS),"aggregates":aggregates,"rows":rows,
                "full_corpus_tokens":len(s.encoder.encode(json.dumps(CORPUS))),"catchup":catchup}
    finally:
        if baseline:
            baseline.close()
        s.close()


def offered_operation(sequence,clients):
    return sequence%clients,(sequence+sequence//clients)%10


def workload(s,clients,rate,seconds,label,unique=False,source_bound=False):
    wire=s.wire
    series=BoundedLatency if seconds>1800 else list
    chains={}
    disposable={i:[] for i in range(clients)}
    for i in range(clients):
        subject=f"Synthetic changing session {i} {label}"
        arguments=s.bound_arguments(i,subject,"Initial synthetic revision.") if source_bound else {"type":"semantic","subject":subject,"summary":"Initial synthetic revision."}
        chains[i]=wire.admin("remember-bound" if source_bound else "remember",arguments,session=f"chat-{i}")["memory_id"]
        subject=f"Synthetic disposable session {i} {label}"
        record=s.bound_arguments(i,subject,"Disposable synthetic inventory fixture.") if source_bound else {
            "type":"semantic","subject":subject,"summary":"Disposable synthetic inventory fixture."}
        disposable[i].append(wire.admin("remember-bound" if source_bound else "remember",record,session=f"chat-{i}")["memory_id"])
    owners={ident:project for project,ids in s.project_ids.items() for ident in ids}
    owners.update({ident:client%s.projects for client,ident in chains.items()})
    owners.update({ident:client%s.projects for client,ids in disposable.items() for ident in ids})
    assert s.catchup(30)["drained"]
    initial_index=wire.recall(CORPUS[0][2])["semantic_host"]["index"]["latency"]
    pending={}
    available=set(range(clients))
    stats=Counter()
    failures=Counter()
    vector_states=Counter()
    quality_examples={}
    latencies=series()
    queues=series()
    services=series()
    query_latency=series()
    failed_latency=series()
    arrival_latency=series()
    scheduling_lag=series()
    response_dispatch=series()
    backpressure_examples=[]
    category_latency=defaultdict(series)
    native_stages=defaultdict(lambda:defaultdict(series))
    slow_requests=SlowRequests()
    checkpoints=[]
    stages={key:series() for key in ("encode_ms","candidates_ms","rerank_ms","final_ms")}
    last_host={}
    resources=[]
    start=time.perf_counter()
    initial=wire.sample()
    next_sample=0
    next_progress=30
    offered=0
    expected_offered=int(seconds*rate)
    while time.perf_counter()-start<seconds or pending or offered<expected_offered:
        response_available=False
        try:
            response=wire.queue.get_nowait()
            response_available=True
        except queue.Empty:
            pass
        now=time.perf_counter()-start
        if now>=next_sample:
            sample=wire.sample()
            sample.update(seconds=now,wal_bytes=Path(str(s.fixture.target)+"-wal").stat().st_size if Path(str(s.fixture.target)+"-wal").exists() else 0)
            resources.append(sample)
            next_sample=now+1
        if now>=next_progress:
            print(json.dumps({"progress":label,"seconds":round(now,1),"offered":offered,
                "completed":stats["completed"],"recall_hits":stats["retrieval_hit"],
                "errors":sum(failures.values()),"client_backpressure":stats["client_backpressure"],
                "private_bytes":resources[-1]["private_bytes"]}),flush=True)
            next_progress=now+30
        if not response_available and (now<seconds or offered<expected_offered):
            due=min(int(now*rate)+1,expected_offered)
            while offered<due:
                # A completed chat is available even after the driver was descheduled.
                try:
                    response=wire.queue.get_nowait()
                    response_available=True
                    break
                except queue.Empty:
                    pass
                n=offered
                offered+=1
                scheduled=start+n/rate
                session,kind=offered_operation(n,clients)
                if kind<6:
                    stats["offered_recall"]+=1
                if session not in available:
                    stats["client_backpressure"]+=1
                    if len(backpressure_examples)<24:
                        observed=time.perf_counter()
                        outstanding=next(item for item in pending.values() if item[0]==session)
                        backpressure_examples.append({"offered_sequence":n,"client":session,
                            "scheduled_seconds":n/rate,"driver_lag_ms":(observed-scheduled)*1000,
                            "outstanding_ms":(observed-outstanding[1])*1000})
                    continue
                available.remove(session)
                expected=None
                if kind<6:
                    qindex=(n//10*6+kind)%len(CORPUS)
                    query=CORPUS[qindex][2]
                    if unique:
                        query=f"Request reference {n}: {query}"
                    operation="call"
                    arguments={"name":"memory","arguments":{"recall":query}}
                    category="recall"
                    expected=s.project_ids[session%s.projects][qindex]
                elif kind<8:
                    operation="admin"
                    subject=f"Synthetic load {label} {n}"
                    summary_text=f"Synthetic fixture value {n} for concurrent ingestion."
                    record=s.bound_arguments(session,subject,summary_text) if source_bound else {"type":"semantic","subject":subject,"summary":summary_text}
                    arguments={"action":"remember-bound" if source_bound else "remember","arguments":record}
                    category="insert"
                elif kind==8:
                    operation="admin"
                    subject=f"Synthetic corrected {label} {session}"
                    summary_text=f"Revised synthetic value {n}."
                    record=s.bound_arguments(session,subject,summary_text,supersedes=chains[session]) if source_bound else {"type":"semantic","subject":subject,"summary":summary_text,"supersedes":chains[session]}
                    arguments={"action":"remember-bound" if source_bound else "remember","arguments":record}
                    category="correction"
                else:
                    operation="admin"
                    if disposable[session]:
                        arguments={"action":"purge","arguments":{"memory_id":disposable[session][-1],"user_confirmed":True}}
                        category="purge"
                    else:
                        stats["dependency_unavailable"]+=1
                        available.add(session)
                        continue
                sent=time.perf_counter()
                scheduling_lag.append((sent-scheduled)*1000)
                ident=wire.send(f"chat-{session}",operation,arguments)
                pending[ident]=(session,sent,category,expected,query if category=="recall" else None,scheduled)
                stats["submitted"]+=1
                if category=="recall":
                    stats["submitted_recall"]+=1
        if not response_available:
            try:
                response=wire.queue.get(timeout=.001)
            except queue.Empty:
                continue
        if response is None:
            raise RuntimeError("Host exited during workload")
        ident=response.get("id")
        if ident not in pending:
            raise AssertionError("Unmatched response")
        session,sent,category,expected,query,scheduled=pending.pop(ident)
        assert response["session"]==f"chat-{session}"
        available.add(session)
        received=time.perf_counter()
        arrival_latency.append((received-scheduled)*1000)
        if "_benchmark_received_at" in response:
            response_dispatch.append((received-response["_benchmark_received_at"])*1000)
        if error(response):
            failures[str(error(response))]+=1
            failed_latency.append((received-sent)*1000)
            continue
        elapsed=(received-sent)*1000
        slow_requests.record(category,elapsed,received-start,response)
        stats["completed"]+=1
        stats[category]+=1
        latencies.append(elapsed)
        category_latency[category].append(elapsed)
        for key,value in response.get("native_timing",{}).items():
            native_stages[category][key].append(value)
        if "checkpoint" in response:
            checkpoints.append({"seconds":time.perf_counter()-start,**response["checkpoint"]})
        queues.append(response["timing"]["queue_ms"])
        services.append(response["timing"]["service_ms"])
        if category=="recall":
            last_host=response.get("semantic_host",{})
            for key,values in stages.items():
                value=response.get("semantic_host",{}).get("timing",{}).get(key)
                if value is not None:
                    values.append(value)
            body,found=unpack(response)
            retrieval=response.get("semantic_host",{}).get("retrieval",body)
            vector_states[retrieval.get("vector_state","not_reported")]+=1
            stats["retrieval_hit"]+=expected in [r["id"] for r in found]
            stats["useful_semantic"]+=retrieval.get("vector_state") in {"ready","lagging"} and retrieval.get("reranker_state") in {"ready","partial"} and expected in [r["id"] for r in found]
            stats["partial_reranker"]+=retrieval.get("reranker_state")=="partial"
            stats["cross_scope_leaks"]+=sum(r["id"] in owners and owners[r["id"]]!=session%s.projects for r in found)
            stats["unrecognised_ids"]+=sum(r["id"] not in owners for r in found)
            stats["non_gold_in_scope"]+=sum(r["id"] in owners and owners[r["id"]]==session%s.projects and r["id"] not in s.project_ids[session%s.projects] for r in found)
            stats["delivered_facts"]+=len(found)
            stats["non_expected_in_scope"]+=sum(r["id"]!=expected and owners.get(r["id"])==session%s.projects for r in found)
            fault="incorrect" if any(r["id"]!=expected for r in found) else "missing" if not found else None
            key=(fault,expected)
            if fault and key not in quality_examples and sum(k[0]==fault for k in quality_examples)<24:
                quality_examples[key]={"failure_kind":fault,"query":query,"expected":expected,"returned":found,"retrieval":retrieval}
            stats["top1_hit"]+=bool(found) and found[0]["id"]==expected
            stats["max_pending_vectors"]=max(stats["max_pending_vectors"],retrieval.get("pending_index_updates",0))
            query_latency.append(elapsed)
        elif category=="insert":
            disposable[session].append(response["result"]["memory_id"])
            owners[response["result"]["memory_id"]]=session%s.projects
        elif category=="correction":
            chains[session]=response["result"]["memory_id"]
            owners[response["result"]["memory_id"]]=session%s.projects
        elif category=="purge":
            disposable[session].pop()
    total=time.perf_counter()-start
    final=wire.sample()
    catchup=s.catchup(30)
    final_index=wire.recall(CORPUS[0][2])["semantic_host"]["index"]["latency"]
    verified_scopes={str(project):wire.admin("verify",session=f"chat-{project}")["verified"]
                     for project in s.project_ids}
    verified=bool(verified_scopes) and all(verified_scopes.values())
    assert verified
    settled=wire.sample()
    wal=Path(str(s.fixture.target)+"-wal")
    settled["wal_bytes"]=wal.stat().st_size if wal.exists() else 0
    semantic=sum(count for state,count in vector_states.items() if state in {"ready","lagging"})
    return {"kind":label,"clients":clients,"offered_rate":rate,"offered":offered,"seconds":total,
            "model_readiness":wire.model_readiness,"startup_ms":wire.startup_ms,
            "query_cache_busted":unique,"source_bound_mutations":source_bound,"projects":s.projects,"offered_seconds":seconds,
            "workload_version":"mixed-fixed-chat-schedule/2",
            "driver_version":"response-first-arrivals/3",
            "expected_offered":expected_offered,
            "latency_scope":"Host send to receive; fixture source-file creation is excluded, offered throughput includes it.",
            "last_host_metrics":{key:last_host.get(key) for key in ("embedding","foreground","background","resource_budget","index","model_runtimes")},
            "completed_per_second":stats["completed"]/total,"committed_mutations_per_second":(stats["completed"]-stats["recall"])/total,
            "counts":dict(stats),"errors":dict(failures),"latency_ms":summary(latencies),"query_latency_ms":summary(query_latency),
            "failed_latency_ms":summary(failed_latency),"foreground_stages_ms":{key:summary(values) for key,values in stages.items()},
            "scheduled_to_response_ms":summary(arrival_latency),"scheduling_lag_ms":summary(scheduling_lag),
            "response_dispatch_ms":summary(response_dispatch),"backpressure_examples":backpressure_examples,
            "category_latency_ms":{key:summary(values) for key,values in category_latency.items()},
            "native_stages_ms":{category:{key:summary(values) for key,values in stages.items()} for category,stages in native_stages.items()},
            "slow_request_examples":dict(slow_requests.rows),
            "checkpoints":checkpoints,
            "indexing_latency":latency_delta(initial_index,final_index),
            "completion_fraction":stats["completed"]/offered if offered else None,
            "broker_queue_ms":summary(queues),"broker_service_ms":summary(services),"vector_states":dict(vector_states),
            "semantic_fraction":semantic/stats["recall"] if stats["recall"] else None,
            "useful_semantic_fraction":stats["useful_semantic"]/stats["offered_recall"] if stats["offered_recall"] else None,
            "useful_semantic_denominator":"All offered answerable recalls, including request errors and client backpressure.",
            "strict_expected_id_precision":stats["retrieval_hit"]/stats["delivered_facts"] if stats["delivered_facts"] else None,
            "quality_examples":list(quality_examples.values()),
            "hit_at_8":stats["retrieval_hit"]/stats["recall"] if stats["recall"] else None,
            "cpu_core_equivalents":(final["cpu_seconds"]-initial["cpu_seconds"])/total,
            "peak_tree_rss_bytes":max(r["rss_bytes"] for r in resources),"peak_private_bytes":max((r["private_bytes"] for r in resources if r["private_bytes"] is not None),default=None),
            "resources":resources,"catchup":catchup,"integrity_verified":verified,
            "integrity_scope_count":len(verified_scopes),"integrity_by_project":verified_scopes,
            "post_load_resources":settled,"peak_wal_bytes":max(r["wal_bytes"] for r in resources)}


def verify_scope(wire,session,mode):
    if mode=="interactive":
        return wire.call(session,"admin",{"action":"verify","arguments":{}})
    if mode!="maintenance":
        raise ValueError("Unknown verification mode")
    started=time.perf_counter()
    response=wire.call(session,"admin",{"action":"verify-start","arguments":{}})
    start_ms=(time.perf_counter()-started)*1000
    if response.get("error") or not {"job_id","state"} <= response.get("result",{}).keys():
        return response
    job=response["result"]
    deadline=time.monotonic()+95
    while job["state"] in {"queued","running"}:
        if time.monotonic()>=deadline:
            return {"error":"maintenance_poll_deadline","maintenance":job}
        time.sleep(.05)
        response=wire.call(session,"admin",{"action":"verify-status","arguments":{"job_id":job["job_id"]}})
        if response.get("error") or not {"job_id","state"} <= response.get("result",{}).keys():
            return response
        job=response["result"]
    receipt=job|{"start_ms":start_ms}
    if job["state"]!="complete" or job.get("verified") is not True:
        return {"error":job.get("error","maintenance_verification_failed"),"maintenance":receipt}
    return {"result":{"verified":True},"maintenance":receipt}


def growth(source_bound=False,sizes=(24,256,1024),checkpoint=None,verification="interactive"):
    s=Scenario(10)
    resources=ResourcePeaks(s.wire)
    try:
        resources.start()
        s.seed()
        results=[]
        for size in sizes:
            inserted=s.grow(size-24,source_bound=source_bound)
            latency=[]
            cold=[]
            warm=[]
            hits=0
            states=Counter()
            queries=[]
            for repeat in range(2):
                for i in range(12):
                    start=time.perf_counter()
                    response=s.wire.recall(CORPUS[i][2])
                    request_error=error(response)
                    if request_error:
                        body,rows={},[]
                        retrieval={"vector_state":"request_failed"}
                    else:
                        body,rows=unpack(response)
                        retrieval=response.get("semantic_host",{}).get("retrieval",body)
                    elapsed=(time.perf_counter()-start)*1000
                    latency.append(elapsed)
                    if retrieval.get("index_cache_hit") is True:
                        warm.append(elapsed)
                    elif retrieval.get("index_cache_hit") is False:
                        cold.append(elapsed)
                    hits+=s.ids[i] in [r["id"] for r in rows]
                    states[retrieval["vector_state"]]+=1
                    queries.append({"query":CORPUS[i][2],"expected":s.ids[i],"ids":[r["id"] for r in rows],
                        "latency_ms":elapsed,"retrieval":retrieval,"host_timing":response.get("semantic_host",{}).get("timing"),
                        "native_timing":response.get("native_timing"),"error":request_error,
                        "worker_failure":response.get("worker_failure")})
            results.append({"memories":size,"latency_ms":summary(latency),"cold_index_ms":summary(cold),"warm_index_ms":summary(warm),"hit_at_8":hits/24,"vector_states":dict(states),
                            "resources":s.wire.sample(),"resource_peaks":resources.snapshot(),"ingestion":inserted,"queries":queries,
                            "last_host_metrics":response.get("semantic_host")})
            if checkpoint is not None:
                checkpoint(results[-1])
            print(json.dumps({"progress":"growth_checkpoint","memories":size,"hit_at_8":hits/24,
                "p99_ms":summary(latency)["p99"],"ingestion_seconds":inserted["total_seconds"],
                "drained":inserted["catchup"]["drained"]}),flush=True)
            if not inserted["catchup"]["drained"]:
                break
        verified_scopes={}
        integrity_errors={}
        integrity_jobs={}
        for project in s.project_ids:
            try:
                response=verify_scope(s.wire,f"chat-{project}",verification)
                if "maintenance" in response:
                    integrity_jobs[str(project)]=response["maintenance"]
                if error(response):
                    verified_scopes[str(project)]=False
                    integrity_errors[str(project)]={"error":error(response),"worker_failure":response.get("worker_failure"),
                        "timing":response.get("timing")}
                else:
                    verified_scopes[str(project)]=response["result"]["verified"] is True
            except Exception as exc:
                verified_scopes[str(project)]=False
                integrity_errors[str(project)]=str(exc)
        verified=bool(verified_scopes) and all(verified_scopes.values())
        return {"kind":"growth","results":results,"source_bound":source_bound,
            "integrity_verified":verified,"integrity_by_project":verified_scopes,"integrity_errors":integrity_errors,
            "verification_mode":verification,"integrity_jobs":integrity_jobs,
            "resource_peaks_after_verification":resources.snapshot(),
            "distractors":"Source-bound synthetic warehouse records, eligible for indexing and recall." if source_bound else "Unbound synthetic warehouse records, excluded from indexing and recall."}
    finally:
        resources.close()
        s.close()


def faults():
    s=Scenario(10)
    checks={}
    try:
        s.seed()
        s.wire.admin("lifecycle",{"memory_id":s.ids[0],"action":"archive"})
        checks["archive_excluded"]=s.ids[0] not in [r["id"] for r in unpack(s.wire.recall(CORPUS[0][2]))[1]]
        s.wire.admin("purge",{"memory_id":s.ids[1],"user_confirmed":True})
        checks["purge_excluded"]=s.ids[1] not in [r["id"] for r in unpack(s.wire.recall(CORPUS[1][2]))[1]]
        s.paths[2].write_text("Fact: Changed synthetic evidence.",encoding="utf-8")
        checks["changed_source_excluded"]=s.ids[2] not in [r["id"] for r in unpack(s.wire.recall(CORPUS[2][2]))[1]]
        descendants=psutil.Process(s.wire.process.pid).children(recursive=True)
        models=[p for p in descendants if "embedding-server" in p.cmdline()]
        for model in models:
            model.kill()
            model.wait(timeout=5)
        start=time.perf_counter()
        response=s.wire.recall("release approval")
        body,_=unpack(response)
        checks["model_failure_conservative_response"]=response["semantic_host"]["retrieval"]["vector_state"]=="unavailable"
        checks["model_failure_response_ms"]=(time.perf_counter()-start)*1000
        recovery={"key":"benchmark-interrupted-write","expires_at":int(time.time())+3600}
        arguments={"action":"stage","arguments":{"text":"Synthetic recoverable write for benchmark"}}
        interrupted_id=s.wire.send("chat-0","admin",arguments,recovery)
        # Terminate only this synthetic process tree without reading the acknowledgement.
        s.wire.sample()
        descendants=psutil.Process(s.wire.process.pid).children(recursive=True)
        s.wire.process.kill()
        for child in descendants:
            try:
                child.kill()
                child.wait(timeout=5)
            except psutil.Error:
                pass
        s.wire.close()
        s.wire=Wire(s.fixture)
        s.wire.sequence=int(interrupted_id)-1
        first=s.wire.call("chat-0","admin",arguments,recovery)
        s.wire.sequence=int(interrupted_id)-1
        second=s.wire.call("chat-0","admin",arguments,recovery)
        checks["restart_replay_stable"]=not error(first) and not error(second) and first["result"]==second["result"]
        checks["interrupted_write_exactly_one_stage"]=s.wire.admin("list",{"kind":"stage","status":"active"})["count"]==1
        checks["restarted_integrity"]=s.wire.admin("verify")["verified"]
        s.wire.close()
        s.h.config["sessions"][8]["scope"]="synthetic:isolated"
        s.h.config["sessions"][9].update(use_memories=False,generate_memories=False)
        s.h.file.write_text(json.dumps(s.h.config),encoding="utf-8")
        s.wire=Wire(s.fixture)
        checks["scope_isolation"]=len(unpack(s.wire.recall("release approval",session="chat-8"))[1])==0
        checks["disabled_memory_rejected"]=bool(error(s.wire.recall("release approval",session="chat-9")))
        return {"kind":"faults_and_privacy","checks":checks,"all_checks_pass":all(v is True for k,v in checks.items() if not k.endswith("_ms"))}
    finally:
        s.close()


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--phase",choices=["quality","load","growth","soak","faults","projects","warm","unique","burst","all"],default="all")
    p.add_argument("--load-seconds",type=float,default=8)
    p.add_argument("--soak-seconds",type=float,default=120)
    p.add_argument("--read-workers",type=int,choices=range(1,9))
    p.add_argument("--cpu-budget-percent",type=float,default=50)
    p.add_argument("--memory-budget-percent",type=float,default=15)
    p.add_argument("--min-free-memory-percent",type=float,default=15)
    p.add_argument("--adaptive-priority",dest="adaptive_priority",action="store_true",default=True,
                   help="Give queued foreground work one additional inference lane (default).")
    p.add_argument("--no-adaptive-priority",dest="adaptive_priority",action="store_false",
                   help="Use the previous fixed foreground/background reservation policy for comparison.")
    p.add_argument("--defer-index-on-interactive",dest="defer_index_on_interactive",action="store_true",default=True,
                   help="Pause maintenance index writes while a durable interactive write is in flight (default).")
    p.add_argument("--no-defer-index-on-interactive",dest="defer_index_on_interactive",action="store_false",
                   help="Allow maintenance index writes during interactive writes for comparison.")
    p.add_argument("--rerank-deadline-seconds",type=float,default=.25,
                   help="Maximum foreground reranker wait before falling back to source-checked candidates.")
    p.add_argument("--load-clients",type=int,nargs="+",choices=(10,32,64),default=[10,32,64])
    p.add_argument("--load-rates",type=int,nargs="+",choices=(25,100,300),default=[25,100,300])
    p.add_argument("--storage",choices=["sqlcipher","plaintext"],default="sqlcipher")
    p.add_argument("--verification",choices=["interactive","maintenance"],default="interactive")
    p.add_argument("--source-bound",action="store_true")
    p.add_argument("--growth-sizes",type=int,nargs="+",default=[24,256,1024])
    args=p.parse_args()
    if sorted(set(args.growth_sizes))!=args.growth_sizes or min(args.growth_sizes)<24:
        p.error("Growth sizes must increase from at least the 24-record seed")
    if sorted(set(args.load_clients))!=args.load_clients:
        p.error("Load clients must be strictly increasing")
    if sorted(set(args.load_rates))!=args.load_rates:
        p.error("Load rates must be strictly increasing")
    Scenario.read_workers=args.read_workers
    Scenario.storage=args.storage
    Scenario.resource_policy={"cpu_percent":args.cpu_budget_percent,"memory_percent":args.memory_budget_percent,
                              "free_percent":args.min_free_memory_percent,"adaptive_priority":args.adaptive_priority,
                              "defer_index_on_interactive":args.defer_index_on_interactive,
                              "rerank_deadline_seconds":args.rerank_deadline_seconds}
    args.output.mkdir(exist_ok=False,parents=True)
    meta={"started_at":datetime.now(timezone.utc).isoformat(),"synthetic_only":True,"platform":platform.platform(),
          "read_workers":args.read_workers or 4,
          "verification_mode":args.verification,
          "storage":{"mode":args.storage,"encrypted":args.storage=="sqlcipher",
                     "binary":str(fixtures.BINARY.resolve()),"synchronous":"FULL","journal_mode":"WAL"},
          "runtime_environment":{
              "embedding_profile":os.environ.get("MEMORYCORE_AI_EMBEDDING_PROFILE","bge-int8"),
              "reranker":os.environ.get("MEMORYCORE_AI_RERANKER","Xenova/ms-marco-MiniLM-L-6-v2"),
              "cpu_budget_percent":args.cpu_budget_percent,"memory_budget_percent":args.memory_budget_percent,
              "min_free_memory_percent":args.min_free_memory_percent,"adaptive_priority":args.adaptive_priority,
              "defer_index_on_interactive":args.defer_index_on_interactive,
              "rerank_deadline_seconds":args.rerank_deadline_seconds},
          "workload_contract":{"mixed":"mixed-fixed-chat-schedule/2",
              "corpus_sha256":hashlib.sha256(json.dumps(CORPUS,separators=(",",":"),ensure_ascii=True).encode()).hexdigest(),
              "growth":"source-bound-warehouse-ordinal/1","growth_recall_indices":list(range(12)),"growth_repeats":2},
          "source_bound_mutations":args.source_bound,"growth_sizes":args.growth_sizes,
          "python":sys.version,"psutil":psutil.__version__,"logical_cpus":psutil.cpu_count(),"ram_bytes":psutil.virtual_memory().total,
          "binary_sha256":digest(os.environ["MEMORYCORE_AI_SQLCIPHER_BINARY"]),
          "host_root":str(HOST_ROOT),
          "host_source_hashes":{str(p.relative_to(HOST_ROOT)):digest(p) for p in sorted((HOST_ROOT/"scripts").glob("*.py"))},
          "source_hashes":{str(p.relative_to(ROOT)):digest(p) for p in sorted((ROOT/"scripts").glob("*.py"))},
          "dependency_hashes":{str(p.relative_to(ROOT)):digest(p) for p in [ROOT/"scripts/model-assets-lock.json",ROOT/"scripts/vector-requirements.txt",ROOT/"rust-broker/Cargo.lock"]},
          "limits":["Local synthetic benchmark, not production certification.","Response token counts use o200k_base, not account usage or billing.",
                    "RSS is summed over the process tree and can double-count shared pages.","CPU core equivalents are aggregate process CPU seconds per elapsed second.",
                    "Quality corpus is small, hand-authored and not blinded; negative matches measure relevance, not invented answers.",
                    "The fixed chat schedule rotates 60% recall,20% insert,10% correction and10% purge; each chat starts with a disposable target.",
                    "Successful-response latency excludes rejects; offered/client-backpressure/error counts are separate."]}
    (args.output/"manifest.json").write_text(json.dumps(meta,indent=2),encoding="utf-8")
    index=0
    def save(result):
        nonlocal index
        index+=1
        path=args.output/f"{index:02}-{result['kind']}.json"
        path.write_text(json.dumps(result,indent=2),encoding="utf-8")
        print(json.dumps({"saved":str(path),"kind":result["kind"],"errors":result.get("errors"),"semantic_fraction":result.get("semantic_fraction"),"p99_ms":result.get("latency_ms",{}).get("p99")}),flush=True)
    if args.phase in {"quality","all"}:
        save(quality())
    if args.phase in {"load","all"}:
        for clients in args.load_clients:
            for rate in args.load_rates:
                s=Scenario(clients)
                try:
                    s.seed()
                    save(workload(s,clients,rate,args.load_seconds,f"load-{clients}-{rate}",source_bound=args.source_bound))
                finally:
                    s.close()
    if args.phase in {"growth","all"}:
        def checkpoint_growth(row):
            path=args.output/f"growth-stage-{row['memories']:06}.json"
            with path.open("x",encoding="utf-8") as stream:
                json.dump(row,stream,indent=2)
        save(growth(args.source_bound,args.growth_sizes,checkpoint=checkpoint_growth,verification=args.verification))
    if args.phase in {"projects","all"}:
        for clients in [10,100]:
            s=Scenario(clients,projects=10)
            try:
                s.seed()
                save(workload(s,clients,100,args.load_seconds,f"projects-10-chats-{clients}",source_bound=args.source_bound))
            finally:
                s.close()
    if args.phase in {"soak","all"}:
        s=Scenario(10)
        try:
            s.seed()
            save(workload(s,10,50,args.soak_seconds,"soak",source_bound=args.source_bound))
        finally:
            s.close()
    if args.phase in {"warm","unique","burst"}:
        s=Scenario(100,projects=10)
        try:
            assert s.seed()["drained"]
            if args.phase in {"warm","burst"}:
                warm_hits=0
                for project in range(10):
                    for i,row in enumerate(CORPUS):
                        warm_hits+=s.project_ids[project][i] in [r["id"] for r in unpack(s.wire.recall(row[2],session=f"chat-{project}"))[1]]
                save({"kind":"declared_prewarm","queries":240,"expected_hits":warm_hits})
            if args.phase=="burst":
                start=time.perf_counter()
                requests={}
                for i in range(100):
                    ident=s.wire.send(f"chat-{i}","call",{"name":"memory","arguments":{"recall":CORPUS[i%24][2]}})
                    requests[ident]=(i,time.perf_counter())
                results=[]
                for _ in range(100):
                    response=s.wire.queue.get(timeout=30)
                    client,sent=requests.pop(response["id"])
                    assert response["session"]==f"chat-{client}"
                    rows=unpack(response)[1] if not error(response) else []
                    results.append({"error":error(response),"ms":(time.perf_counter()-sent)*1000,
                        "hit":s.project_ids[client%10][client%24] in [r["id"] for r in rows]})
                save({"kind":"simultaneous_100_arrivals","seconds":time.perf_counter()-start,
                    "responses":len(results),"errors":sum(bool(r["error"]) for r in results),
                    "hits":sum(r["hit"] for r in results),"latency_ms":summary([r["ms"] for r in results]),"rows":results})
            else:
                save(workload(s,100,25 if args.phase=="unique" else 100,args.soak_seconds,
                    "cold_cache_busted_25" if args.phase=="unique" else "warm_100",unique=args.phase=="unique",source_bound=args.source_bound))
        finally:
            s.close()
    if args.phase in {"faults","all"}:
        save(faults())
    print(json.dumps({"completed":index,"directory":str(args.output)}),flush=True)


if __name__=="__main__":
    main()
