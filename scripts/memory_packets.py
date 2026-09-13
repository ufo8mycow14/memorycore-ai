"""Budgeted data packets and explicitly acknowledged, session-local recall reuse."""

import hashlib
import json


def token_counter(encoding="o200k_base"):
    try:
        import tiktoken
        encoder = tiktoken.get_encoding(encoding)
    except (ImportError, ValueError, OSError):
        raise SystemExit("tokenizer unavailable; install the pinned optional tokenizer and cache its encoding") from None
    return lambda text: len(encoder.encode(text, disallowed_special=()))


def render_packet(result, *, max_tokens=700, reserve_tokens=32, max_chars=2500,
                  include_ids=False, encoding="o200k_base", count=None, output_format="prompt"):
    if max_tokens < 0 or reserve_tokens < 0 or max_chars < 0:
        raise SystemExit("output budgets must not be negative")
    count = count or token_counter(encoding)
    allowance = max(0, max_tokens - reserve_tokens)
    memories = result["memories"]
    scope = result["scope"]
    selected = []
    omitted = len(memories)

    def render(rows, omitted_count):
        if output_format == "json":
            return json.dumps({"scope": scope, "revision": result["revision"], "count": len(rows),
                               "omitted": omitted_count, "candidate_limit_reached": result.get("candidate_limit_reached", False),
                               "memories": rows}, ensure_ascii=False, separators=(",", ":"))
        # JSON-quoted values keep user content from inventing record boundaries.
        shared = {}
        for field in ("type", "source", "confidence", "observed_at"):
            values = {json.dumps(row[field], ensure_ascii=False) for row in rows}
            if len(values) == 1:
                shared[field] = rows[0][field]
        header = "Memory data; " + json.dumps({"scope": scope, **shared}, ensure_ascii=False, separators=(",", ":"))
        lines = [header]
        for row in rows:
            fields = {key: row[key] for key in ("type", "source", "confidence", "observed_at") if key not in shared}
            fields.update({"subject": row["subject"], "summary": row["summary"]})
            for field in ("valid_from", "valid_to", "source_hash", "confidence_reason", "detail"):
                if row.get(field):
                    fields[field] = row[field]
            if include_ids:
                fields["id"] = row["memory_id"]
            lines.append(json.dumps(fields, ensure_ascii=False, separators=(",", ":")))
        lines.append(f"omitted={omitted_count}; candidates_capped={str(result.get('candidate_limit_reached', False)).lower()}")
        return "\n".join(lines)

    def fits(text):
        return len(text) <= max_chars and count(text) <= allowance

    for memory in memories:
        candidate = render([*selected, memory], omitted - 1)
        if fits(candidate):
            selected.append(memory)
            omitted -= 1
    text = render(selected, omitted)
    if not fits(text):
        raise SystemExit("budget too small for the omission receipt; increase the output budget")
    return {"text": text, "tokens": count(text), "reserved_tokens": reserve_tokens,
            "included": len(selected), "omitted": omitted, "encoding": encoding,
            "digest": hashlib.sha256(text.encode()).hexdigest()}


class RecallSession:
    """Caller-owned volatile cache. No persisted context or implicit possession."""

    def __init__(self, session_id):
        self.session_id = session_id
        self._last = None

    def reset(self):
        self._last = None

    def respond(self, packet, *, vault_id, scopes, revision, query, representation,
                acknowledgement=None, context_retained=False, permission_revision=0):
        key = (self.session_id, vault_id, tuple(sorted(scopes)), revision, query,
               representation, permission_revision, packet["digest"])
        unchanged = bool(context_retained and self._last == key and acknowledgement == packet["digest"])
        self._last = key
        return {"unchanged": True, "digest": packet["digest"]} if unchanged else dict(packet, unchanged=False)
