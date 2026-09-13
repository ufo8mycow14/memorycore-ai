"""Install a local monitored MemoryCore AI MCP entry for this Windows user.

This creates a dedicated synthetic vault and appends one MCP server block to the
user Codex config. It does not ingest existing chats or alter native memory.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOME = Path.home() / ".codex" / "memorycore-ai-local"
PYTHON = Path(sys.executable)
BINARY = ROOT / "rust-broker" / "target" / "release" / "memorycore-ai-broker.exe"


def run(command, **kwargs):
    result = subprocess.run(command, text=True, capture_output=True, **kwargs)
    if result.returncode:
        raise SystemExit(json.dumps({
            "failed": command,
            "returncode": result.returncode,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-4000:],
        }, indent=2))
    return result


def mcp_entry(python, host_config, cache, metrics):
    return {
        "command": str(python),
        "args": ['-B', '-m', 'scripts.monitored_native_mcp', 'serve', '--binary', str(BINARY),
                 '--config', str(host_config), '--cache', str(cache), '--session',
                 'memorycore-ai-local', '--metrics', str(metrics)],
        "cwd": str(ROOT), "startup_timeout_sec": 120.0, "tool_timeout_sec": 60.0,
    }


def append_mcp_config(config_path, name, python, host_config, cache, metrics, *, dry_run=False):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("Server name must contain only letters, digits, underscores or hyphens")
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    servers = tomllib.loads(text).get("mcp_servers", {})
    entry = mcp_entry(python, host_config, cache, metrics)
    header = f"[mcp_servers.{name}]"
    if name in servers:
        if servers[name].get("enabled") is False:
            raise ValueError("Existing MCP registration is disabled; configuration was not changed")
        if any(servers[name].get(key) != value for key, value in entry.items()):
            raise ValueError("Existing MCP registration differs; configuration was not changed")
        return {"changed": False, "reason": "already_present"}
    block = "\n\n# MemoryCore AI synthetic local provider; no chat ingestion.\n" + header + "\n"
    block += "\n".join(f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in entry.items())
    candidate = text.rstrip() + block + "\n"
    tomllib.loads(candidate)
    if dry_run:
        return {"changed": False, "would_add": True, "entry": entry}
    backup = config_path.with_name(config_path.name + time.strftime(".memorycore-ai-%Y%m%d-%H%M%S.bak"))
    if config_path.exists():
        shutil.copyfile(config_path, backup)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(candidate, encoding="utf-8")
    return {"changed": True, "backup": str(backup)}


def verify_mcp(python, host_config, cache, metrics):
    entry = mcp_entry(python, host_config, cache, metrics)
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "memorycore-setup-check", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
    ]
    result = run([str(python), *entry["args"]], cwd=ROOT,
                 input="".join(json.dumps(row) + "\n" for row in requests), timeout=120)
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    if (len(rows) != 3 or [row.get("id") for row in rows] != [1, 2, 3]
            or any("error" in row for row in rows)
            or rows[0].get("result", {}).get("protocolVersion") != "2025-06-18"
            or [tool.get("name") for tool in rows[1].get("result", {}).get("tools", [])] != ["memory"]
            or rows[2].get("result") != {}):
        raise ValueError("MCP startup verification failed; client configuration was not changed")
    return {"initialize": True, "memory_tool": True, "ping": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    parser.add_argument("--codex-config", type=Path, default=Path.home() / ".codex" / "config.toml")
    parser.add_argument("--server-name", default="memorycore_ai_monitored")
    parser.add_argument("--skip-deps", action="store_true")
    parser.add_argument("--skip-models", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Preview registration without creating files or running installers")
    args = parser.parse_args()
    home = args.home.resolve()
    args.codex_config = args.codex_config.resolve()
    vault = home / "vault"
    sources = home / "sources"
    cache = home / "model-cache"
    logs = home / "logs"
    host_config = home / "host.json"
    metrics = logs / "metrics.jsonl"
    venv_python = home / "venv" / "Scripts" / "python.exe"
    preview = append_mcp_config(args.codex_config, args.server_name, venv_python,
                                host_config, cache, metrics, dry_run=True)
    if args.dry_run:
        print(json.dumps({"dry_run": True, "home": str(home), "mcp_config": preview,
                          "private_chat_ingestion": False}, indent=2))
        return
    if host_config.exists():
        config = json.loads(host_config.read_text(encoding="utf-8"))
        if config.get("synthetic") is not True or config.get("backend") != "native":
            raise ValueError("Explicit synthetic native configuration required")
    for directory in (vault, sources, cache, logs):
        directory.mkdir(parents=True, exist_ok=True)
    if not host_config.exists():
        config = {
            "synthetic": True,
            "backend": "native",
            "database": str(vault / "memorycore-ai.sqlite3"),
            "allow_plaintext": True,
            "read_workers": 4,
            "sessions": [{
                "id": "memorycore-ai-local",
                "scope": "local:memorycore-ai",
                "source_root": str(sources),
                "allow_admin": True,
                "use_memories": True,
                "generate_memories": True,
                "redact_secrets": True,
                "disable_on_external_context": True,
            }],
        }
        host_config.write_text(json.dumps(config, indent=2), encoding="utf-8")
    if not BINARY.exists():
        run(["cargo", "build", "--locked", "--release"], cwd=ROOT / "rust-broker")
    if not venv_python.exists():
        run([str(PYTHON), "-m", "venv", str(home / "venv")])
    if not args.skip_deps:
        run([str(venv_python), "-m", "pip", "install", "--upgrade", "pip"])
        run([str(venv_python), "-m", "pip", "install", "-r", str(ROOT / "scripts" / "vector-requirements.txt")])
        run([str(venv_python), "-m", "pip", "install", "-r", str(ROOT / "scripts" / "model-build-requirements.txt")])
        run([str(venv_python), "-m", "pip", "install", "-r", str(ROOT / "requirements-tokenizer.txt")])
        run([str(venv_python), "-m", "pip", "install", "-r", str(ROOT / "requirements-security-lab.txt")])
    if not args.skip_models:
        run([str(venv_python), "-B", "-m", "scripts.vector_pipeline", "download-model", "--cache", str(cache)], cwd=ROOT)
        run([str(venv_python), "-B", "-m", "scripts.vector_pipeline", "download-reranker", "--cache", str(cache)], cwd=ROOT)
    config = json.loads(host_config.read_text(encoding="utf-8"))
    if config.get("synthetic") is not True or config.get("backend") != "native":
        raise ValueError("Explicit synthetic native configuration required")
    database = Path(config["database"])
    if not database.exists():
        run([str(BINARY), "--init", "--config", str(host_config)])
    verification = verify_mcp(venv_python, host_config, cache, metrics)
    mcp = append_mcp_config(args.codex_config, args.server_name, venv_python, host_config, cache, metrics)
    print(json.dumps({
        "installed": True,
        "home": str(home),
        "host_config": str(host_config),
        "database": str(database),
        "source_root": str(sources),
        "metrics": str(metrics),
        "mcp_config": mcp,
        "server_name": args.server_name,
        "verification": verification,
        "restart_required": "Restart Codex or start a new task for the new MCP server to be discovered.",
        "private_chat_ingestion": False,
    }, indent=2))


if __name__ == "__main__":
    main()
