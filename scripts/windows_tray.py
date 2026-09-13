"""Windows system tray companion for a local MemoryCore AI process.

The tray process can either launch MemoryCore AI from a command after ``--`` or
attach to an existing PID. It keeps no memory payloads and only reports local
process state plus optional payload-free metrics summaries.
"""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time


APP_NAME = "MemoryCore AI"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ICON = ROOT / "docs" / "assets" / "memorycore-ai-icon.ico"
PREFERENCES_SCHEMA = "memorycore-ai-tray-preferences/v1"
DEFAULT_PREFERENCES = {
    "schema": PREFERENCES_SCHEMA,
    "min_idle_seconds": 24 * 60 * 60,
    "auto_archive_after_seconds": None,
    "auto_delete_after_days": 365,
    "read_only_mode": False,
}
WM_USER = 0x0400
WM_TRAYICON = WM_USER + 20
WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
IDI_APPLICATION = 32512
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
TPM_RIGHTBUTTON = 0x0002
MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
CMD_STATUS = 1001
CMD_METRICS = 1002
CMD_FOLDER = 1003
CMD_RESTART = 1004
CMD_EXIT = 1005
CMD_PREFERENCES = 1006
CMD_USAGE = 1007
CMD_VAULT = 1008


def now():
    return time.time()


def duration(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def file_size(path):
    if path is None:
        return {"configured": False}
    path = Path(path)
    if not path.exists():
        return {"configured": True, "path": str(path), "exists": False, "bytes": 0}
    return {"configured": True, "path": str(path), "exists": True, "bytes": path.stat().st_size}


def default_icon_path():
    return DEFAULT_ICON if DEFAULT_ICON.exists() else None


def resolve_icon_path(path):
    if path is not None:
        return Path(path)
    return default_icon_path()


def validate_preferences(preferences):
    if preferences.get("schema") != PREFERENCES_SCHEMA:
        raise ValueError("Unsupported tray preferences schema")
    idle = preferences.get("min_idle_seconds")
    archive = preferences.get("auto_archive_after_seconds")
    delete = preferences.get("auto_delete_after_days")
    if type(idle) is not int or not 3600 <= idle <= 365 * 24 * 60 * 60:
        raise ValueError("Idle generation interval must be between one hour and 365 days")
    if archive is not None and (type(archive) is not int or not 3600 <= archive <= 365 * 24 * 60 * 60):
        raise ValueError("Auto-archive interval must be null or between one hour and 365 days")
    if delete is not None and (type(delete) is not int or not 1 <= delete <= 36500):
        raise ValueError("Auto-delete days must be null or between 1 and 36500")
    if type(preferences.get("read_only_mode")) is not bool:
        raise ValueError("Read-only mode must be boolean")


def load_preferences(path):
    if path is None:
        return dict(DEFAULT_PREFERENCES)
    path = Path(path)
    if not path.exists():
        return dict(DEFAULT_PREFERENCES)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Unsupported tray preferences file")
    preferences = dict(DEFAULT_PREFERENCES)
    preferences.update(data)
    validate_preferences(preferences)
    return preferences


def save_preferences(path, preferences):
    path = Path(path)
    validate_preferences(preferences)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(preferences, indent=2, sort_keys=True), encoding="utf-8")
    return path


def update_preferences(path, **changes):
    preferences = load_preferences(path)
    preferences.update({key: value for key, value in changes.items() if value is not None})
    save_preferences(path, preferences)
    return preferences


class MemoryCoreProcess:
    def __init__(self, command=None, pid=None, cwd=None):
        if bool(command) == bool(pid):
            raise ValueError("Provide either a launch command or an existing PID")
        self.command = list(command or [])
        self.pid = pid
        self.cwd = Path(cwd).resolve() if cwd else None
        self.process = None
        self.started_at = None

    def start(self):
        if self.pid:
            self.started_at = now()
            return
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(
            self.command,
            cwd=str(self.cwd) if self.cwd else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        self.pid = self.process.pid
        self.started_at = now()

    def running(self):
        if self.process is not None:
            return self.process.poll() is None
        if self.pid is None:
            return False
        if os.name != "nt":
            return False
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(self.pid))
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    def stop(self):
        if self.process is not None and self.running():
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def restart(self):
        if not self.command:
            raise ValueError("Cannot restart when attached to an existing PID")
        self.stop()
        self.pid = None
        self.process = None
        self.start()

    def status(self):
        running = self.running()
        return {
            "name": APP_NAME,
            "pid": self.pid,
            "running": running,
            "uptime_seconds": int(now() - self.started_at) if running and self.started_at else 0,
            "mode": "attached" if self.command == [] else "launched",
        }


def metrics_summary(path):
    if path is None:
        return {"configured": False}
    path = Path(path)
    if not path.exists():
        return {"configured": True, "path": str(path), "exists": False, "rows": 0}
    try:
        from scripts.monitored_native_mcp import summarise
    except ModuleNotFoundError:
        if __package__ in {None, ""}:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from scripts.monitored_native_mcp import summarise
        else:
            raise
    summary = summarise(path)
    return {"configured": True, "path": str(path), "exists": True, **summary}


def usage_summary(path):
    if path is None:
        return {"configured": False}
    path = Path(path)
    if not path.exists():
        return {"configured": True, "path": str(path), "exists": False}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Unsupported usage report")
    baseline = data.get("baseline", {}).get("observed_tokens")
    repaired = data.get("repaired", {}).get("observed_tokens")
    codex_latest = data.get("codex_tokens", {}).get("latest_request", {})
    sentinel = data.get("context_budget_sentinel", {})
    memory_operations = data.get("memory_operations", {})
    projection = data.get("model_cost_projection", {})
    return {
        "configured": True,
        "path": str(path),
        "exists": True,
        "comparable": data.get("comparable"),
        "model_profile": data.get("model_profile"),
        "quality_regressions": data.get("quality_regressions"),
        "tokens_used": repaired if isinstance(repaired, dict) else None,
        "baseline_tokens": baseline if isinstance(baseline, dict) else None,
        "total_token_saving": data.get("total_token_saving"),
        "uncached_input_saving": data.get("uncached_input_saving"),
        "cost_saving": data.get("cost_saving"),
        "codex_tokens": data.get("codex_tokens") if isinstance(data.get("codex_tokens"), dict) else None,
        "provider_prompt_cache": provider_prompt_cache_summary(codex_latest, repaired),
        "context_reduction": {
            "estimated_memorycore_packet_tokens": sentinel.get("estimated_memorycore_packet_tokens"),
            "estimated_fresh_task_input_saving_percent": sentinel.get("estimated_fresh_task_input_saving_percent"),
            "topic_drift": sentinel.get("topic_drift"),
            "recommendation": sentinel.get("recommendation"),
        } if isinstance(sentinel, dict) else None,
        "memorycore_packet_cache": memory_operations.get("packet_cache_effect") if isinstance(memory_operations, dict) else None,
        "best_projected_cost_saving": best_projected_cost_saving(projection),
    }


def provider_prompt_cache_summary(codex_latest, repaired_tokens=None):
    source = codex_latest if isinstance(codex_latest, dict) and codex_latest else {}
    if not source and isinstance(repaired_tokens, dict):
        source = {
            "input_tokens": repaired_tokens.get("input"),
            "cached_input_tokens": repaired_tokens.get("cached_input"),
            "cache_write_input_tokens": repaired_tokens.get("cache_write_input"),
            "uncached_input_tokens": repaired_tokens.get("uncached_input"),
            "output_tokens": repaired_tokens.get("output"),
            "reasoning_output_tokens": repaired_tokens.get("reasoning"),
            "total_tokens": repaired_tokens.get("total"),
        }
    if not source:
        return None
    input_tokens = source.get("input_tokens")
    cached = source.get("cached_input_tokens")
    uncached = source.get("uncached_input_tokens")
    if uncached is None and isinstance(input_tokens, int) and isinstance(cached, int):
        uncached = input_tokens - cached
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": source.get("cache_write_input_tokens"),
        "uncached_input_tokens": uncached,
        "cached_input_percent": source.get("cached_input_percent") or percent(cached, input_tokens),
        "uncached_input_percent": source.get("uncached_input_percent") or percent(uncached, input_tokens),
        "output_tokens": source.get("output_tokens"),
        "reasoning_output_tokens": source.get("reasoning_output_tokens"),
        "total_tokens": source.get("total_tokens"),
    }


def percent(part, whole):
    if not isinstance(part, (int, float)) or not isinstance(whole, (int, float)) or whole <= 0:
        return None
    return round((part / whole) * 100, 2)


def best_projected_cost_saving(projection):
    if not isinstance(projection, dict):
        return None
    rows = projection.get("rows", [])
    if not isinstance(rows, list):
        return None
    candidates = [row for row in rows if isinstance(row, dict)
                  and isinstance(row.get("estimated_cost_saving_percent"), (int, float))]
    if not candidates:
        return None
    return max(candidates, key=lambda row: row["estimated_cost_saving_percent"])


def packet_cache_summary(metrics, usage):
    usage_cache = usage.get("memorycore_packet_cache") if isinstance(usage, dict) else None
    if isinstance(usage_cache, dict):
        return usage_cache
    metrics_cache = metrics.get("packet_cache_effect") if isinstance(metrics, dict) else None
    if isinstance(metrics_cache, dict):
        return metrics_cache
    return None


def cache_summary(metrics, usage):
    provider = usage.get("provider_prompt_cache") if isinstance(usage, dict) else None
    packet = packet_cache_summary(metrics, usage)
    context = usage.get("context_reduction") if isinstance(usage, dict) else None
    projected = usage.get("best_projected_cost_saving") if isinstance(usage, dict) else None
    configured = any(isinstance(value, dict) for value in (provider, packet, context, projected))
    return {
        "configured": configured,
        "provider_prompt_cache": provider if isinstance(provider, dict) else None,
        "memorycore_packet_cache": packet if isinstance(packet, dict) else None,
        "context_reduction": context if isinstance(context, dict) else None,
        "best_projected_cost_saving": projected if isinstance(projected, dict) else None,
    }


def status_labels(snapshot):
    preferences = snapshot.get("preferences", {})
    database = snapshot.get("database", {})
    usage = snapshot.get("usage", {})
    cache = snapshot.get("cache", {})
    provider_cache = (cache.get("provider_prompt_cache") or {}) if isinstance(cache, dict) else {}
    packet_cache = (cache.get("memorycore_packet_cache") or {}) if isinstance(cache, dict) else {}
    context_reduction = (cache.get("context_reduction") or {}) if isinstance(cache, dict) else {}
    projected = (cache.get("best_projected_cost_saving") or {}) if isinstance(cache, dict) else {}
    labels = {
        "health": "running" if snapshot.get("running") else "stopped",
        "mode": snapshot.get("mode"),
        "pid": snapshot.get("pid"),
        "uptime": snapshot.get("uptime"),
        "database": "not configured",
        "read_only": bool(preferences.get("read_only_mode")),
        "idle_generation_hours": None,
        "auto_archive_hours": None,
        "auto_delete_days": preferences.get("auto_delete_after_days"),
        "token_saving_percent": None,
        "provider_cached_input_percent": provider_cache.get("cached_input_percent"),
        "provider_uncached_input_percent": provider_cache.get("uncached_input_percent"),
        "provider_cache_write_input_tokens": provider_cache.get("cache_write_input_tokens"),
        "memorycore_packet_cache_hit_rate_percent": packet_cache.get("hit_rate_percent"),
        "memorycore_packet_cache_hits": packet_cache.get("hits"),
        "memorycore_packet_cache_misses": packet_cache.get("misses"),
        "memorycore_full_retrievals_avoided": packet_cache.get("estimated_full_retrievals_avoided"),
        "estimated_fresh_task_input_saving_percent": context_reduction.get("estimated_fresh_task_input_saving_percent"),
        "best_projected_cost_saving_percent": projected.get("estimated_cost_saving_percent"),
    }
    if database.get("configured"):
        labels["database"] = "present" if database.get("exists") else "missing"
        labels["database_bytes"] = database.get("bytes", 0)
    idle = preferences.get("min_idle_seconds")
    if isinstance(idle, int):
        labels["idle_generation_hours"] = round(idle / 3600, 2)
    archive = preferences.get("auto_archive_after_seconds")
    if isinstance(archive, int):
        labels["auto_archive_hours"] = round(archive / 3600, 2)
    saving = usage.get("total_token_saving")
    if isinstance(saving, (int, float)):
        labels["token_saving_percent"] = round(saving * 100, 2)
    return labels


def status_snapshot(process, metrics=None, *, database=None, preferences=None, usage_report=None):
    snapshot = process.status()
    snapshot["uptime"] = duration(snapshot["uptime_seconds"])
    metrics_data = metrics_summary(metrics)
    usage_data = usage_summary(usage_report)
    snapshot["metrics"] = metrics_data
    snapshot["database"] = file_size(database)
    snapshot["preferences"] = load_preferences(preferences)
    snapshot["usage"] = usage_data
    snapshot["cache"] = cache_summary(metrics_data, usage_data)
    snapshot["labels"] = status_labels(snapshot)
    return snapshot


def tooltip(snapshot):
    state = "running" if snapshot["running"] else "stopped"
    return f"MemoryCore AI {state}; PID {snapshot['pid'] or 'n/a'}; uptime {snapshot['uptime']}"


def write_status_file(path, snapshot):
    if path is None:
        return None
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    return path


def open_path(path):
    if path is None:
        return
    if os.name == "nt":
        os.startfile(path)
        return
    raise RuntimeError("Opening files from the tray is only supported on Windows")


if os.name == "nt":
    WPARAM = ctypes.c_size_t
    LPARAM = ctypes.c_ssize_t
    LRESULT = ctypes.c_ssize_t
    HICON = wintypes.HANDLE
    HCURSOR = wintypes.HANDLE
    HBRUSH = wintypes.HANDLE
    HINSTANCE = wintypes.HANDLE

    class NOTIFYICONDATA(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", HICON),
            ("szTip", wintypes.WCHAR * 128),
        ]

    WNDPROCTYPE = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)

    class WNDCLASS(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROCTYPE),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", HINSTANCE),
            ("hIcon", HICON),
            ("hCursor", HCURSOR),
            ("hbrBackground", HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]


class TrayApp:
    def __init__(
        self,
        process,
        *,
        metrics=None,
        status_file=None,
        project_dir=None,
        keep_after_exit=False,
        database=None,
        preferences=None,
        usage_report=None,
        icon=None,
        vault_dir=None,
    ):
        if os.name != "nt":
            raise RuntimeError("The MemoryCore AI tray companion requires Windows")
        self.process = process
        self.metrics = Path(metrics) if metrics else None
        self.database = Path(database) if database else None
        self.preferences = Path(preferences) if preferences else None
        self.usage_report = Path(usage_report) if usage_report else None
        self.icon = resolve_icon_path(icon)
        self.status_file = Path(status_file) if status_file else (Path(project_dir or Path.cwd()) / "outputs" / "memorycore-ai-tray-status.json")
        self.project_dir = Path(project_dir).resolve() if project_dir else Path.cwd()
        self.vault_dir = Path(vault_dir).resolve() if vault_dir else None
        self.keep_after_exit = keep_after_exit
        self.class_name = "MemoryCoreAITrayWindow"
        self.hwnd = None
        self.menu = None
        self._proc = WNDPROCTYPE(self._wndproc)

    def run(self):
        self.process.start()
        self._create_window()
        self._add_icon()
        ctypes.windll.user32.SetTimer(self.hwnd, 1, 3000, None)
        msg = wintypes.MSG()
        while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
            ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))
        self._remove_icon()
        self.process.stop()

    def _create_window(self):
        hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
        cls = WNDCLASS()
        cls.lpfnWndProc = self._proc
        cls.hInstance = hinst
        cls.lpszClassName = self.class_name
        if not ctypes.windll.user32.RegisterClassW(ctypes.byref(cls)):
            raise ctypes.WinError()
        self.hwnd = ctypes.windll.user32.CreateWindowExW(
            0, self.class_name, APP_NAME, 0, 0, 0, 0, 0, None, None, hinst, None
        )
        if not self.hwnd:
            raise ctypes.WinError()

    def _notify_data(self, tip):
        icon = None
        if self.icon and self.icon.exists():
            icon = ctypes.windll.user32.LoadImageW(
                None,
                str(self.icon),
                IMAGE_ICON,
                0,
                0,
                LR_LOADFROMFILE,
            )
        if not icon:
            icon = ctypes.windll.user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
        data = NOTIFYICONDATA()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        data.hWnd = self.hwnd
        data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = WM_TRAYICON
        data.hIcon = icon
        data.szTip = tip[:127]
        return data

    def _add_icon(self):
        data = self._notify_data(
            tooltip(
                status_snapshot(
                    self.process,
                    self.metrics,
                    database=self.database,
                    preferences=self.preferences,
                    usage_report=self.usage_report,
                )
            )
        )
        if not ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
            raise ctypes.WinError()

    def _update_icon(self):
        snapshot = status_snapshot(
            self.process,
            self.metrics,
            database=self.database,
            preferences=self.preferences,
            usage_report=self.usage_report,
        )
        write_status_file(self.status_file, snapshot)
        data = self._notify_data(tooltip(snapshot))
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))
        if not snapshot["running"] and not self.keep_after_exit:
            ctypes.windll.user32.PostQuitMessage(0)

    def _remove_icon(self):
        if self.hwnd:
            data = self._notify_data("")
            ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))

    def _show_menu(self):
        self.menu = ctypes.windll.user32.CreatePopupMenu()
        ctypes.windll.user32.AppendMenuW(self.menu, MF_STRING, CMD_STATUS, "Write status snapshot")
        ctypes.windll.user32.AppendMenuW(self.menu, MF_STRING, CMD_METRICS, "Open metrics summary")
        ctypes.windll.user32.AppendMenuW(self.menu, MF_STRING, CMD_PREFERENCES, "Open tray preferences")
        ctypes.windll.user32.AppendMenuW(self.menu, MF_STRING, CMD_USAGE, "Open usage report")
        ctypes.windll.user32.AppendMenuW(self.menu, MF_STRING, CMD_FOLDER, "Open project folder")
        if self.vault_dir:
            ctypes.windll.user32.AppendMenuW(self.menu, MF_STRING, CMD_VAULT, "Open vault folder")
        ctypes.windll.user32.AppendMenuW(self.menu, MF_SEPARATOR, 0, None)
        if self.process.command:
            ctypes.windll.user32.AppendMenuW(self.menu, MF_STRING, CMD_RESTART, "Restart MemoryCore AI")
        ctypes.windll.user32.AppendMenuW(self.menu, MF_STRING, CMD_EXIT, "Exit")
        point = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
        ctypes.windll.user32.SetForegroundWindow(self.hwnd)
        ctypes.windll.user32.TrackPopupMenu(
            self.menu, TPM_RIGHTBUTTON, point.x, point.y, 0, self.hwnd, None
        )
        ctypes.windll.user32.DestroyMenu(self.menu)

    def _open_metrics_summary(self):
        if self.metrics is None:
            return
        snapshot = metrics_summary(self.metrics)
        target = self.metrics.with_suffix(".summary.json")
        target.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
        open_path(target)

    def _write_status_snapshot(self):
        target = write_status_file(
            self.status_file,
            status_snapshot(
                self.process,
                self.metrics,
                database=self.database,
                preferences=self.preferences,
                usage_report=self.usage_report,
            ),
        )
        if target:
            open_path(target)

    def _command(self, ident):
        if ident == CMD_STATUS:
            self._write_status_snapshot()
        elif ident == CMD_METRICS:
            self._open_metrics_summary()
        elif ident == CMD_FOLDER:
            open_path(self.project_dir)
        elif ident == CMD_RESTART:
            self.process.restart()
            self._update_icon()
        elif ident == CMD_PREFERENCES:
            if self.preferences:
                save_preferences(self.preferences, load_preferences(self.preferences))
                open_path(self.preferences)
        elif ident == CMD_USAGE:
            if self.usage_report and self.usage_report.exists():
                open_path(self.usage_report)
        elif ident == CMD_VAULT:
            if self.vault_dir:
                open_path(self.vault_dir)
        elif ident == CMD_EXIT:
            ctypes.windll.user32.PostQuitMessage(0)

    def _wndproc(self, hwnd, message, wparam, lparam):
        if message == WM_COMMAND:
            self._command(int(wparam) & 0xFFFF)
            return 0
        if message == WM_TRAYICON and lparam in {WM_RBUTTONUP, WM_LBUTTONDBLCLK}:
            self._show_menu()
            return 0
        if message == 0x0113:
            self._update_icon()
            return 0
        if message == WM_DESTROY:
            ctypes.windll.user32.PostQuitMessage(0)
            return 0
        return ctypes.windll.user32.DefWindowProcW(hwnd, message, wparam, lparam)


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, help="Attach the tray to an existing MemoryCore AI process ID.")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="Working directory for launched command.")
    parser.add_argument("--metrics", type=Path, help="Payload-free metrics JSONL written by monitored_native_mcp.")
    parser.add_argument("--database", type=Path, help="MemoryCore AI SQLite or SQLCipher database path for size reporting.")
    parser.add_argument("--preferences", type=Path, help="Tray preferences JSON path.")
    parser.add_argument("--usage-report", type=Path, help="Verified lifecycle usage comparison JSON for token/savings reporting.")
    parser.add_argument("--icon", type=Path, default=default_icon_path(), help="Windows .ico file to use for the notification-area icon.")
    parser.add_argument("--vault-dir", type=Path, help="Vault folder to open from the tray menu.")
    parser.add_argument("--set-min-idle-hours", type=float, help="Set the auto-generation idle delay preference.")
    parser.add_argument("--set-auto-archive-hours", type=float, help="Set the auto-archive timing preference.")
    parser.add_argument("--set-auto-delete-days", type=int, help="Set the auto-delete/archive-retention preference.")
    parser.add_argument("--status-file", type=Path, help="Optional JSON status snapshot path.")
    parser.add_argument("--keep-after-exit", action="store_true", help="Keep tray visible if the child exits.")
    parser.add_argument("--status-json", action="store_true", help="Print one status snapshot and exit.")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command to launch after --.")
    args = parser.parse_args(argv)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if args.pid and command:
        parser.error("--pid cannot be combined with a launch command")
    if not args.pid and not command:
        parser.error("provide --pid or a launch command after --")
    args.command = command
    return args


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.preferences and any(
        value is not None
        for value in (args.set_min_idle_hours, args.set_auto_archive_hours, args.set_auto_delete_days)
    ):
        update_preferences(
            args.preferences,
            min_idle_seconds=(
                int(args.set_min_idle_hours * 3600) if args.set_min_idle_hours is not None else None
            ),
            auto_archive_after_seconds=(
                int(args.set_auto_archive_hours * 3600)
                if args.set_auto_archive_hours is not None
                else None
            ),
            auto_delete_after_days=args.set_auto_delete_days,
        )
    process = MemoryCoreProcess(command=args.command or None, pid=args.pid, cwd=args.cwd)
    if args.status_json:
        if process.command:
            process.start()
        else:
            process.started_at = now()
        snapshot = status_snapshot(
            process,
            args.metrics,
            database=args.database,
            preferences=args.preferences,
            usage_report=args.usage_report,
        )
        write_status_file(args.status_file, snapshot)
        print(json.dumps(snapshot, sort_keys=True))
        process.stop()
        return
    app = TrayApp(
        process,
        metrics=args.metrics,
        status_file=args.status_file,
        project_dir=args.cwd,
        keep_after_exit=args.keep_after_exit,
        database=args.database,
        preferences=args.preferences,
        usage_report=args.usage_report,
        icon=args.icon,
        vault_dir=args.vault_dir,
    )
    app.run()


if __name__ == "__main__":
    main()
