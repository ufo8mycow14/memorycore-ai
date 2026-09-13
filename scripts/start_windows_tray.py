"""Launch the MemoryCore AI Windows tray with the standard local profile."""
import argparse
import json
from pathlib import Path
import sys

from scripts import windows_tray


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOME = Path.home() / ".codex" / "memorycore-ai-local"
DEFAULT_SESSION = "memorycore-ai-local"
BROKER = ROOT / "rust-broker" / "target" / "release" / "memorycore-ai-broker.exe"


def host_database(host_config):
    if host_config.exists():
        data = json.loads(host_config.read_text(encoding="utf-8"))
        database = data.get("database")
        if database:
            return Path(database)
    return host_config.parent / "vault" / "memorycore-ai.sqlite3"


def default_python(home):
    venv_python = home / "venv" / "Scripts" / "python.exe"
    return venv_python if venv_python.exists() else Path(sys.executable)


def tray_argv(args):
    home = args.home.resolve()
    logs = home / "logs"
    host_config = args.config or (home / "host.json")
    cache = args.cache or (home / "model-cache")
    metrics = args.metrics or (logs / "metrics.jsonl")
    preferences = args.preferences or (home / "tray-preferences.json")
    status_file = args.status_file or (logs / "tray-status.json")
    usage_report = args.usage_report or (logs / "lifecycle-usage.json")
    vault_dir = args.vault_dir or (home / "vault")
    python = args.python or default_python(home)
    broker = args.binary or BROKER
    command = [
        str(python),
        "-B",
        "-m",
        "scripts.monitored_native_mcp",
        "serve",
        "--binary",
        str(broker),
        "--config",
        str(host_config),
        "--cache",
        str(cache),
        "--session",
        args.session,
        "--metrics",
        str(metrics),
    ]
    tray = [
        "--cwd",
        str(ROOT),
        "--metrics",
        str(metrics),
        "--database",
        str(args.database or host_database(host_config)),
        "--preferences",
        str(preferences),
        "--usage-report",
        str(usage_report),
        "--status-file",
        str(status_file),
        "--vault-dir",
        str(vault_dir),
    ]
    if args.keep_after_exit:
        tray.append("--keep-after-exit")
    if args.status_json:
        tray.append("--status-json")
    if args.icon:
        tray.extend(["--icon", str(args.icon)])
    tray.extend(["--", *command])
    return tray


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME, help="Local MemoryCore AI profile folder.")
    parser.add_argument("--session", default=DEFAULT_SESSION, help="MemoryCore AI session id.")
    parser.add_argument("--python", type=Path, help="Python executable for the monitored MCP process.")
    parser.add_argument("--binary", type=Path, help="MemoryCore AI broker executable.")
    parser.add_argument("--config", type=Path, help="Host JSON config.")
    parser.add_argument("--cache", type=Path, help="Model cache folder.")
    parser.add_argument("--metrics", type=Path, help="Payload-free metrics JSONL.")
    parser.add_argument("--database", type=Path, help="Database path for size reporting.")
    parser.add_argument("--preferences", type=Path, help="Tray preferences JSON.")
    parser.add_argument("--usage-report", type=Path, help="Verified lifecycle usage report.")
    parser.add_argument("--status-file", type=Path, help="Tray status JSON output.")
    parser.add_argument("--vault-dir", type=Path, help="Vault folder to open from the tray.")
    parser.add_argument("--icon", type=Path, help="Override tray icon.")
    parser.add_argument("--keep-after-exit", action="store_true", help="Keep tray visible if the child exits.")
    parser.add_argument("--status-json", action="store_true", help="Print one status snapshot and exit.")
    parser.add_argument("--print-command", action="store_true", help="Print the generated tray argv and exit.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    generated = tray_argv(args)
    if args.print_command:
        print(json.dumps(generated, indent=2))
        return
    windows_tray.main(generated)


if __name__ == "__main__":
    main()
