"""Explicit-path synthetic workbench CLI; no default vault or installation."""
import argparse
import json
import sys
from pathlib import Path

from . import memorycore_ai as bm
from .knowledge_layer import Knowledge, SourceRoot, canonical
from .memory_policy import read_bounded
from .memory_mcp_lab import LabServer
from .generation_quota import background_propose
from .chat_memory import ChatMemoryPolicy


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--synthetic", action="store_true", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--source-root", required=True)
    p.add_argument("--scope", required=True)
    p.add_argument("operation", choices=["init", "call", "export", "import", "aliases", "relate", "expire-proposals", "background-propose"])
    p.add_argument("--reviewed", action="store_true")
    p.add_argument("--no-use-memories", action="store_true")
    p.add_argument("--no-generate-memories", action="store_true")
    p.add_argument("--disable-on-external-context", action="store_true")
    p.add_argument("--redact-secrets", action="store_true")
    args = p.parse_args()
    policy = ChatMemoryPolicy(use_memories=not args.no_use_memories,
        generate_memories=not args.no_generate_memories,
        disable_on_external_context=args.disable_on_external_context)
    if args.operation == "export" and not policy.use_memories:
        raise ValueError("memory use disabled")
    if args.operation in {"import", "aliases", "relate"} and not policy.generate_memories:
        raise ValueError("memory generation disabled")
    root = SourceRoot(args.source_root, redact_secrets=args.redact_secrets)
    db = Path(args.db).resolve()
    if args.operation != "init" and not db.is_file():
        raise ValueError("initialise the explicit synthetic database first")
    conn = bm.connect(str(db), read_only=args.operation == "export")
    try:
        if args.operation == "init":
            bm.initialize(conn)
        k = Knowledge(conn, scope=args.scope, sources=root, synthetic=args.synthetic, create=args.operation == "init")
        if args.operation == "init":
            result = {"initialised": True, "synthetic_only": True, "core_schema": 2, "knowledge_format": 1}
        elif args.operation == "export":
            result = k.export()
        elif args.operation == "expire-proposals":
            result = k.expire_proposals()
        else:
            data = json.loads(read_bounded(sys.stdin.buffer, 8 * 1024 * 1024))
            if args.operation == "background-propose":
                if not isinstance(data, dict) or set(data) != {"path", "quota", "chat"}:
                    raise ValueError("expected path, quota and trusted-host chat observation")
                result = background_propose(k, data["path"], data["quota"], chat=data["chat"], policy=policy)
            elif args.operation == "call":
                if not isinstance(data, dict) or set(data) != {"name", "arguments"}:
                    raise ValueError("expected a named tool request")
                server = LabServer(k, chat_policy=policy)
                server.initialized = True  # Local CLI host, not transport authentication.
                result = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": data})
                if "error" in result:
                    raise ValueError("tool request rejected")
            elif args.operation == "import":
                result = k.import_package(data, reviewed=args.reviewed)
            elif args.operation == "aliases":
                result = k.aliases(data, reviewed=args.reviewed)
            else:
                if not isinstance(data, dict) or set(data) != {"owner", "target", "relation", "evidence"}:
                    raise ValueError("invalid relation fields")
                result = k.relate(**data, reviewed=args.reviewed)
        print(canonical(result))
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, bm.sqlite3.Error, UnicodeError, RecursionError):
        raise SystemExit("knowledge request rejected; no input content is included in this error") from None
