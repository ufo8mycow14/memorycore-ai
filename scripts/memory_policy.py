"""Conservative, local prototype input policy. Not a universal secret detector."""

import json
import re
from datetime import datetime, timezone

MAX_TEXT_BYTES = 1024 * 1024
MAX_EXACT_BYTES = 8 * 1024 * 1024
MAX_QUERY_TERMS = 64
MAX_RESULTS = 100


def timestamp(value, *, optional=True):
    if value is None and optional:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise SystemExit("timestamp must be ISO-8601 with an explicit timezone") from None


def bounded_integer(value, label, minimum=1, maximum=MAX_RESULTS):
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise SystemExit(f"{label} must be between {minimum} and {maximum}")
    return value


def check_text(value, *, maximum=MAX_TEXT_BYTES):
    if not isinstance(value, str):
        raise SystemExit("text field must be a string")
    if len(value.encode("utf-8")) > maximum:
        raise SystemExit("input exceeds the supported byte limit")
    if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise SystemExit("unsupported control characters in text")
    patterns = (
        ("private key", r"-----BEGIN (?:[A-Z0-9 ]* )?PRIVATE KEY-----"),
        ("authentication value", r"\b(?:password|passphrase|passwd|api[_ -]?key|client[_ -]?secret|access[_ -]?token|refresh[_ -]?token|session[_ -]?(?:token|cookie)|csrf[_ -]?token|otp|totp|pin|cvv|cvc|bsb|iban|routing[_ -]?number|bank[_ -]?account)\s*[\"']?\s*[:=]\s*[^\s,;}]+"),
        ("authentication header", r"\b(?:authorization\s*:\s*)?bearer\s+[A-Za-z0-9._~+/=-]{8,}"),
        ("known credential format", r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})\b"),
        ("credential URL", r"https?://[^\s/@:]+:[^\s/@]+@|[?&](?:token|key|secret|code|password)=[^\s&#]+"),
    )
    for category, pattern in patterns:
        if re.search(pattern, value, flags=re.IGNORECASE):
            raise SystemExit(f"memory policy rejected possible {category}")
    iban_lengths = {"DE": 22, "BE": 16, "FR": 27, "GB": 22, "ES": 24, "IT": 27,
                    "NL": 18, "CH": 21, "AT": 20, "IE": 22, "PL": 28, "PT": 25}
    for match in re.finditer(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b", value):
        iban = match[0].replace(" ", "")
        if iban_lengths.get(iban[:2]) == len(iban):
            rearranged = iban[4:] + iban[:4]
            number = "".join(str(ord(c) - 55) if c.isalpha() else c for c in rearranged)
            if int(number) % 97 == 1:
                raise SystemExit("memory policy rejected possible bank identifier")
    # Exempt only complete UUID/fingerprint-shaped hex tokens, not arbitrary
    # alphanumeric wrappers around payment numbers. Contextual gates above
    # remain authoritative even for values shaped like a fingerprint.
    fingerprints = [(m.start(), m.end()) for m in re.finditer(
        r"(?<![A-Za-z0-9])[a-fA-F0-9]{32}(?:[a-fA-F0-9]{32})?(?![A-Za-z0-9])", value)
        if re.search(r"[a-fA-F]", m[0])]
    for match in re.finditer(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)", value):
        numeric_end = match.start() + len(match[0].rstrip(" -"))
        if any(start <= match.start() and numeric_end <= end for start, end in fingerprints):
            continue
        digits = [int(c) for c in match[0] if c.isdigit()]
        if 13 <= len(digits) <= 19:
            total = sum(d if i % 2 == 0 else (2 * d - 9 if d >= 5 else 2 * d)
                        for i, d in enumerate(reversed(digits)))
            if total % 10 == 0:
                raise SystemExit("memory policy rejected possible payment account")
    return value


def check_exact(original, media_type):
    if not original or len(original) > MAX_EXACT_BYTES:
        raise SystemExit("exact input is empty or exceeds the supported byte limit")
    kind = media_type.split(";", 1)[0].strip().lower()
    if kind not in {"text/plain", "text/markdown", "application/json"}:
        raise SystemExit("unsupported exact format; only inspectable UTF-8 text, Markdown and JSON are supported")
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError:
        raise SystemExit("exact input must be inspectable UTF-8") from None
    check_text(text, maximum=MAX_EXACT_BYTES)
    if kind == "application/json":
        class ObjectPairs(list):
            pass
        try:
            # Preserve duplicate keys so overwritten content is inspected too.
            obj = json.loads(text, object_pairs_hook=ObjectPairs)
        except (ValueError, RecursionError):
            raise SystemExit("exact JSON is invalid or too deeply nested") from None
        pending = [(obj, 0)]
        while pending:
            value, depth = pending.pop()
            if depth > 32:
                raise SystemExit("exact JSON nesting limit exceeded")
            if isinstance(value, ObjectPairs):
                for key, item in value:
                    check_text(str(key))
                    # Check decoded keys even when the value is a container or null.
                    check_text(f"{key}=present", maximum=MAX_EXACT_BYTES)
                    pending.append((item, depth + 1))
            elif isinstance(value, list):
                pending.extend((item, depth + 1) for item in value)
            elif isinstance(value, str):
                check_text(value, maximum=MAX_EXACT_BYTES)
    return original


def read_bounded(stream, maximum):
    data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise SystemExit("input exceeds the supported byte limit")
    return data
