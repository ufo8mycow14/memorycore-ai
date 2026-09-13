"""Bounded, scoped integrity jobs outside the interactive broker worker queue."""
import concurrent.futures
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import uuid


class VerificationJobs:
    def __init__(self,binary,config,environment,budget,containment=None):
        self.binary=str(binary)
        self.configuration=copy.deepcopy(config)
        self.environment=dict(environment)
        self.budget=budget
        self.containment=containment
        self.sessions={s["id"]:s for s in self.configuration["sessions"]}
        self.lock=threading.Lock()
        self.stop=threading.Event()
        self.pool=concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.jobs={}
        self.requests={}

    def exchange(self,request):
        response={"id":request.get("id"),"session":request.get("session")}
        session=self.sessions.get(request.get("session")) if isinstance(request.get("session"),str) else None
        outer=request.get("arguments",{})
        args=outer.get("arguments") if isinstance(outer,dict) else None
        if not session or not session.get("allow_admin",False):
            return response|{"error":"maintenance_admin_required"}
        if (request.get("operation")!="admin" or set(request)-{"id","session","operation","arguments"}
                or not isinstance(request.get("id"),str) or not 1<=len(request["id"])<=128
                or not isinstance(outer,dict) or set(outer)!={"action","arguments"} or not isinstance(args,dict)):
            return response|{"error":"invalid_maintenance_request"}
        with self.lock:
            if self.stop.is_set():
                return response|{"error":"maintenance_closed"}
            if outer["action"]=="verify-start" and not args:
                key=(session["id"],request["id"])
                ident=self.requests.get(key)
                if ident is None:
                    if any(j["state"] in {"queued","running"} for j in self.jobs.values()):
                        return response|{"error":"verification_busy"}
                    # Keep receipts bounded without silently forgetting replay identities.
                    if len(self.jobs)>=64:
                        return response|{"error":"verification_receipt_limit"}
                    ident=uuid.uuid4().hex
                    job={"job_id":ident,"state":"queued","session":session["id"],"scope":session["scope"],
                         "verified":False,"snapshot_semantics":True,"persistence":"host_lifetime"}
                    self.jobs[ident]=job
                    self.requests[key]=ident
                    self.pool.submit(self.run,ident)
            elif outer["action"] in {"verify-status","verify-forget"} and set(args)=={"job_id"} and isinstance(args["job_id"],str):
                ident=args["job_id"]
                if ident not in self.jobs or self.jobs[ident]["session"]!=session["id"]:
                    return response|{"error":"verification_job_unavailable"}
                if outer["action"]=="verify-forget":
                    if self.jobs[ident]["state"] in {"queued","running"}:
                        return response|{"error":"verification_still_active"}
                    self.jobs.pop(ident)
                    self.requests={key:value for key,value in self.requests.items() if value!=ident}
                    return response|{"lane":"maintenance","result":{"job_id":ident,"forgotten":True}}
            else:
                return response|{"error":"invalid_maintenance_request"}
            return response|{"lane":"maintenance","result":dict(self.jobs[ident])}

    def run(self,ident):
        start=time.monotonic()
        acquired=False
        outcome={"state":"failed","verified":False}
        self.budget.queue("background",1)
        try:
            while not self.stop.is_set() and time.monotonic()-start<90:
                if self.budget.acquire("background"):
                    acquired=True
                    break
                self.stop.wait(.05)
            if not acquired:
                raise RuntimeError("cancelled" if self.stop.is_set() else "resource_wait_timeout")
            with self.lock:
                self.jobs[ident]["state"]="running"
                job=dict(self.jobs[ident])
            self.verify(job,start)
            outcome={"state":"complete","verified":True}
        except Exception as exc:
            reason=str(exc)
            if reason not in {"cancelled","resource_wait_timeout","deadline","native_rejected","invalid_response"}:
                reason="verification_failed"
            outcome={"state":"failed","error":reason,"verified":False}
        finally:
            self.budget.queue("background",-1)
            if acquired:
                self.budget.release("background")
            with self.lock:
                self.jobs[ident].update(outcome,elapsed_ms=(time.monotonic()-start)*1000)

    def verify(self,job,start):
        configuration=dict(self.configuration,sessions=[self.sessions[job["session"]]])
        with tempfile.TemporaryDirectory(prefix="memorycore-ai-verification-") as root:
            path=Path(root)/"config.json"
            path.write_text(json.dumps(configuration),encoding="utf-8")
            request={"session":job["session"],"id":job["job_id"],"operation":"admin",
                     "arguments":{"action":"verify","arguments":{}}}
            process=subprocess.Popen([self.binary,"--native-command","--config",str(path)],env=self.environment,
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
            data=json.dumps(request).encode()
            try:
                if self.containment is not None:
                    self.containment.assign(process.pid)
                while True:
                    if self.stop.is_set():
                        raise RuntimeError("cancelled")
                    if time.monotonic()-start>=90:
                        raise RuntimeError("deadline")
                    try:
                        stdout,stderr=process.communicate(input=data,timeout=.2)
                        break
                    except subprocess.TimeoutExpired:
                        data=None
                if process.returncode:
                    raise RuntimeError("native_rejected")
                if len(stdout)>65536 or len(stderr)>65536:
                    raise RuntimeError("invalid_response")
                result=json.loads(stdout)
                if (result.get("id")!=job["job_id"] or result.get("result",{}).get("verified") is not True
                        or result["result"].get("scope")!=job["scope"] or "error" in result):
                    raise RuntimeError("invalid_response")
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate()

    def close(self):
        with self.lock:
            self.stop.set()
        self.pool.shutdown(wait=True,cancel_futures=False)
