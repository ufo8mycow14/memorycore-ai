"""Bounded synthetic native-runtime growth and mixed-request measurements."""
import argparse
import hashlib
import json
import platform
import time
import os
from pathlib import Path
from datetime import datetime, timezone

from scripts import memorycore_ai as bm
from scripts.knowledge_layer import Knowledge, SourceRoot, source_binding
from scripts.test_rust_broker import Harness, packet, ROOT, BINARY
from scripts.evaluate_rust_broker import percentile


def process_memory(pids):
    if os.name != "nt":
        return {"available":False}
    import ctypes
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [("cb",wintypes.DWORD),("faults",wintypes.DWORD)] + [(name,ctypes.c_size_t) for name in
            ("peak_working","working","peak_paged","paged","peak_nonpaged","nonpaged","pagefile","peak_pagefile","private")]
    kernel = ctypes.WinDLL("kernel32",use_last_error=True)
    psapi = ctypes.WinDLL("psapi",use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
    values = []
    for pid in pids:
        handle = kernel.OpenProcess(0x410,False,pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            value = Counters()
            value.cb = ctypes.sizeof(value)
            if not psapi.GetProcessMemoryInfo(handle,ctypes.byref(value),value.cb):
                raise ctypes.WinError(ctypes.get_last_error())
            values.append({"working_set_bytes":value.working,"private_bytes":value.private})
        finally:
            kernel.CloseHandle(handle)
    return {"available":True,"process_count":len(values),"aggregate_private_mib":sum(v["private_bytes"] for v in values)/2**20,
            "aggregate_working_set_mib":sum(v["working_set_bytes"] for v in values)/2**20}


def run(size):
    h = Harness(records_per_scope=0)
    try:
        h.config["backend"] = "native"
        h.config.pop("python")
        h.config.pop("backend_root")
        for i, session in enumerate(h.config["sessions"][:10]):
            root = Path(session["source_root"])
            k = Knowledge(h.conn, scope=session["scope"], sources=SourceRoot(root), synthetic=True)
            with bm.transaction(h.conn):
                for n in range(size):
                    content = f"Fact: Service{i} item{n} uses SQLite.\nNever publish without approval.\n"
                    path = f"item{n}.md"
                    (root/path).write_text(content, encoding="utf-8")
                    raw = (root/path).read_bytes()
                    args = bm.build_parser().parse_args(["remember", "--scope", k.scope, "--type", "semantic",
                        "--subject", f"Service{i} item{n}", "--summary", content.strip(), "--source", path,
                        "--source-hash", hashlib.sha256(raw).hexdigest()])
                    saved = bm.remember(h.conn, args)
                    h.receipts[i,n] = saved
                    k._put("source", source_binding(path, raw), owner=saved["memory_id"])
        h.file.write_text(json.dumps(h.config), encoding="utf-8")
        ready = h.start()
        assert ready["backend"] == "rust-native"
        timings = {"read":[], "write":[]}
        queues = {"read":[], "write":[]}
        # Twenty warm mixed batches; each has seven recalls and three writes.
        for cycle in range(21):
            started = {}
            for i in range(10):
                write = (i+cycle)%3 == 0
                arguments = {"name":"memory", "arguments":{"propose":"item0.md"} if write else {"recall":f"item{size-1}"}}
                before = time.perf_counter()
                mid = h.send(f"chat-{i}", "call", arguments)
                started[mid] = before, write
            for _ in range(10):
                response = h.receive()
                before, write = started[response["id"]]
                elapsed = (time.perf_counter()-before)*1000
                body = packet(response)
                i = int(response["session"].split("-")[-1])
                if write:
                    assert f"Service{i}" in body["proposals"][0]["summary"]
                else:
                    assert f"Service{i}" in body["packet"]
                    assert all(f"Service{other}" not in body["packet"] for other in range(10) if other != i)
                if cycle:
                    lane = "write" if write else "read"
                    timings[lane].append(elapsed)
                    queues[lane].append(response["timing"]["queue_ms"])
        broad = {}
        for i in range(10):
            before = time.perf_counter()
            mid = h.send(f"chat-{i}", "call", {"name":"memory","arguments":{"recall":"SQLite"}})
            broad[mid] = before
        broad_ms = []
        for _ in range(10):
            response = h.receive()
            broad_ms.append((time.perf_counter()-broad[response["id"]])*1000)
            assert "SQLite" in packet(response)["packet"]
        for i, s in enumerate(h.config["sessions"][:10]):
            (Path(s["source_root"])/"item0.md").write_text(f"Fact: Service{i} item0 uses SQLite.\nNever publish without renewed approval.\n",encoding="utf-8")
            h.send(f"chat-{i}","call",{"name":"memory","arguments":{"propose":"item0.md"}})
        proposals = {}
        for _ in range(10):
            response=h.receive()
            proposals[response["session"]]=packet(response)["proposals"][0]
        rewrite_started={}
        for i in range(10):
            proposal=proposals[f"chat-{i}"]
            before=time.perf_counter()
            mid=h.send(f"chat-{i}","call",{"name":"memory","arguments":{"accept":proposal["id"],"review_digest":proposal["review_digest"],"supersedes":h.receipts[i,0]["memory_id"]}})
            rewrite_started[mid]=before
        rewrite_ms=[]
        for _ in range(10):
            response=h.receive()
            rewrite_ms.append((time.perf_counter()-rewrite_started[response["id"]])*1000)
            assert packet(response)["memory_id"]
        return {"records":size*10,"records_per_scope":size,"sessions":10,"measured_requests":200,"warmup_requests":10,
            "startup_ms":h.startup_ms,"errors":0,"memory_after_load":process_memory([h.process.pid]+ready["worker_pids"]),"narrow_mixed":{lane:{"requests":len(values),
                "round_trip_ms":{f"p{int(p*100)}":percentile(values,p) for p in (.5,.95,.99)},
                "queue_p95_ms":percentile(queues[lane],.95)} for lane,values in timings.items()},
            "broad_query_10_concurrent_ms":{f"p{int(p*100)}":percentile(broad_ms,p) for p in (.5,.95,.99)},
            "reviewed_corrections_10_concurrent_ms":{f"p{int(p*100)}":percentile(rewrite_ms,p) for p in (.5,.95,.99)}}
    finally:
        h.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--sizes", nargs="+",type=int,default=[10,100,1000])
    args = p.parse_args()
    if args.output.exists():
        raise ValueError("preserve previous evidence")
    results = []
    for size in args.sizes:
        if not 1 <= size <= 1500:
            raise ValueError("synthetic size outside supported knowledge limit")
        result = run(size)
        results.append(result)
        print(json.dumps(result), flush=True)
    with args.output.open("x",encoding="utf-8") as f:
        json.dump({"kind":"native-rust-growth-mixed-load","platform":platform.platform(),"backend":"native",
            "checked_at":datetime.now(timezone.utc).isoformat(),"binary_sha256":hashlib.sha256(BINARY.read_bytes()).hexdigest(),
            "source_sha256":{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/"rust-broker"/"src").rglob("*.rs"))},
            "cases":results,"limitations":["Synthetic local development fixtures, not a production capacity guarantee.",
            "Warm narrow reads and reviewed-proposal writes; not continuous deep rewrites or hostile input.",
            "Ordinary SQLite WAL, no encryption in this measurement.",
            "All fixtures and worker processes closed after measurement."]},f,indent=2)


if __name__ == "__main__":
    main()
