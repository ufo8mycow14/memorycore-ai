# Windows Tray Companion

`scripts/windows_tray.py` is a local Windows notification-area companion for a MemoryCore AI process. It does not read or write memory payloads. It watches a process, keeps a tray icon alive while MemoryCore AI is alive, and can write a status snapshot or open payload-free metrics, preferences and usage reports.

## Standard Local Launcher

For the standard local profile created by `scripts/setup_local_rollout.py`, use the wrapper:

```powershell
python -B scripts\start_windows_tray.py
```

The wrapper uses `C:\Users\<you>\.codex\memorycore-ai-local` by default. It launches `scripts.monitored_native_mcp`, points the tray at the local metrics JSONL, database, preferences file, status file and vault folder, and uses the bundled tray icon automatically.

To inspect the generated command without starting the tray:

```powershell
python -B scripts\start_windows_tray.py --print-command
```

To use a different local profile:

```powershell
python -B scripts\start_windows_tray.py --home D:\MemoryCoreProfiles\work
```

## Launching a MemoryCore AI command

Build or choose the MemoryCore AI command you want to supervise, then place it after `--`:

```powershell
python -B scripts\windows_tray.py `
  --cwd . `
  --metrics outputs\memorycore-ai-metrics.jsonl `
  --database C:\Users\kaoro\.codex\memorycore-ai-local\vault\memorycore-ai.sqlite3 `
  --preferences C:\Users\kaoro\.codex\memorycore-ai-local\tray-preferences.json `
  --usage-report outputs\memorycore-ai-usage.json `
  --status-file outputs\memorycore-ai-tray-status.json `
  -- python -B -m scripts.monitored_native_mcp serve --binary rust-broker\target\release\memorycore-ai-broker.exe --config C:\Users\kaoro\.codex\memorycore-ai-local\host.json --cache C:\Users\kaoro\.codex\memorycore-ai-local\cache --session memorycore-ai-local --metrics outputs\memorycore-ai-metrics.jsonl
```

The companion exits when the launched process exits unless `--keep-after-exit` is supplied.

The project icon is saved as `docs/assets/memorycore-ai-icon.png`, with the Windows tray/app version at `docs/assets/memorycore-ai-icon.ico`. The tray uses the `.ico` automatically unless `--icon` is supplied.

## Attaching to an Existing Process

```powershell
python -B scripts\windows_tray.py --pid 12345 --cwd . --metrics outputs\memorycore-ai-metrics.jsonl
```

Attached processes can be watched and reported on, but only launched processes can be restarted from the tray menu.

## Preferences

The tray keeps UI-editable preferences in a separate JSON file so it does not add unsupported fields to the strict native host configuration:

```powershell
python -B scripts\windows_tray.py `
  --pid 12345 `
  --preferences C:\Users\kaoro\.codex\memorycore-ai-local\tray-preferences.json `
  --set-min-idle-hours 24 `
  --set-auto-archive-hours 168 `
  --set-auto-delete-days 365 `
  --status-json
```

`--set-min-idle-hours` controls the tray preference for the chat-generation idle gate that defaults to 24 hours in `scripts/chat_memory.py`. `--set-auto-delete-days` mirrors the native archive-retention control; the live broker command remains `archive-retention` and should be used for enforcement. `--set-auto-archive-hours` is a tray preference until automatic chat/archive ingestion is certified.

## Tray Menu

- `Write status snapshot` writes process state, uptime and metrics summary metadata.
- `Open metrics summary` writes and opens a JSON summary beside the metrics JSONL file.
- `Open tray preferences` creates the preferences file when missing and opens it.
- `Open usage report` opens the configured verified lifecycle usage report when it exists.
- `Open project folder` opens the configured working directory.
- `Open vault folder` opens the configured vault folder when supplied by the launcher.
- `Restart MemoryCore AI` is available only when the tray launched the process.
- `Exit` closes the tray and stops a launched child process.

The metrics path is expected to be the JSONL file produced by `scripts.monitored_native_mcp`. Those metrics are designed to be payload-free.

`--usage-report` can point at a verified `scripts/lifecycle_usage.py` comparison output. The tray reports totals and savings from that report without storing raw prompts, recalled text or private source content.

The status snapshot includes a compact `labels` block for UI surfaces: health, mode, PID, uptime, database presence and size, read-only state, idle-generation hours, auto-archive hours, auto-delete days and token-saving percentage when a verified report supplies one.

When cache data is present, the snapshot keeps three cache stories separate:

- `provider_prompt_cache`: provider-reported input, cached input, cache-write input, uncached input, output, reasoning output and derived percentages.
- `memorycore_packet_cache`: MemoryCore recall-packet cacheable recalls, hits, misses, hit rate and estimated full retrievals avoided.
- `context_reduction`: estimated fresh-task input reduction from replacing stale conversation context with a compact MemoryCore recall packet.

The `labels` block mirrors the most useful cache values as `provider_cached_input_percent`, `provider_uncached_input_percent`, `provider_cache_write_input_tokens`, `memorycore_packet_cache_hit_rate_percent`, `memorycore_full_retrievals_avoided`, `estimated_fresh_task_input_saving_percent` and `best_projected_cost_saving_percent`.

## Useful User Surface

The tray should stay quiet by default and make the important state obvious at a glance:

- health: running, stopped, starting, degraded, or indexing;
- privacy mode: synthetic fixture, plaintext local vault, or encrypted SQLCipher vault;
- current profile: active scope/session name and whether writes are allowed;
- retrieval health: vector index ready/warming/unavailable and reranker ready/unavailable;
- activity: recalls, writes, reviews and errors over recent time windows;
- queue state: pending index work, deferred work and oldest pending age;
- resource pressure: memory pressure, system CPU pressure, host CPU pressure and active inference limit;
- storage state: active memories, archived memories, staged items, tombstones and last verify result;
- database size: configured database path, file existence and byte size;
- accounting: input tokens, cached input tokens, cache-write tokens, ordinary input tokens, output tokens, reasoning tokens and observed savings from verified usage reports;
- speed: recent median/p95/p99 monitored MCP latency and native database timing once a live stats bridge is connected;
- cache proof: provider prompt-cache reuse, MemoryCore packet-cache hit rate, estimated full retrievals avoided and model/reasoning cost projections when supplied by verified reports;
- freshness state: changed or unavailable bound sources needing review;
- retention state: archive retention default, next cleanup and items awaiting review.

Good first controls are deliberately conservative:

- pause or resume background indexing;
- open the project folder, metrics summary, host config or vault folder;
- restart the launched MemoryCore AI process;
- edit the 24-hour idle generation delay preference;
- edit auto-archive timing preference;
- edit auto-delete/archive-retention days and apply it through the broker `archive-retention` command;
- run a verification check and show only the result;
- switch between reviewed local profiles;
- toggle read-only mode for maintenance;
- export a payload-free diagnostic bundle;
- open documentation for setup, security and evaluation.

Controls that touch durable memory content, deletion, source ingestion, private chat capture or live host configuration should require an explicit foreground confirmation in a proper app window, not a one-click tray menu item.
