# Model-Aware Caching Strategy

MemoryCore AI should optimise for correct work that costs less end to end. Prompt caching can help, but it only helps when the provider can reuse a stable rendered prefix and when usage counters prove the saving.

## Current OpenAI Constraints

OpenAI prompt caching reuses work for matching prompt prefixes. The rendered prefix includes instructions, tool definitions, developer messages and conversation history. A change in model, tools, structured output, reasoning effort, verbosity or context management can stop reuse after the first changed token.

For GPT-5.6 and later, OpenAI supports explicit cache breakpoints and `prompt_cache_options`. Earlier models use implicit caching. Usage can report `cached_tokens` and `cache_write_tokens`; both need to be tracked separately from ordinary input. Reasoning models also report reasoning tokens as part of output usage, and reasoning settings can affect caching and cost.

Sources checked on 2026-09-13:

- OpenAI prompt caching guide: https://developers.openai.com/api/docs/guides/prompt-caching
- OpenAI reasoning models guide: https://developers.openai.com/api/docs/guides/reasoning

## Design Principle

Do not make MemoryCore AI a second prompt cache. Make it a cache-aware context planner:

- keep stable, reusable material at the beginning of requests;
- put changing recall packets after stable breakpoints;
- use provider usage counters to measure actual cached input, cache writes, output and reasoning tokens;
- compare only like-for-like model profiles;
- never count byte compression, vector compression or local cache hits as provider-token savings.

## Prompt Shape

Use a three-zone request layout where the client supports it:

1. Stable foundation: system/developer policy, tool schemas, output contract and durable project rules.
2. Semi-stable project context: selected MemoryCore operating profile, source boundaries, retention policy and current tool catalogue digest.
3. Volatile task context: fresh recall packet, user request, source excerpts, latest tool results and working notes.

Cache breakpoints should sit after zones 1 and 2. Zone 3 should be as small and source-checked as possible, but it should not be placed before stable material.

## Model Profile

Every measured request should record:

- provider;
- exact model ID or model alias at the time of the call;
- reasoning effort;
- reasoning mode;
- reasoning context;
- text verbosity;
- tool catalogue digest;
- output schema digest;
- cache key and cache option mode when available;
- input, cached input, cache-write input, output and reasoning token counters.

Comparisons across different profiles should be labelled as migrations or tuning experiments, not direct savings claims.

## Supporting All Models and Reasoning Levels

Represent model support as a capability matrix rather than hard-coded assumptions:

- `cache_mode`: none, implicit, explicit, or unknown;
- `cache_ttl`: observed or documented value, not guessed;
- `reasoning_efforts`: supported values for that model;
- `reasoning_modes`: standard/pro/other supported modes;
- `reasoning_context`: current-turn/all-turns/unsupported;
- `usage_fields`: which counters the provider returns;
- `minimum_cacheable_length`: documented or unknown;
- `supports_breakpoints`: true, false or unknown.

Unknown fields must disable savings claims, not the whole run.

## Tray Stats Worth Showing

The tray can show a simple caching panel without exposing payloads:

- cache hit rate by model profile;
- cached input tokens, cache-write tokens and ordinary input tokens over the last hour/day;
- estimated weighted input cost where pricing is explicitly configured;
- reasoning tokens by effort level;
- cache misses grouped by likely cause: model change, tool/schema change, verbosity change, reasoning change, compacted context, short prefix or expired cache;
- top stable-prefix digests by reuse count, never raw prompt text;
- volatile packet size and omitted-memory counts;
- quality guardrail status for paired evaluations.

## Implementation Plan

1. Extend usage accounting to track `cache_write_tokens` and exact model profiles.
2. Add a model capability registry loaded from local JSON and refreshed only by explicit review.
3. Add a request-layout planner that emits stable-prefix, semi-stable and volatile sections with digests.
4. Add an OpenAI adapter path that records actual Responses usage fields when available.
5. Teach evaluation fixtures to run cold/warm/miss cases across each supported model profile and reasoning level.
6. Add payload-free cache metrics to `monitored_native_mcp` and expose summaries through the Windows tray.
7. Add cache-miss diagnostics that compare request digests and settings, not prompt text.
8. Gate any public savings claim on matched model profile, matched task quality and complete observed usage.

## Non-Goals

- Do not persist raw private prompts solely to debug cache behaviour.
- Do not claim universal savings across models or reasoning levels.
- Do not auto-switch reasoning effort to chase cache hits when task quality needs a higher setting.
- Do not treat provider cache retention as durable memory.
