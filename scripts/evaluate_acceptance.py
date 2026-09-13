"""Freeze then evaluate new synthetic histories; no provider-cost or answer claims."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import time

DOMAINS=[("observatory","telescope"),("museum","collection"),("rail depot","locomotive"),
    ("water laboratory","sampler"),("theatre","lighting rig"),("print workshop","press"),
    ("survey office","survey instrument"),("wind farm","turbine"),("textile studio","loom"),
    ("radio station","transmitter")]
NAMES=["Arden","Blair","Casey","Devon","Ellis","Finley","Harper","Jules","Morgan","Quinn"]


def freeze(path):
    facts=[]
    questions=[]
    for project,(site,object_name) in enumerate(DOMAINS):
        for number in range(10):
            code=f"PX{project+1:02}-{number+21:03}"
            owner=NAMES[(project+number)%10]
            day=["Monday","Tuesday","Wednesday","Thursday","Friday"][(number+project)%5]
            hour=8+(number+project)%8
            place=f"bay {number+41}"
            count=2+(number*3+project)%19
            subject=f"{site.title()} {object_name} {code}"
            text=f"{owner} is responsible for {object_name} {code} at the {site}. Its inspection happens every {day} at {hour:02}:00. It is kept in {place}. The approved inspection uses {count} reference samples."
            record={"key":code,"project":project,"subject":subject,"summary":text}
            if number%5==0:
                record["previous"]=text.replace(f"{hour:02}:00",f"{(hour+2)%24:02}:00")
            facts.append(record)
            positive=[f"Who is responsible for {object_name} {code}?",f"Which person looks after {object_name} {code}?",
                f"What day is {object_name} {code} inspected?",f"At what time does the current inspection of {object_name} {code} happen?",
                f"Where is {object_name} {code} kept?",f"How many reference samples are approved for {object_name} {code}?",
                f"Give the inspection schedule for {object_name} {code}."]
            negative=[f"What is the purchase price of {object_name} {code}?",f"Who manufactured {object_name} {code}?",
                f"What is the warranty expiry date for {object_name} {code}?"]
            for kind,queries in [("positive",positive),("no_answer",negative)]:
                for query in queries:
                    questions.append({"id":f"Q{len(questions)+1:04}","project":project,"kind":kind,
                        "query":query,"expected":code if kind=="positive" else None})
    document={"format":"memory-retrieval-acceptance/1","facts":facts,"questions":questions,
        "gates":{"recall":.95,"precision":.98,"no_answer_nonempty_max":.01},
        "declaration":["New synthetic histories, frozen before the first evaluation; no thresholds may be tuned on these results.",
            "100 facts, 20 corrections, 700 positive and 300 related-but-unanswerable questions across 10 scopes.",
            "Template-generated retrieval evidence, not independently graded free-form answers or a native memory comparison."]}
    with path.open("x",encoding="utf-8") as stream:
        json.dump(document,stream,indent=2)
    return {"frozen":str(path),"sha256":hashlib.sha256(path.read_bytes()).hexdigest(),"questions":len(questions)}


def interval(hits,total):
    if not total:
        return None
    p=hits/total
    z=1.95996398454
    divisor=1+z*z/total
    middle=(p+z*z/(2*total))/divisor
    width=z*math.sqrt(p*(1-p)/total+z*z/(4*total*total))/divisor
    return [max(0,middle-width),min(1,middle+width)]


def evaluate(source,output,regression_of=None):
    from scripts.benchmark_memory_host import Scenario,unpack,error,summary
    document=json.loads(source.read_text(encoding="utf-8"))
    if document["format"]!="memory-retrieval-acceptance/1" or output.exists():
        raise ValueError("Expected frozen histories and an exclusive output receipt")
    dataset_hash=hashlib.sha256(source.read_bytes()).hexdigest()
    if regression_of:
        previous=json.loads(regression_of.read_text(encoding="utf-8"))
        if previous["dataset_sha256"]!=dataset_hash:
            raise ValueError("Regression must reference the same preserved dataset")
    root=Path(__file__).resolve().parents[1]
    source_hashes={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((root/"scripts").glob("*.py"))}
    scenario=Scenario(100,projects=10)
    identities={}
    rows=[]
    try:
        for fact in document["facts"]:
            project=fact["project"]
            previous=None
            for text in ([fact["previous"]] if "previous" in fact else [])+[fact["summary"]]:
                arguments=scenario.bound_arguments(project,fact["subject"],text,**({"supersedes":previous} if previous else {}))
                previous=scenario.wire.admin("remember-bound",arguments,session=f"chat-{project}")["memory_id"]
            identities[fact["key"]]=previous
        assert scenario.catchup(120)["drained"]
        for question in document["questions"]:
            started=time.perf_counter()
            response=scenario.wire.recall(question["query"],session=f"chat-{question['project']}")
            failed=error(response)
            found=[] if failed else unpack(response)[1]
            expected=identities.get(question["expected"])
            rows.append({**question,"expected_id":expected,"returned":[r["id"] for r in found],
                "hit":expected is not None and expected in [r["id"] for r in found],"error":failed,
                "ms":(time.perf_counter()-started)*1000,
                "tool_tokens":None if failed else len(scenario.encoder.encode(response["result"]["result"]["content"][0]["text"])),
                "retrieval":response.get("semantic_host",{}).get("retrieval",{})})
            if len(rows)%100==0:
                print(json.dumps({"evaluated":len(rows),"total":len(document["questions"])}),flush=True)
        positives=[r for r in rows if r["kind"]=="positive"]
        negatives=[r for r in rows if r["kind"]=="no_answer"]
        hits=sum(r["hit"] for r in positives)
        delivered=sum(len(r["returned"]) for r in rows)
        nonempty=sum(bool(r["returned"]) for r in negatives)
        metrics={"positive_hits":hits,"positives":len(positives),"delivered_facts":delivered,
            "recall":hits/len(positives),"precision":hits/delivered if delivered else 0,
            "no_answer_nonempty":nonempty,"no_answer_questions":len(negatives),"no_answer_rate":nonempty/len(negatives),
            "recall_ci95":interval(hits,len(positives)),"precision_ci95":interval(hits,delivered),
            "no_answer_ci95":interval(nonempty,len(negatives)),"latency_ms":summary([r["ms"] for r in rows]),
            "errors":sum(bool(r["error"]) for r in rows)}
        gates=document["gates"]
        metrics["gate_pass"]=metrics["recall"]>=gates["recall"] and metrics["precision"]>=gates["precision"] and metrics["no_answer_rate"]<=gates["no_answer_nonempty_max"] and not metrics["errors"]
        receipt={"classification":"known-synthetic-retrieval-regression" if regression_of else "frozen-synthetic-retrieval-acceptance",
            "dataset_sha256":dataset_hash,"gates":gates,"source_hashes":source_hashes,
            "binary_sha256":hashlib.sha256(Path(os.environ["MEMORYCORE_AI_SQLCIPHER_BINARY"]).read_bytes()).hexdigest(),
            "regression_of_sha256":hashlib.sha256(regression_of.read_bytes()).hexdigest() if regression_of else None,
            "metrics":metrics,"rows":rows,"dataset_declaration":document["declaration"],
            "limitations":["Template-generated retrieval, not independently graded answers or a native memory comparison.",
                "Wilson intervals treat questions as independent; shared templates and histories make that assumption optimistic.",
                "Dataset has been used for diagnosis; passing this rerun is not fresh acceptance." if regression_of else
                "Generator was inspected before this run; this is not genuinely blind or independently reviewed acceptance."],
            "production_approved":False}
        with output.open("x",encoding="utf-8") as stream:
            json.dump(receipt,stream,indent=2)
        return metrics
    finally:
        scenario.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=["freeze","run"])
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--input",type=Path)
    parser.add_argument("--regression-of",type=Path)
    args=parser.parse_args()
    if args.action=="run" and args.input is None:
        parser.error("A frozen --input dataset is required")
    print(json.dumps(freeze(args.output) if args.action=="freeze" else evaluate(args.input,args.output,args.regression_of)),flush=True)


if __name__=="__main__":
    main()
