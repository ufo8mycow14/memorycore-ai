"""Explicit synthetic local routing workbench; never creates or sends chats."""
import argparse
import json
import sys
from pathlib import Path
from . import memorycore_ai as bm
from .knowledge_layer import Knowledge, SourceRoot, canonical
from .memory_policy import read_bounded
from .session_routing import (Placement, Session, Boundary, Costs, RouteOutbox,
                              plan, detect_boundary, save_checkpoint, load_checkpoint)
from .native_capabilities import manifest, HandoffGateway
from .routing_calibration import fresh_verification_profile


def session(data):
    return Session(**(data | {"placement": Placement(**data["placement"])}))


def handle(k, operation, data):
    if not isinstance(data, dict):
        raise ValueError("routing request must be an object")
    box = RouteOutbox(k)
    if operation == "calibrate":
        if set(data) != {"records"}:
            raise ValueError("expected only native observation records")
        return fresh_verification_profile(data["records"])
    if operation == "plan":
        allowed = {"current", "boundary", "message", "sessions", "costs", "desired_placement", "placement_authorised"}
        if not set(data).issubset(allowed) or not {"current", "costs"}.issubset(data) or (("boundary" in data) == ("message" in data)):
            raise ValueError("invalid routing plan fields")
        current = session(data["current"])
        others = [session(s) for s in data.get("sessions", [])]
        boundary = Boundary(**data["boundary"]) if "boundary" in data else detect_boundary(data["message"], current.task_key, {s.task_key for s in others})
        return plan(current, boundary, others, Costs(**data["costs"]),
                    desired_placement=Placement(**data["desired_placement"]) if data.get("desired_placement") else None,
                    placement_authorised=data.get("placement_authorised") is True)
    if operation == "checkpoint":
        if not {"task_key", "source_session", "state"}.issubset(data) or not set(data).issubset({"task_key", "source_session", "state", "source_paths"}):
            raise ValueError("invalid checkpoint fields")
        return save_checkpoint(k, **data)
    if operation == "recall":
        if "receipt" not in data or not set(data).issubset({"receipt", "categories", "max_tokens"}):
            raise ValueError("invalid checkpoint recall fields")
        return load_checkpoint(k, **data)
    if operation == "prepare":
        if set(data) != {"message_id", "message", "route", "current_task", "state", "target_packet"}:
            raise ValueError("invalid handoff fields")
        return box.prepare(**data)
    if operation in {"handoff", "cancel"}:
        if set(data) != {"message_id"}:
            raise ValueError("expected only message identity")
        return box.dispatch(data["message_id"], HandoffGateway()) if operation == "handoff" else box.cancel(data["message_id"])
    raise ValueError("unsupported routing operation")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--synthetic", action="store_true", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--source-root", required=True)
    p.add_argument("--scope", required=True)
    p.add_argument("operation", choices=["init", "capabilities", "calibrate", "plan", "checkpoint", "recall", "prepare", "handoff", "cancel", "export"])
    args = p.parse_args()
    if args.operation == "capabilities":
        print(canonical(manifest()))
        return
    path = Path(args.db).resolve()
    if args.operation != "init" and not path.is_file():
        raise ValueError("initialise the explicit synthetic database first")
    conn = bm.connect(str(path), read_only=args.operation in {"plan", "recall", "export", "calibrate"})
    try:
        if args.operation == "init":
            bm.initialize(conn)
        k = Knowledge(conn, scope=args.scope, sources=SourceRoot(args.source_root), synthetic=True, create=args.operation == "init")
        if args.operation == "init":
            RouteOutbox(k, create=True)
            result = {"initialised": True, "synthetic_only": True, "automatic_dispatch": False}
        elif args.operation == "export":
            result = RouteOutbox(k).export()
        else:
            data = json.loads(read_bounded(sys.stdin.buffer, 8 * 1024 * 1024))
            result = handle(k, args.operation, data)
        print(canonical(result))
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, bm.sqlite3.Error, UnicodeError, RecursionError):
        raise SystemExit("synthetic routing request rejected; no input content is included") from None
