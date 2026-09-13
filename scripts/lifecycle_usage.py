"""Whole-task token accounting from explicit provider observations, never byte proxies."""
import argparse
import json
from pathlib import Path

PHASES={"ingest","consolidate","answer","verify","retry","recover"}


def count(value):
    if type(value) is not int or not 0<=value<=10**12:
        raise ValueError("Non-negative observed integer token count required")
    return value


def summarise(arm):
    if set(arm["coverage"])!=PHASES or any(v not in {"observed","no_calls","unknown"} for v in arm["coverage"].values()):
        raise ValueError("Declare coverage for every lifecycle phase")
    totals=dict(input=0,cached_input=0,uncached_input=0,output=0,reasoning=0,total=0,calls=0,failed_calls=0)
    seen=set()
    unknown={key for key,value in arm["coverage"].items() if value=="unknown"}
    phases=set()
    for call in arm["calls"]:
        identity=call["call_id"]
        if identity in seen or call["phase"] not in PHASES:
            raise ValueError("Duplicate call or unsupported phase")
        seen.add(identity)
        phases.add(call["phase"])
        if arm["coverage"][call["phase"]]=="no_calls":
            raise ValueError("No-call declaration contradicts observed usage")
        if call["status"] not in {"completed","failed","cancelled"}:
            raise ValueError("Unknown call outcome")
        totals["calls"]+=1
        totals["failed_calls"]+=call["status"]!="completed"
        fields=("input_tokens","cached_input_tokens","output_tokens","reasoning_tokens")
        if any(call.get(key) is None for key in fields):
            unknown.add(call["phase"])
            continue
        inputs,cached,output,reasoning=(count(call[key]) for key in fields)
        if cached>inputs or reasoning>output:
            raise ValueError("Inconsistent provider token subsets")
        totals["input"]+=inputs
        totals["cached_input"]+=cached
        totals["uncached_input"]+=inputs-cached
        totals["output"]+=output
        totals["reasoning"]+=reasoning
        totals["total"]+=inputs+output  # Reasoning is a subset of output, not an extra charge.
    unknown.update(phase for phase,status in arm["coverage"].items() if status=="observed" and phase not in phases)
    return {"complete":not unknown,"unknown_phases":sorted(unknown),"observed_tokens":totals}


def compare(document):
    if document.get("format")!="memory-lifecycle-usage/1":
        raise ValueError("Unsupported usage format")
    baseline=summarise(document["baseline"])
    repaired=summarise(document["repaired"])
    tasks=document["paired_tasks"]
    if not tasks or len({r["id"] for r in tasks})!=len(tasks):
        raise ValueError("Unique paired task outcomes required")
    for row in tasks:
        if any(type(row[key]) is not bool for key in ("baseline_pass","repaired_pass")):
            raise ValueError("Observed paired quality outcomes required")
    declared={row["id"] for row in tasks}
    for arm in (document["baseline"],document["repaired"]):
        if any(call["task"] not in declared for call in arm["calls"]):
            raise ValueError("Usage includes an unpaired task")
        if {call["task"] for call in arm["calls"] if call["phase"]=="answer"}!=declared:
            raise ValueError("Every paired task needs answer-call accounting in both arms")
    same_model=document["baseline"].get("model")==document["repaired"].get("model")
    matched=bool(document["baseline"].get("model")) and same_model and document.get("matched_conditions") is True
    complete=baseline["complete"] and repaired["complete"] and matched
    result={"baseline":baseline,"repaired":repaired,"comparable":complete,
            "quality_regressions":sum(r["baseline_pass"] and not r["repaired_pass"] for r in tasks),
            "quality_improvements":sum(r["repaired_pass"] and not r["baseline_pass"] for r in tasks),
            "total_token_saving":None,"uncached_input_saving":None,"cost_saving":None,
            "limitations":["Validates supplied observations; it cannot establish their provenance or grading independence.",
                          "No conversion from tokens to subscription quota or monetary cost."]}
    if complete:
        for output,key in (("total_token_saving","total"),("uncached_input_saving","uncached_input")):
            before=baseline["observed_tokens"][key]
            result[output]=1-repaired["observed_tokens"][key]/before if before else None
    result["savings_target_met"]=complete and result["quality_regressions"]==0 and (
        (result["total_token_saving"] or 0)>=.30 and (result["uncached_input_saving"] or 0)>=.15)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input",type=Path)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    result=compare(json.loads(args.input.read_text(encoding="utf-8")))
    with args.output.open("x",encoding="utf-8") as stream:
        json.dump(result,stream,indent=2)
    print(json.dumps(result))


if __name__=="__main__":
    main()
