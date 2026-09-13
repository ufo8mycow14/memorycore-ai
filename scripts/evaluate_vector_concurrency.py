"""Synthetic encrypted open-loop mixed-load measurement; no deployment capacity claim."""
import argparse
import json
import queue
import time
from pathlib import Path
from scripts import test_rust_encryption as fixtures


def percentile(values, fraction):
    values=sorted(values)
    return values[min(len(values)-1,int((len(values)-1)*fraction))] if values else None


def measure(clients,batch_size,rate,seconds):
    fixture=fixtures.EncryptedNativeTests()
    fixture.setUp()
    h=fixture.h
    try:
        seed=fixture.command("remember",{"type":"semantic","subject":"Shared synthetic seed","summary":"Synthetic maintenance schedule."})["memory_id"]
        h.config["sessions"]=[dict(h.config["sessions"][0],id=f"chat-{i}") for i in range(clients)]
        h.file.write_text(json.dumps(h.config),encoding="utf-8")
        fixture.command("vector-configure",{"model":"synthetic-load-v1","dimensions":64})
        h.start()
        pending={}
        available=list(range(clients))
        latencies=[]
        queues=[]
        services=[]
        errors={}
        submitted=completed=mutations=reads=blocked=0
        start=time.perf_counter()
        offered=0
        while time.perf_counter()-start<seconds or pending:
            now=time.perf_counter()
            if now-start<seconds:
                due=min(int((now-start)*rate)+1,int(seconds*rate))
                while offered<due:
                    serial=offered
                    offered+=1
                    if not available:
                        blocked+=1
                        continue
                    session=available.pop()
                    if serial%4==0:
                        args={"action":"recall","arguments":{"query":"maintenance"}}
                        count=0
                    elif serial%4==1:
                        args={"action":"lifecycle","arguments":{"memory_id":seed,"action":"pin" if (serial//4)%2 else "unpin"}}
                        count=1
                    else:
                        items=[{"action":"remember","arguments":{"type":"semantic","subject":f"Synthetic {serial} {j}","summary":f"Synthetic maintenance entry {serial} item {j}."}} for j in range(batch_size)]
                        args=items[0] if batch_size==1 else {"action":"batch","arguments":{"items":items}}
                        count=batch_size
                    ident=h.send(f"chat-{session}","admin",args)
                    pending[ident]=(session,time.perf_counter(),count)
                    submitted+=1
            try:
                response=h.responses.get(timeout=0.001)
            except queue.Empty:
                continue
            if response is None:
                raise RuntimeError("Broker stopped")
            session,sent,count=pending.pop(response["id"])
            available.append(session)
            reason=response.get("error") or response.get("result",{}).get("error")
            if reason:
                reason=str(reason)
                errors[reason]=errors.get(reason,0)+1
            else:
                completed+=1
                mutations+=count
                reads+=count==0
                latencies.append((time.perf_counter()-sent)*1000)
                queues.append(response["timing"]["queue_ms"])
                services.append(response["timing"]["service_ms"])
        elapsed=time.perf_counter()-start
        h.send("chat-0","admin",{"action":"verify","arguments":{}})
        assert h.receive()["result"]["verified"]
        wal=Path(str(fixture.target)+"-wal")
        return {"clients":clients,"batch_size":batch_size,"offered_requests_per_second":rate,"offered":offered,
                "submitted":submitted,"client_backpressure":blocked,"completed":completed,"errors":errors,
                "elapsed_seconds":elapsed,"completed_requests_per_second":completed/elapsed,
                "committed_mutations":mutations,"mutations_per_second":mutations/elapsed,"reads":reads,
                "latency_ms":{k:{"p50":percentile(v,.50),"p95":percentile(v,.95),"p99":percentile(v,.99),"max":max(v) if v else None} for k,v in [("end_to_end",latencies),("queue",queues),("service",services)]},
                "wal_bytes":wal.stat().st_size if wal.exists() else 0,"integrity_verified":True}
    finally:
        fixture.doCleanups()


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--seconds",type=float,default=4)
    p.add_argument("--rate",type=int,default=300)
    args=p.parse_args()
    if args.output.exists():
        raise ValueError("Preserve prior evidence")
    results=[]
    for clients in [10,32,64]:
        for size in [1,4,8]:
            row=measure(clients,size,args.rate,args.seconds)
            results.append(row)
            print(json.dumps(row),flush=True)
    receipt={"synthetic_only":True,"encrypted":True,"results":results,
             "limits":["Short local open-loop runs; not a capacity guarantee or full saturation sweep.",
                       "Shared scope; insert batches, lifecycle rewrites and lexical reads; vector outbox enabled but no embedding worker.",
                       "Successful requests only in latency percentiles; rejected requests and client backpressure reported separately.",
                       "No independent fsync, process RSS or cold vector rebuild measurement."]}
    with args.output.open("x",encoding="utf-8") as f:
        json.dump(receipt,f,indent=2)


if __name__=="__main__":
    main()
