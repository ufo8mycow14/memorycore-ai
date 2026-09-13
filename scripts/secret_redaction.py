"""Conservative text redaction followed by the unchanged rejection policy."""
import re
from .memory_policy import check_text, MAX_TEXT_BYTES


def redact_text(value):
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError("invalid redaction input")
    if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise ValueError("unsupported redaction input")
    if re.search(r"(?im)\b(?:password|passphrase|passwd|api[_ -]?key|client[_ -]?secret|"
                 r"access[_ -]?token|refresh[_ -]?token|session[_ -]?(?:token|cookie))"
                 r"[ \t]*[\"']?[ \t]*[:=][ \t]*$", value):
        raise ValueError("ambiguous multiline sensitive value")
    # A private-key block may span lines. An incomplete block remains rejected.
    value, blocks = re.subn(
        r"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY)-----[\s\S]*?-----END \1-----",
        "[REDACTED PRIVATE KEY]", value)
    lines, count = [], blocks
    source_lines = value.splitlines(keepends=True)
    for index, line in enumerate(source_lines):
        try:
            check_text(line)
        except SystemExit as error:
            if str(error) not in {
                "memory policy rejected possible authentication value",
                "memory policy rejected possible authentication header",
                "memory policy rejected possible known credential format",
                "memory policy rejected possible credential URL",
            }:
                raise ValueError("source cannot be safely redacted") from None
            # Multiline quoted assignments are ambiguous; do not leave their
            # continuation behind after removing just the opening line.
            next_line = source_lines[index + 1] if index + 1 < len(source_lines) else ""
            if (line.count('"') % 2 or line.count("'") % 2
                    or line.rstrip().endswith(("\\", "|", ">", "|-", ">-", "|+", ">+"))
                    or (next_line.strip() and next_line[:1].isspace())):
                raise ValueError("ambiguous multiline sensitive value")
            lines.append("[REDACTED SENSITIVE LINE]" + ("\n" if line.endswith("\n") else ""))
            count += 1
        else:
            lines.append(line)
    text = "".join(lines)
    check_text(text)
    return {"text": text, "redactions": count, "format": "redacted-text/1"}
