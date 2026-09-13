"""Private persistent compatibility worker for the Rust synthetic broker.

Only a trusted launcher may supply the configuration and transport. No listener,
credentials, automatic native chat capture or production identity is provided.
"""
import argparse
import json
import sys
from pathlib import Path

from . import memorycore_ai as bm
from .chat_memory import ChatMemoryPolicy
from .generation_quota import background_propose
from .knowledge_layer import Knowledge, SourceRoot, canonical
from .memory_mcp_lab import LabServer, COMPACT_TOOL
from .routing_cli import handle as routing_handle
from .session_routing import RouteOutbox


def object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def loads(raw):
    return json.loads(raw, object_pairs_hook=object_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid number")))


class Backend:
    def __init__(self, config, role):
        if config.get("synthetic") is not True or role not in {"read", "write"}:
            raise ValueError("synthetic host configuration required")
        self.role = role
        path = Path(config["database"]).resolve(strict=True)
        self.conn = bm.connect(str(path), read_only=role == "read")
        self.sessions = {}
        try:
            for s in config["sessions"]:
                if s["id"] in self.sessions:
                    raise ValueError("duplicate host session")
                policy = ChatMemoryPolicy(use_memories=s.get("use_memories", True),
                    generate_memories=s.get("generate_memories", True),
                    disable_on_external_context=s.get("disable_on_external_context", False))
                k = Knowledge(self.conn, scope=s["scope"], synthetic=True,
                    sources=SourceRoot(s["source_root"], redact_secrets=s.get("redact_secrets", False)))
                server = LabServer(k, chat_policy=policy)
                server.initialized = True
                self.sessions[s["id"]] = k, server, policy
        except BaseException:
            self.conn.close()
            raise

    def execute(self, request):
        if not isinstance(request, dict) or set(request) != {"session", "id", "operation", "arguments"}:
            raise ValueError("invalid worker envelope")
        k, server, policy = self.sessions[request["session"]]
        op, args = request["operation"], request["arguments"]
        if not isinstance(args, dict):
            raise ValueError("invalid arguments")
        if op == "ping":
            if args:
                raise ValueError("ping has no arguments")
            return {"alive": True, "backend": "python-compatibility"}
        if op == "catalogue":
            if args:
                raise ValueError("catalogue has no arguments")
            return {"tools": [COMPACT_TOOL]}
        if op == "call":
            return server.handle({"jsonrpc": "2.0", "id": request["id"], "method": "tools/call", "params": args})
        if op == "background-propose":
            if self.role != "write" or set(args) != {"path", "quota", "chat"}:
                raise ValueError("invalid background request")
            return background_propose(k, args["path"], args["quota"], chat=args["chat"], policy=policy)
        if op.startswith("routing-"):
            action = op.removeprefix("routing-")
            read = action in {"plan", "recall", "export", "calibrate"}
            if (read and not policy.use_memories) or (not read and not policy.generate_memories):
                raise ValueError("chat policy prohibits operation")
            if not read and self.role != "write":
                raise ValueError("read worker cannot mutate")
            if action == "export":
                if args:
                    raise ValueError("export has no arguments")
                # Large exports remain available through the existing CLI;
                # the broker must not transport unbounded responses.
                return RouteOutbox(k).export()
            return routing_handle(k, action, args)
        raise ValueError("unknown worker operation")

    def respond(self, request):
        # The full transport bound is checked before a mutation commits.
        with bm.transaction(self.conn, write=self.role == "write"):
            result = self.execute(request)
            response = {"session": request["session"], "id": request["id"], "result": result}
            encoded = canonical(response)
            if len(encoded.encode()) >= 1024 * 1024:
                raise ValueError("response too large")
        return encoded


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--role", required=True, choices=["read", "write"])
    args = p.parse_args()
    raw = sys.stdin.buffer.readline(65537)
    if len(raw) > 65536 or not raw.endswith(b"\n"):
        raise ValueError("host configuration too large")
    backend = Backend(loads(raw), args.role)
    try:
        print('{"ready":true}', flush=True)
        while True:
            raw = sys.stdin.buffer.readline(65537)
            if not raw:
                break
            if len(raw) > 65536 or not raw.endswith(b"\n"):
                break
            request = None
            try:
                request = loads(raw)
                encoded = backend.respond(request)
            except (ValueError, KeyError, TypeError, OSError, bm.sqlite3.Error, SystemExit, UnicodeError, RecursionError):
                response = {"session": request.get("session") if isinstance(request, dict) else None,
                            "id": request.get("id") if isinstance(request, dict) else None,
                            "error": "request_rejected"}
                encoded = canonical(response)
            print(encoded, flush=True)
    finally:
        backend.conn.close()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, bm.sqlite3.Error, SystemExit, UnicodeError):
        raise SystemExit("compatibility worker stopped; no input content is included") from None
