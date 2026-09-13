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


def append_mcp_config(config_path, name, python, host_config, cache, metrics):
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    header = f"[mcp_servers.{name}]"
    if header in text:
        return {"changed": False, "reason": "already_present"}
    backup = config_path.with_name(config_path.name + time.strftime(".memorycore-ai-%Y%m%d-%H%M%S.bak"))
    if config_path.exists():
        shutil.copyfile(config_path, backup)
    block = f"""

# MemoryCore AI local monitored rollout. Synthetic/local provider; no chat ingestion.
{header}
command = '{str(python)}'
args = ['-B', '-m', 'scripts.monitored_native_mcp', 'serve', '--binary', '{str(BINARY)}', '--config', '{str(host_config)}', '--cache', '{str(cache)}', '--session', 'memorycore-ai-local', '--metrics', '{str(metrics)}']
cwd = '{str(ROOT)}'
startup_timeout_sec = 120.0
tool_timeout_sec = 60.0
"""
    config_path.write_text(text.rstrip() + block + "\n", encoding="utf-8")
    return {"changed": True, "backup": str(backup)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    parser.add_argument("--codex-config", type=Path, default=Path.home() / ".codex" / "config.toml")
    parser.add_argument("--server-name", default="memorycore_ai_monitored")
    parser.add_argument("--skip-deps", action="store_true")
    parser.add_argument("--skip-models", action="store_true")
    args = parser.parse_args()
    home = args.home
    vault = home / "vault"
    sources = home / "sources"
    cache = home / "model-cache"
    logs = home / "logs"
    for directory in (vault, sources, cache, logs):
        directory.mkdir(parents=True, exist_ok=True)
    host_config = home / "host.json"
    metrics = logs / "metrics.jsonl"
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
    else:
        config = json.loads(host_config.read_text(encoding="utf-8"))
        changed = False
        for session in config.get("sessions", []):
            if isinstance(session, dict) and "archive_delete_after_days" in session:
                session.pop("archive_delete_after_days")
                changed = True
        if changed:
            host_config.write_text(json.dumps(config, indent=2), encoding="utf-8")
    if not BINARY.exists():
        run(["cargo", "build", "--locked", "--release"], cwd=ROOT / "rust-broker")
    venv_python = home / "venv" / "Scripts" / "python.exe"
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
    database = vault / "memorycore-ai.sqlite3"
    if not database.exists():
        run([str(BINARY), "--init", "--config", str(host_config)])
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
        "restart_required": "Restart Codex or start a new task for the new MCP server to be discovered.",
        "private_chat_ingestion": False,
    }, indent=2))


if __name__ == "__main__":
    main()
