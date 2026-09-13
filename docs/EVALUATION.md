# Evaluating memory and token efficiency

I want lower total cost with preserved correctness. I do not treat a smaller recall response as sufficient evidence that a whole task became cheaper.

## Comparisons I consider meaningful

I compare the same task, evidence, client, model and reasoning settings across direct source reading, repeated-history context, and selective memory retrieval. I include:

- Acquisition, extraction, review and memory-write costs.
- Startup instructions and tool catalogues.
- All intermediate calls, returned context and final answers.
- Source validation, corrections, retries and rework.
- Input/output usage and caching, reported separately where available.
- Cold startup, warm recall, missed evidence, abstention and correctness.
- Local CPU, memory, latency and index catch-up alongside token usage.

I label unavailable usage as unavailable, rather than treating it as zero. I use actual provider counters when supplied and identify tokenizer estimates separately. Storage compression and vector quantisation are separate measurements.

## Reproducible tools

| Script | Purpose |
| --- | --- |
| `scripts/evaluate_synthetic.py` | Core synthetic evaluation |
| `scripts/evaluate_knowledge.py` | Retrieval and source-aware comparisons |
| `scripts/evaluate_sessions.py` | Matched deterministic session replay |
| `scripts/evaluate_acceptance.py` | Synthetic acceptance evaluation |
| `scripts/benchmark_memory_host.py` | Native semantic-host load/resource experiments |
| `scripts/benchmark_graph_memory.py` | Bounded graph retrieval experiments |

I inspect each script’s `--help` and use disposable synthetic paths. A deterministic replay does not call a model and cannot establish generated-answer quality or billed savings. A short load run does not establish an hours-long stability result or a service-level guarantee.

## Results I invite

I welcome compact reports with the commit, environment, exact command, synthetic fixture, repetitions, correctness criteria, token-accounting boundary and failures. Please exclude private prompts, raw conversation logs, credentials and private source content. I prefer reproducible evidence over headline percentages.
