"""Local embedding maintenance commands for the standard semantic memory host."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import queue
import threading
import concurrent.futures
from scripts.model_assets import reviewed_model
from scripts.query_projection import search_query, VERSION as QUERY_PROJECTION
from scripts.vector_transport import pack_vector

MODEL_NAME = "BAAI/bge-small-en-v1.5"
INT8_MODEL_NAME = "memorycore-ai/bge-small-en-v1.5-int8"
RERANKER_NAME = "Xenova/ms-marco-MiniLM-L-6-v2"
FAST_RERANKER_NAME = "memorycore-ai/minilm-l6-range7"
DEFAULT_RERANKER_NAME = FAST_RERANKER_NAME


class LocalReranker:
    def __init__(self, cache, *, download=False, threads=1, model_name=DEFAULT_RERANKER_NAME):
        if model_name not in {RERANKER_NAME,FAST_RERANKER_NAME,"jinaai/jina-reranker-v1-tiny-en","mixedbread-ai/mxbai-rerank-xsmall-v1"}:
            raise ValueError("Unreviewed reranker")
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        os.environ["DO_NOT_TRACK"] = "1"
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        if not download:
            os.environ["HF_HUB_OFFLINE"] = "1"
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        reviewed=reviewed_model(cache,model_name,download) if model_name in {RERANKER_NAME,FAST_RERANKER_NAME,"mixedbread-ai/mxbai-rerank-xsmall-v1"} else None
        if model_name=="mixedbread-ai/mxbai-rerank-xsmall-v1" and not any(m["model"]==model_name for m in TextCrossEncoder.list_supported_models()):
            from fastembed.common.model_description import ModelSource
            TextCrossEncoder.add_custom_model(model=model_name,sources=ModelSource(hf=model_name),
                model_file="onnx/model_quantized.onnx",license="apache-2.0",size_in_gb=.07)
        self.model = TextCrossEncoder(model_name=RERANKER_NAME if model_name==FAST_RERANKER_NAME else model_name, cache_dir=str(cache),
            threads=threads, providers=["CPUExecutionProvider"], local_files_only=not download,
            specific_model_path=str(reviewed[0]) if reviewed else None)
        directory=Path(self.model.model._model_dir)
        assets=[]
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.suffix in {".onnx", ".json", ".txt"}:
                digest=hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda:stream.read(1024*1024),b""):
                        digest.update(block)
                assets.append({"name":path.relative_to(directory).as_posix(),"sha256":digest.hexdigest()})
        if reviewed:
            assets=reviewed[1]
        if not assets:
            raise ValueError("Reranker assets unavailable")
        # Dynamic quantisation depends on batch composition; cached scores need a
        # stable per-document execution contract for the experimental model.
        self.batch_size=1 if model_name in {FAST_RERANKER_NAME,"mixedbread-ai/mxbai-rerank-xsmall-v1"} else 4
        self.manifest={"name":model_name,"license":"apache-2.0","assets":assets,"batch_size":self.batch_size,
                       "query_projection":QUERY_PROJECTION,
                       "fastembed":importlib.metadata.version("fastembed"),
                       "onnxruntime":importlib.metadata.version("onnxruntime")}
        profile={RERANKER_NAME:"minilm-reranker", FAST_RERANKER_NAME:"minilm-reranker", "jinaai/jina-reranker-v1-tiny-en":"jina-tiny-reranker",
                 "mixedbread-ai/mxbai-rerank-xsmall-v1":"mxbai-xsmall-reranker"}[model_name]
        self.identity=profile+":"+hashlib.sha256(json.dumps(self.manifest,sort_keys=True).encode()).hexdigest()
        self.manifest["model"]=self.identity

    def rerank(self, value):
        query=value["query"]
        documents=value["documents"]
        if (not isinstance(query,str) or len(query.encode())>4096 or not isinstance(documents,list)
                or not 1<=len(documents)<=16 or any(not isinstance(d,str) or len(d)>4096 for d in documents)):
            raise ValueError("Reranker input bound")
        return [float(score) for score in self.model.rerank(search_query(query),documents,batch_size=self.batch_size)]


class LocalModel:
    def __init__(self, cache, *, download=False, threads=2, profile="bge-int8"):
        if profile not in {"bge-int8", "bge-legacy"}:
            raise ValueError("Unreviewed embedding profile")
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        os.environ["DO_NOT_TRACK"] = "1"
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        if not download:
            os.environ["HF_HUB_OFFLINE"] = "1"
        from fastembed import TextEmbedding
        runtime_name=INT8_MODEL_NAME if profile=="bge-int8" else MODEL_NAME
        directory,assets=reviewed_model(cache,runtime_name,download)
        if profile=="bge-int8" and not any(m["model"]==runtime_name for m in TextEmbedding.list_supported_models()):
            from fastembed.common.model_description import ModelSource, PoolingType
            TextEmbedding.add_custom_model(model=runtime_name,pooling=PoolingType.CLS,normalization=True,
                sources=ModelSource(hf="Xenova/bge-small-en-v1.5"),dim=384,
                model_file="onnx/model_int8.onnx",license="mit",size_in_gb=.034)
        self.model = TextEmbedding(model_name=runtime_name, cache_dir=str(cache),
                                   threads=threads, providers=["CPUExecutionProvider"],
                                   local_files_only=not download,specific_model_path=str(directory))
        self.manifest = {"name":MODEL_NAME,"dimensions":384,"license":"MIT",
                         "query_projection":QUERY_PROJECTION,
                         "projection":"subject-summary-detail:4096-chars;model-512-token-limit:v1",
                         "assets":assets,"dependencies":{p:importlib.metadata.version(p) for p in ["fastembed","onnxruntime","tokenizers","numpy"]}}
        if profile=="bge-int8":
            self.manifest["inference_profile"]={"name":profile,"pooling":"CLS","normalised":True,"max_tokens":512}
        digest = hashlib.sha256(json.dumps(self.manifest,sort_keys=True).encode()).hexdigest()
        self.identity = "bge-small-en-v1.5:"+digest
        self.manifest["model"] = self.identity

    def passages(self, texts):
        return [v.tolist() for v in self.model.passage_embed(texts,batch_size=8,parallel=None)]

    def query(self, text):
        if not isinstance(text,str) or not text.strip() or len(text.encode())>4096:
            raise ValueError("Query bound exceeded")
        return next(iter(self.model.query_embed(search_query(text)))).tolist()


class BrokerClient:
    def __init__(self,binary,config,session,environment=None):
        host=json.loads(Path(config).read_text(encoding="utf-8"))
        if host.get("synthetic") is not True or host.get("backend","native")!="native":
            raise ValueError("Explicit synthetic native host required")
        self.session=session
        self.process=subprocess.Popen([str(binary),"--config",str(config)],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
        self.responses=queue.Queue(maxsize=1)
        def receive():
            try:
                while True:
                    raw=self.process.stdout.readline(1024*1024+1)
                    self.responses.put(raw,timeout=45)
                    if not raw or len(raw)>1024*1024:
                        return
            except (OSError,ValueError,queue.Full):
                return
        self.reader=threading.Thread(target=receive,daemon=True)
        self.reader.start()
        try:
            if self.read().get("event")!="ready":
                raise ValueError("Native broker unavailable")
        except Exception:
            self.close()
            raise

    def read(self):
        try:
            raw=self.responses.get(timeout=45)
        except queue.Empty as exc:
            raise TimeoutError("Native broker response deadline exceeded") from exc
        if not raw.endswith(b"\n") or len(raw)>1024*1024:
            raise ValueError("Invalid broker response")
        return json.loads(raw)

    def call(self,action,arguments):
        request={"session":self.session,"id":uuid.uuid4().hex,"operation":"admin",
                 "arguments":{"action":action,"arguments":arguments}}
        raw=json.dumps(request,separators=(",",":"),allow_nan=False).encode()+b"\n"
        if len(raw)>65536:
            raise ValueError("Request bound exceeded")
        self.process.stdin.write(raw)
        self.process.stdin.flush()
        response=self.read()
        if response.get("id")!=request["id"] or response.get("session")!=self.session or "error" in response:
            raise ValueError("Broker request rejected; inspect host policy and durable state")
        return response["result"]

    def close(self):
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()
        self.reader.join(timeout=1)


def drain(client,model,max_batches=100):
    stored=stale=0
    for _ in range(max_batches):
        batch=client.call("vector-claim",{})
        if batch["model"]!=model.identity:
            raise ValueError("Embedding model differs; explicit configure required")
        jobs=batch["jobs"]
        if not jobs:
            return {"stored":stored,"stale":stale,"drained":True}
        vectors=model.passages([j["text"] for j in jobs])
        if len(vectors)!=len(jobs):
            raise ValueError("Embedding batch mismatch")
        items=[{"id":j["id"],"checksum":j["checksum"],"lease":j["lease"],"vector":v} for j,v in zip(jobs,vectors)]
        for start in range(0,len(items),4):
            receipt=client.call("vector-put",{"model":model.identity,"items":items[start:start+4]})
            stored+=receipt["stored"]
            stale+=receipt["stale"]
    return {"stored":stored,"stale":stale,"drained":False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("action",choices=["download-model","download-reranker","configure","drain","watch","query","serve","embedding-server"])
    p.add_argument("--cache",required=True,type=Path)
    p.add_argument("--binary",type=Path)
    p.add_argument("--config",type=Path)
    p.add_argument("--session",default="chat-0")
    p.add_argument("--threads",type=int,choices=[1,2],default=2)
    p.add_argument("--supervised",action="store_true")
    p.add_argument("--rerank",action="store_true")
    p.add_argument("--reranker-name",default=DEFAULT_RERANKER_NAME)
    p.add_argument("--embedding-profile",choices=["bge-int8","bge-legacy"],default="bge-int8")
    p.add_argument("--parallel-slots",type=int,choices=range(1,9),default=1)
    args=p.parse_args()
    if args.supervised:
        if args.action!="embedding-server" or sys.stdin.buffer.readline(64)!=b'{"start":true}\n':
            raise ValueError("Model start handshake required")
    if args.action=="download-reranker":
        print(json.dumps(LocalReranker(args.cache,download=True,threads=args.threads,model_name=args.reranker_name).manifest))
        return
    model=LocalModel(args.cache,download=args.action=="download-model",threads=args.threads,profile=args.embedding_profile)
    if args.action=="download-model":
        print(json.dumps(model.manifest))
        return
    if args.action=="embedding-server":
        reranker=LocalReranker(args.cache,threads=args.threads,model_name=args.reranker_name) if args.rerank else None
        print(json.dumps({"ready":True,"model":model.identity,"reranker":reranker.identity if reranker else None}),flush=True)
        output_lock=threading.Lock()
        capacity=threading.Semaphore(args.parallel_slots)
        def handle(request):
            response={"id":request.get("id")}
            try:
                if request.get("method")=="query":
                    vector=model.query(request["value"])
                elif request.get("method")=="rerank" and reranker:
                    vector=reranker.rerank(request["value"])
                elif request.get("method")=="passages":
                    values=request["value"]
                    if not isinstance(values,list) or not 1<=len(values)<=8 or any(not isinstance(v,str) or len(v)>4096 for v in values):
                        raise ValueError("Passage bound")
                    vector=[pack_vector(v) for v in model.passages(values)]
                else:
                    raise ValueError("Unknown embedding method")
                response["vector"]=vector
            except Exception:
                response["error"]="model_request_failed"
            try:
                with output_lock:
                    print(json.dumps(response,allow_nan=False),flush=True)
            finally:
                capacity.release()
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel_slots) as executor:
            while True:
                raw=sys.stdin.buffer.readline(65537)
                if not raw:
                    break
                if len(raw)>65536 or not raw.endswith(b"\n"):
                    raise ValueError("Embedding request bound")
                request=json.loads(raw)
                if not isinstance(request,dict):
                    raise ValueError("Embedding object required")
                capacity.acquire()
                executor.submit(handle,request)
        return
    if args.binary is None or args.config is None:
        p.error("--binary and --config required")
    client=BrokerClient(args.binary,args.config,args.session)
    try:
        if args.action=="configure":
            print(json.dumps(client.call("vector-configure",{"model":model.identity,"dimensions":384})))
        elif args.action in {"drain","watch"}:
            while True:
                print(json.dumps(drain(client,model)),flush=True)
                if args.action=="drain":
                    break
                time.sleep(1)
        else:
            while True:
                raw=sys.stdin.buffer.readline(8193)
                if not raw:
                    break
                if len(raw)>8192:
                    raise ValueError("Input frame too large")
                query=json.loads(raw)["query"]
                arguments={"query":query,"model":model.identity,"vector":model.query(query)}
                print(json.dumps(client.call("vector-recall",arguments)),flush=True)
                if args.action=="query":
                    break
    finally:
        client.close()


if __name__=="__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        print("Local vector pipeline stopped; validate model cache, host config and durable state.",file=sys.stderr)
        sys.exit(1)
