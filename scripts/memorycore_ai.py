#!/usr/bin/env python3
"""Dependency-free binary memory store for the MemoryCore AI skill."""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import sys
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from .memory_policy import (MAX_EXACT_BYTES, MAX_QUERY_TERMS, MAX_RESULTS, MAX_TEXT_BYTES,
                                bounded_integer, check_exact, check_text, read_bounded, timestamp)
    from .memory_packets import render_packet
except ImportError:
    from memory_policy import (MAX_EXACT_BYTES, MAX_QUERY_TERMS, MAX_RESULTS, MAX_TEXT_BYTES,
                               bounded_integer, check_exact, check_text, read_bounded, timestamp)
    from memory_packets import render_packet

SCHEMA_VERSION = 2
MAX_DECOMPRESSED = MAX_EXACT_BYTES + 65536

MEMORY_TYPES = {
    "semantic": 1,
    "episodic": 2,
    "procedural": 3,
    "priming_conditioning": 4,
    "classical_conditioning": 5,
}
TYPE_NAMES = {value: key for key, value in MEMORY_TYPES.items()}
SENSITIVITY = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
SENSITIVITY_NAMES = {value: key for key, value in SENSITIVITY.items()}
WIRE_MAGIC = b"BM1"
STATUS_NAMES = {0: "active", 1: "superseded", 2: "deleted", 3: "expired", 4: "archived"}
STAGE_STATUS_NAMES = {0: "active", 1: "consolidated", 2: "expired"}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varints cannot be negative")
    output = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        output.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(output)


def decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if offset >= len(data) or shift > 63:
            raise ValueError("invalid MemoryCore AI varint")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7


def pack_text(value: str) -> bytes:
    raw = value.encode("utf-8")
    return encode_varint(len(raw)) + raw


def unpack_text(data: bytes, offset: int) -> tuple[str, int]:
    length, offset = decode_varint(data, offset)
    end = offset + length
    if end > len(data):
        raise ValueError("truncated MemoryCore AI payload")
    return data[offset:end].decode("utf-8"), end


def compress(raw: bytes) -> bytes:
    zipped = zlib.compress(raw, level=9)
    return b"Z" + zipped if len(zipped) < len(raw) else b"N" + raw


def decompress(blob: bytes) -> bytes:
    if not blob:
        return b""
    if blob[:1] == b"Z":
        decoder = zlib.decompressobj()
        try:
            raw = decoder.decompress(blob[1:], MAX_DECOMPRESSED + 1)
        except zlib.error:
            raise SystemExit("invalid compressed memory") from None
        if len(raw) > MAX_DECOMPRESSED or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise SystemExit("compressed memory exceeds limits or has invalid boundaries")
        return raw
    if blob[:1] == b"N":
        if len(blob) - 1 > MAX_DECOMPRESSED:
            raise SystemExit("memory exceeds decompression limit")
        return blob[1:]
    raise ValueError("unknown MemoryCore AI compression marker")


def encode_payload(subject: str, summary: str, detail: str, keywords: str, source: str) -> bytes:
    """CMN/1 wire record: magic plus five varint-length UTF-8 fields."""
    raw = WIRE_MAGIC + b"".join(pack_text(value.strip()) for value in (subject, summary, detail, keywords, source))
    return compress(raw)


def decode_payload(blob: bytes) -> dict[str, str]:
    raw = decompress(blob)
    return decode_raw_payload(raw)


def decode_raw_payload(raw: bytes) -> dict[str, str]:
    if not raw.startswith(WIRE_MAGIC):
        raise ValueError("unsupported MemoryCore AI wire version")
    offset = len(WIRE_MAGIC)
    values = []
    for _ in range(5):
        value, offset = unpack_text(raw, offset)
        values.append(value)
    if offset != len(raw):
        raise ValueError("unexpected trailing MemoryCore AI data")
    return dict(zip(("subject", "summary", "detail", "keywords", "source"), values))


def tokenize(value: str) -> set[str]:
    cleaned = "".join(character.lower() if character.isalnum() else " " for character in value)
    tokens = {token for token in cleaned.split() if len(token) > 1}
    # Preserve original identifier fragments; add conservative English inflections.
    for token in tuple(tokens):
        if token.isalpha() and token.isascii() and len(token) > 4:
            if token.endswith("ies"):
                tokens.add(token[:-3] + "y")
            elif token.endswith("s") and not token.endswith(("ss", "us", "is")):
                tokens.add(token[:-1])
    return tokens - {"the", "and", "for", "with", "from", "this", "that", "what", "where", "does", "are", "is", "of", "to", "in"}


def term_hash(token: str) -> bytes:
    return hashlib.blake2b(token.encode("utf-8"), digest_size=8, person=b"BMemTerm").digest()


def default_home() -> Path:
    override = os.environ.get("MEMORYCORE_AI_HOME")
    if override:
        base = Path(override).expanduser().resolve()
        forbidden = {Path(base.anchor).resolve(), Path.home().resolve(), Path.cwd().resolve()}
        if base in forbidden:
            raise SystemExit("MEMORYCORE_AI_HOME must be a dedicated subdirectory, not a filesystem, home, or working-directory root")
        return base
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA")
        return (Path(root) if root else Path.home() / "AppData" / "Local") / "MemoryCoreAI"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MemoryCoreAI"
    root = os.environ.get("XDG_DATA_HOME")
    return (Path(root).expanduser() if root else Path.home() / ".local" / "share") / "memorycore-ai"


def default_db_path() -> Path:
    return default_home() / "vault" / "memorycore-ai.sqlite3"


def connect(db_path: str | None, *, read_only=False) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve() if db_path else default_db_path().resolve()
    if read_only:
        if not path.is_file():
            raise SystemExit("memory database does not exist; initialise it explicitly")
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    conn = sqlite3.connect(path)
    if os.name != "nt":
        os.chmod(path, 0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA secure_delete=ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS hippocampus_stage (
    stage_id BLOB PRIMARY KEY CHECK(length(stage_id)=16),
    created_at TEXT NOT NULL,
    expires_at TEXT,
    scope TEXT NOT NULL,
    source TEXT NOT NULL,
    raw_blob BLOB NOT NULL,
    raw_bytes INTEGER NOT NULL,
    stored_bytes INTEGER NOT NULL,
    checksum_sha256 BLOB NOT NULL CHECK(length(checksum_sha256)=32),
    status INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cortex_memory (
    memory_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id BLOB UNIQUE NOT NULL CHECK(length(memory_id)=16),
    memory_type INTEGER NOT NULL,
    scope TEXT NOT NULL,
    payload_blob BLOB NOT NULL,
    payload_raw_bytes INTEGER NOT NULL,
    payload_stored_bytes INTEGER NOT NULL,
    importance INTEGER NOT NULL CHECK(importance BETWEEN 0 AND 255),
    confidence INTEGER NOT NULL CHECK(confidence BETWEEN 0 AND 255),
    sensitivity INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    status INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    supersedes_id BLOB,
    checksum_sha256 BLOB NOT NULL CHECK(length(checksum_sha256)=32),
    FOREIGN KEY(supersedes_id) REFERENCES cortex_memory(memory_id)
);

CREATE TABLE IF NOT EXISTS cortex_verbatim (
    archive_id BLOB PRIMARY KEY CHECK(length(archive_id)=16),
    scope TEXT NOT NULL,
    media_type TEXT NOT NULL,
    original_blob BLOB NOT NULL,
    original_bytes INTEGER NOT NULL,
    stored_bytes INTEGER NOT NULL,
    content_sha256 BLOB NOT NULL CHECK(length(content_sha256)=32),
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    retention TEXT NOT NULL,
    expires_at TEXT,
    pinned INTEGER NOT NULL DEFAULT 1,
    status INTEGER NOT NULL DEFAULT 0,
    linked_memory_id BLOB,
    FOREIGN KEY(linked_memory_id) REFERENCES cortex_memory(memory_id)
);

CREATE TABLE IF NOT EXISTS cortex_term (
    memory_pk INTEGER NOT NULL,
    term_hash BLOB NOT NULL CHECK(length(term_hash)=8),
    PRIMARY KEY(memory_pk, term_hash),
    FOREIGN KEY(memory_pk) REFERENCES cortex_memory(memory_pk) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS cortex_scope_status ON cortex_memory(scope, status, memory_type);
CREATE INDEX IF NOT EXISTS cortex_expiry ON cortex_memory(expires_at);
CREATE INDEX IF NOT EXISTS cortex_term_hash ON cortex_term(term_hash);
CREATE INDEX IF NOT EXISTS verbatim_scope_status ON cortex_verbatim(scope, status);
CREATE INDEX IF NOT EXISTS verbatim_expiry ON cortex_verbatim(expires_at);

CREATE TABLE IF NOT EXISTS cortex_detail (
    memory_id BLOB PRIMARY KEY REFERENCES cortex_memory(memory_id) ON DELETE CASCADE,
    detail_blob BLOB NOT NULL,
    checksum_sha256 BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS cortex_tombstone (
    memory_id BLOB PRIMARY KEY, scope TEXT NOT NULL, removed_at TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS vault_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), vault_id TEXT NOT NULL, revision INTEGER NOT NULL
);
"""


def initialize(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise SystemExit("database schema is newer than this implementation")
    if version == SCHEMA_VERSION:
        return
    if conn.in_transaction:
        raise SystemExit("migration requires a connection with no active transaction")
    additions = {
        "cortex_memory": {"pinned": "INTEGER NOT NULL DEFAULT 0", "content_fingerprint": "BLOB",
            "detail_sha256": "BLOB", "observed_at": "TEXT", "valid_from": "TEXT", "valid_to": "TEXT",
            "source_hash": "TEXT", "confidence_reason": "TEXT NOT NULL DEFAULT ''", "claim_id": "TEXT",
            "prior_version_id": "BLOB", "stage_id": "BLOB", "record_checksum": "BLOB"},
        "cortex_verbatim": {"record_checksum": "BLOB"},
        "hippocampus_stage": {"record_checksum": "BLOB"},
    }
    try:
        conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
        for table, fields in additions.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for field, declaration in fields.items():
                if field not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {field} {declaration}")
        conn.execute("INSERT OR IGNORE INTO vault_state VALUES (1,?,0)", (uuid.uuid4().hex,))
        roots = validate_links(conn, migrating=True)
        for row in conn.execute("SELECT * FROM cortex_memory").fetchall():
            raw = decompress(row["payload_blob"])
            canonical = bytes((row["memory_type"], row["sensitivity"])) + row["scope"].encode() + raw
            if hashlib.sha256(canonical).digest() != row["checksum_sha256"]:
                raise SystemExit("migration rejected corrupt semantic content")
            fields = decode_raw_payload(raw)
            for text in (*fields.values(), row["scope"]):
                check_text(text)
            detail = fields["detail"].encode()
            payload = encode_payload(fields["subject"], fields["summary"], "", fields["keywords"], fields["source"])
            canonical_summary = bytes((row["memory_type"], row["sensitivity"])) + row["scope"].encode() + decompress(payload)
            fingerprint = hashlib.sha256(canonical_summary + hashlib.sha256(detail).digest()).digest()
            conn.execute("INSERT INTO cortex_detail VALUES (?,?,?)", (row["memory_id"], compress(detail), hashlib.sha256(detail).digest()))
            conn.execute("""UPDATE cortex_memory SET payload_blob=?,payload_raw_bytes=?,payload_stored_bytes=?,
                checksum_sha256=?,content_fingerprint=?,detail_sha256=?,observed_at=?,valid_from=?,expires_at=?,
                claim_id=?,prior_version_id=supersedes_id WHERE memory_id=?""",
                (payload, len(decompress(payload)), len(payload), hashlib.sha256(canonical_summary).digest(), fingerprint,
                 hashlib.sha256(detail).digest(), timestamp(row["created_at"]), timestamp(row["created_at"]),
                 timestamp(row["expires_at"]), roots[row["memory_id"]].hex(), row["memory_id"]))
            conn.execute("UPDATE cortex_memory SET created_at=?,updated_at=? WHERE memory_id=?",
                         (timestamp(row["created_at"], optional=False), timestamp(row["updated_at"], optional=False), row["memory_id"]))
            seal_row(conn, "cortex_memory", "memory_id", row["memory_id"])
        for row in conn.execute("SELECT * FROM cortex_verbatim").fetchall():
            original = decompress(row["original_blob"])
            if hashlib.sha256(original).digest() != row["content_sha256"]:
                raise SystemExit("migration rejected corrupt exact content")
            check_exact(original, row["media_type"])
            check_text(row["scope"])
            check_text(row["source"])
            conn.execute("UPDATE cortex_verbatim SET expires_at=? WHERE archive_id=?", (timestamp(row["expires_at"]), row["archive_id"]))
            conn.execute("UPDATE cortex_verbatim SET created_at=?,updated_at=? WHERE archive_id=?",
                         (timestamp(row["created_at"], optional=False), timestamp(row["updated_at"], optional=False), row["archive_id"]))
            seal_row(conn, "cortex_verbatim", "archive_id", row["archive_id"])
        for row in conn.execute("SELECT * FROM hippocampus_stage").fetchall():
            raw = decompress(row["raw_blob"])
            if hashlib.sha256(raw).digest() != row["checksum_sha256"]:
                raise SystemExit("migration rejected corrupt staged content")
            check_text(raw.decode("utf-8"))
            check_text(row["source"])
            check_text(row["scope"])
            expiry = timestamp(row["expires_at"]) or after_days(1, row["created_at"])
            conn.execute("UPDATE hippocampus_stage SET expires_at=? WHERE stage_id=?", (expiry, row["stage_id"]))
            conn.execute("UPDATE hippocampus_stage SET created_at=? WHERE stage_id=?", (timestamp(row["created_at"], optional=False), row["stage_id"]))
            if row["status"] or expiry <= now_utc():
                dispose_stage(conn, row["stage_id"], 1 if row["status"] == 1 else 2)
            seal_row(conn, "hippocampus_stage", "stage_id", row["stage_id"])
        for scope_row in conn.execute("SELECT scope FROM cortex_memory UNION SELECT scope FROM cortex_verbatim UNION SELECT scope FROM hippocampus_stage").fetchall():
            verify_scope(conn, scope_row[0])
        conn.execute("DELETE FROM cortex_term")
        for row in conn.execute("SELECT * FROM cortex_memory").fetchall():
            index_memory(conn, row["memory_id"])
        conn.execute("CREATE INDEX IF NOT EXISTS cortex_content ON cortex_memory(scope,content_fingerprint,status)")
        for table in ("cortex_memory", "cortex_detail", "cortex_verbatim", "hippocampus_stage", "cortex_tombstone"):
            for event in ("INSERT", "UPDATE", "DELETE"):
                conn.execute(f"CREATE TRIGGER IF NOT EXISTS revision_{table}_{event} AFTER {event} ON {table} BEGIN UPDATE vault_state SET revision=revision+1 WHERE singleton=1; END")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


@contextlib.contextmanager
def transaction(conn, *, write=True):
    nested = conn.in_transaction
    name = "bm_" + uuid.uuid4().hex
    conn.execute(f"SAVEPOINT {name}" if nested else ("BEGIN IMMEDIATE" if write else "BEGIN"))
    try:
        yield
        conn.execute(f"RELEASE {name}") if nested else conn.commit()
    except BaseException:
        if nested:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
        else:
            conn.rollback()
        raise


def record_digest(row):
    values = {k: ({"bytes": row[k].hex()} if isinstance(row[k], bytes) else row[k])
              for k in row.keys() if k not in {"record_checksum", "memory_pk", "hits"}}
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).digest()


def seal_row(conn, table, key, identifier):
    row = conn.execute(f"SELECT * FROM {table} WHERE {key}=?", (identifier,)).fetchone()
    conn.execute(f"UPDATE {table} SET record_checksum=? WHERE {key}=?", (record_digest(row), identifier))


def verify_record(row):
    if not row or row["record_checksum"] != record_digest(row):
        raise SystemExit("record metadata integrity verification failed")


def after_days(days, base=None):
    return (datetime.fromisoformat(timestamp(base or now_utc())) + timedelta(days=days)).isoformat(timespec="seconds")


def dispose_stage(conn, stage_id, status):
    conn.execute("UPDATE hippocampus_stage SET status=?,raw_blob=?,raw_bytes=0,stored_bytes=0,checksum_sha256=? WHERE stage_id=?",
                 (status, b"", hashlib.sha256(b"").digest(), stage_id))
    seal_row(conn, "hippocampus_stage", "stage_id", stage_id)


def index_memory(conn, identifier):
    row = conn.execute("SELECT * FROM cortex_memory WHERE memory_id=?", (identifier,)).fetchone()
    payload = verify_memory_row(row)
    detail = load_detail(conn, row)
    terms = tokenize(" ".join((payload["subject"], payload["summary"], payload["keywords"], detail)))
    conn.execute("DELETE FROM cortex_term WHERE memory_pk=?", (row["memory_pk"],))
    conn.executemany("INSERT INTO cortex_term VALUES (?,?)", ((row["memory_pk"], term_hash(t)) for t in sorted(terms)))


def load_detail(conn, row):
    detail = conn.execute("SELECT * FROM cortex_detail WHERE memory_id=?", (row["memory_id"],)).fetchone()
    if not detail:
        raise SystemExit("semantic detail is missing")
    raw = decompress(detail["detail_blob"])
    digest = hashlib.sha256(raw).digest()
    if digest != detail["checksum_sha256"] or digest != row["detail_sha256"]:
        raise SystemExit("semantic detail integrity verification failed")
    return check_text(raw.decode("utf-8"))


def parse_id(value: str | None) -> bytes | None:
    if not value:
        return None
    try:
        result = bytes.fromhex(value)
    except ValueError as exc:
        raise SystemExit("memory IDs must be hexadecimal") from exc
    if len(result) != 16:
        raise SystemExit("memory IDs must be exactly 128 bits (32 hex characters)")
    return result


def require_scope(scope: str | None) -> str:
    if not scope or not scope.strip():
        raise SystemExit("explicit scope is required")
    return check_text(scope.strip(), maximum=256)


def canonical_bytes(row: sqlite3.Row) -> bytes:
    payload = decompress(row["payload_blob"])
    metadata = bytes((row["memory_type"], row["sensitivity"])) + row["scope"].encode("utf-8")
    return metadata + payload


def verify_memory_row(row: sqlite3.Row) -> dict:
    verify_record(row)
    raw = decompress(row["payload_blob"])
    canonical = bytes((row["memory_type"], row["sensitivity"])) + row["scope"].encode() + raw
    if hashlib.sha256(canonical).digest() != row["checksum_sha256"]:
        raise SystemExit("semantic memory integrity verification failed")
    payload = decode_raw_payload(raw)
    for value in payload.values():
        check_text(value)
    return payload


def consolidate_stage(conn: sqlite3.Connection, stage_hex: str | None, scope: str) -> None:
    if not stage_hex:
        return
    stage_id = parse_id(stage_hex)
    row = conn.execute("SELECT * FROM hippocampus_stage WHERE stage_id=? AND scope=? AND status=0", (stage_id, scope)).fetchone()
    if not row:
        raise SystemExit("active staged memory not found")
    verify_record(row)
    raw = decompress(row["raw_blob"])
    if hashlib.sha256(raw).digest() != row["checksum_sha256"]:
        raise SystemExit("staged memory integrity verification failed")
    check_text(raw.decode("utf-8"))
    if row["expires_at"] <= now_utc():
        raise SystemExit("staged memory has expired")
    dispose_stage(conn, stage_id, 1)


def read_input(args: argparse.Namespace) -> str:
    if getattr(args, "text", None) is not None:
        return args.text
    if getattr(args, "file", None):
        with Path(args.file).open("rb") as stream:
            return read_bounded(stream, MAX_TEXT_BYTES).decode("utf-8")
    return read_bounded(sys.stdin, MAX_TEXT_BYTES)


def read_exact_input(args: argparse.Namespace) -> bytes:
    if getattr(args, "text", None) is not None:
        return args.text.encode("utf-8")
    if getattr(args, "file", None):
        with Path(args.file).open("rb") as stream:
            return read_bounded(stream, MAX_EXACT_BYTES)
    return read_bounded(sys.stdin.buffer, MAX_EXACT_BYTES)


def stage(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    scope = require_scope(args.scope)
    text = check_text(read_input(args))
    check_text(args.source)
    expires = timestamp(args.expires) or after_days(1)
    if not text.strip():
        raise SystemExit("nothing to stage")
    raw = text.encode("utf-8")
    blob = compress(raw)
    stage_id = uuid.uuid4().bytes
    with transaction(conn):
        conn.execute(
            """INSERT INTO hippocampus_stage
            (stage_id,created_at,expires_at,scope,source,raw_blob,raw_bytes,stored_bytes,checksum_sha256)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (stage_id, now_utc(), expires, scope, args.source, blob,
             len(raw), len(blob), hashlib.sha256(raw).digest()),
        )
        seal_row(conn, "hippocampus_stage", "stage_id", stage_id)
    return {"stage_id": stage_id.hex(), "raw_bytes": len(raw), "stored_bytes": len(blob), "scope": scope, "expires_at": expires}


def remember(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    scope = require_scope(args.scope)
    if args.type == "classical_conditioning" and not args.user_confirmed:
        raise SystemExit("classical_conditioning requires --user-confirmed")
    if not args.subject.strip() or not args.summary.strip():
        raise SystemExit("subject and summary are required")
    if not 0 <= args.importance <= 1 or not 0 <= args.confidence <= 1:
        raise SystemExit("importance and confidence must be between 0 and 1")

    for value in (args.subject, args.summary, args.detail or "", args.keywords, args.source, getattr(args, "confidence_reason", "")):
        check_text(value)
    detail = (args.detail or "").strip().encode()
    detail_digest = hashlib.sha256(detail).digest()
    payload = encode_payload(args.subject, args.summary, "", args.keywords, args.source)
    raw_payload = decompress(payload)
    metadata = bytes((MEMORY_TYPES[args.type], SENSITIVITY[args.sensitivity])) + scope.encode("utf-8")
    canonical = metadata + raw_payload
    fingerprint = hashlib.sha256(canonical + detail_digest).digest()
    memory_id = uuid.uuid4().bytes
    supersedes = parse_id(args.supersedes)
    created = now_utc()
    observed = timestamp(getattr(args, "observed_at", None)) or created
    valid_from = timestamp(getattr(args, "valid_from", None)) or observed
    valid_to = timestamp(getattr(args, "valid_to", None))
    if valid_to and valid_to <= valid_from:
        raise SystemExit("valid_to must be later than valid_from")
    expires = timestamp(args.expires)
    if not expires and args.type in {"episodic", "priming_conditioning", "classical_conditioning"}:
        expires = after_days(180 if args.type == "priming_conditioning" else 90)
    source_hash = getattr(args, "source_hash", None)
    if source_hash and not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise SystemExit("source hash must be a lowercase SHA-256 digest")
    claim_id = getattr(args, "claim_id", None) or uuid.uuid4().hex
    claim_id = parse_id(claim_id).hex()

    with transaction(conn):
        existing = conn.execute("SELECT * FROM cortex_memory WHERE scope=? AND content_fingerprint=? ORDER BY status=0 DESC,created_at DESC LIMIT 1", (scope, fingerprint)).fetchone()
        if supersedes:
            row = conn.execute("SELECT * FROM cortex_memory WHERE memory_id=?", (supersedes,)).fetchone()
            if not row:
                raise SystemExit("superseded memory not found")
            if row["scope"] != scope:
                raise SystemExit("superseded memory scope does not match requested scope")
            verify_memory_row(row)
            if row["status"] != 0 or (row["expires_at"] and row["expires_at"] <= created):
                raise SystemExit("correction requires the current active version")
            claim_id = row["claim_id"]
        if existing and not supersedes:
            verify_memory_row(existing)
            load_detail(conn, existing)
            if existing["status"] != 0 or (existing["expires_at"] and existing["expires_at"] <= created):
                raise SystemExit("matching memory is inactive; restore or correct it explicitly")
            requested = (round(args.importance * 255), round(args.confidence * 255), bool(args.pinned), expires if args.expires else existing["expires_at"])
            persisted = (existing["importance"], existing["confidence"], bool(existing["pinned"]), existing["expires_at"])
            if requested != persisted:
                raise SystemExit("duplicate metadata differs; use explicit retention or pin operations")
            for field in ("observed_at", "valid_from", "valid_to", "source_hash", "confidence_reason", "claim_id"):
                requested_value = getattr(args, field, None)
                if requested_value is not None and requested_value != "":
                    if field in {"observed_at", "valid_from", "valid_to"}:
                        requested_value = timestamp(requested_value)
                    if requested_value != existing[field]:
                        raise SystemExit("duplicate provenance or validity differs; use an explicit correction")
            if getattr(args, "stage_id", None) and parse_id(args.stage_id) != existing["stage_id"]:
                if existing["stage_id"]:
                    raise SystemExit("duplicate stage provenance differs; use an explicit correction")
                conn.execute("UPDATE cortex_memory SET stage_id=? WHERE memory_id=?", (parse_id(args.stage_id), existing["memory_id"]))
                seal_row(conn, "cortex_memory", "memory_id", existing["memory_id"])
            consolidate_stage(conn, getattr(args, "stage_id", None), scope)
            return {"memory_id": existing["memory_id"].hex(), "deduplicated": True, "status": "active", "verified": True}
        if conn.execute("SELECT 1 FROM cortex_memory WHERE scope=? AND claim_id=? AND status=0 AND memory_id!=?", (scope, claim_id, supersedes or b"")).fetchone():
            raise SystemExit("claim already has an active version; use its ID to correct it")
        if supersedes:
            conn.execute("UPDATE cortex_memory SET status=1,updated_at=? WHERE memory_id=?", (created, supersedes))
            seal_row(conn, "cortex_memory", "memory_id", supersedes)
        cursor = conn.execute(
            """INSERT INTO cortex_memory
            (memory_id,memory_type,scope,payload_blob,payload_raw_bytes,payload_stored_bytes,
             importance,confidence,sensitivity,created_at,updated_at,expires_at,pinned,
             supersedes_id,checksum_sha256)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (memory_id, MEMORY_TYPES[args.type], scope, payload, len(raw_payload), len(payload),
             round(args.importance * 255), round(args.confidence * 255), SENSITIVITY[args.sensitivity],
              created, created, expires, 1 if args.pinned else 0, supersedes,
             hashlib.sha256(canonical).digest()),
        )
        conn.execute("""UPDATE cortex_memory SET content_fingerprint=?,detail_sha256=?,observed_at=?,valid_from=?,valid_to=?,
            source_hash=?,confidence_reason=?,claim_id=?,prior_version_id=?,stage_id=? WHERE memory_id=?""",
            (fingerprint, detail_digest, observed, valid_from, valid_to, source_hash, getattr(args, "confidence_reason", ""),
             claim_id, supersedes, parse_id(getattr(args, "stage_id", None)), memory_id))
        conn.execute("INSERT INTO cortex_detail VALUES (?,?,?)", (memory_id, compress(detail), detail_digest))
        seal_row(conn, "cortex_memory", "memory_id", memory_id)
        index_memory(conn, memory_id)
        consolidate_stage(conn, getattr(args, "stage_id", None), scope)
    return {"memory_id": memory_id.hex(), "deduplicated": False, "supersedes_id": supersedes.hex() if supersedes else None}


def store_exact(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    scope = require_scope(args.scope)
    if not args.user_confirmed:
        raise SystemExit("full-fidelity storage requires --user-confirmed")
    check_text(args.source)
    check_text(args.media_type, maximum=256)
    original = check_exact(read_exact_input(args), args.media_type)
    if not original:
        raise SystemExit("nothing to store exactly")
    if args.retention == "expiring" and not args.expires:
        raise SystemExit("expiring full-fidelity memory requires --expires")
    expires = timestamp(args.expires)
    if args.retention == "until_user_deletes" and expires:
        raise SystemExit("until_user_deletes retention cannot have an expiry")
    digest = hashlib.sha256(original).digest()
    identity = scope.encode("utf-8") + b"\0" + args.media_type.encode("utf-8") + b"\0" + original
    archive_id = hashlib.blake2b(identity, digest_size=16, person=b"BMemExact").digest()
    blob = compress(original)
    created = now_utc()
    with transaction(conn):
        if conn.execute("SELECT 1 FROM cortex_tombstone WHERE memory_id=?", (archive_id,)).fetchone():
            raise SystemExit("exact identifier was purged and cannot be recreated")
        existing = conn.execute("SELECT * FROM cortex_verbatim WHERE archive_id=?", (archive_id,)).fetchone()
        if existing:
            verify_record(existing)
            persisted = decompress(existing["original_blob"])
            if hashlib.sha256(persisted).digest() != existing["content_sha256"] or persisted != original:
                raise SystemExit("exact duplicate integrity verification failed")
            if existing["status"] != 0 or (existing["expires_at"] and existing["expires_at"] <= created):
                raise SystemExit("matching exact memory is inactive; restore it explicitly")
            requested = (args.retention, expires, args.retention == "until_user_deletes" or args.pinned)
            if requested != (existing["retention"], existing["expires_at"], bool(existing["pinned"])):
                raise SystemExit("duplicate retention differs; change retention explicitly")
            if args.source != existing["source"]:
                raise SystemExit("duplicate exact provenance differs")
        if not existing:
            conn.execute(
                """INSERT INTO cortex_verbatim
                (archive_id,scope,media_type,original_blob,original_bytes,stored_bytes,
                 content_sha256,source,created_at,updated_at,retention,expires_at,pinned)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (archive_id, scope, args.media_type, blob, len(original), len(blob),
                  digest, args.source, created, created, args.retention, expires,
                 1 if args.retention == "until_user_deletes" or args.pinned else 0),
            )
            seal_row(conn, "cortex_verbatim", "archive_id", archive_id)
        linked_id = parse_id(getattr(args, "linked_memory_id", None))
        if linked_id:
            linked = conn.execute("SELECT * FROM cortex_memory WHERE memory_id=? AND scope=? AND status=0", (linked_id, scope)).fetchone()
            if not linked:
                raise SystemExit("active same-scope summary link required")
            verify_memory_row(linked)
            conn.execute("UPDATE cortex_verbatim SET linked_memory_id=? WHERE archive_id=?", (linked_id, archive_id))
            seal_row(conn, "cortex_verbatim", "archive_id", archive_id)
        row = conn.execute("SELECT * FROM cortex_verbatim WHERE archive_id=?", (archive_id,)).fetchone()
        verify_record(row)
        if hashlib.sha256(decompress(row["original_blob"])).digest() != row["content_sha256"]:
            raise SystemExit("exact storage verification failed")
    return {
        "archive_id": archive_id.hex(),
        "original_bytes": len(original),
        "content_sha256": digest.hex(),
        "scope": scope,
        "media_type": args.media_type,
        "stored_at": row["created_at"],
        "retention": row["retention"],
        "expires_at": row["expires_at"],
        "pinned": bool(row["pinned"]),
        "status": STATUS_NAMES[row["status"]],
        "linked_memory_id": row["linked_memory_id"].hex() if row["linked_memory_id"] else None,
        "encryption": "UNENCRYPTED_PROTOTYPE",
        "verified": True,
        "deduplicated": bool(existing),
    }


def recall_exact(conn: sqlite3.Connection, args: argparse.Namespace) -> bytes:
    archive_id = parse_id(args.archive_id)
    scope = require_scope(getattr(args, "scope", None))
    row = conn.execute(
        "SELECT * FROM cortex_verbatim WHERE archive_id=? AND scope=? AND status=0 AND (expires_at IS NULL OR expires_at>?)",
        (archive_id, scope, now_utc()),
    ).fetchone()
    if not row:
        raise SystemExit("active full-fidelity memory not found")
    verify_record(row)
    original = decompress(row["original_blob"])
    if hashlib.sha256(original).digest() != row["content_sha256"]:
        raise SystemExit("full-fidelity integrity verification failed")
    check_exact(original, row["media_type"])
    offset = bounded_integer(getattr(args, "offset", 0), "offset", 0, len(original))
    length = getattr(args, "length", None)
    if length is not None:
        bounded_integer(length, "length", 0, MAX_EXACT_BYTES)
    return original[offset:offset + length] if length is not None else original[offset:]


def recall(conn: sqlite3.Connection, args: argparse.Namespace, *, _allow_extensions=False) -> dict:
    if not _allow_extensions and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_item'").fetchone():
        if conn.execute("SELECT 1 FROM knowledge_item WHERE scope=? AND kind='source' LIMIT 1", (require_scope(args.scope),)).fetchone():
            raise SystemExit("use Knowledge.recall with the host source root to validate bound memories")
    with transaction(conn, write=False):
        return _recall(conn, args)


def _recall(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    scope = require_scope(args.scope)
    query_terms = tokenize(check_text(args.query, maximum=4096))
    if len(query_terms) > MAX_QUERY_TERMS:
        raise SystemExit("query contains too many terms")
    limit = bounded_integer(args.limit, "limit")
    state = conn.execute("SELECT * FROM vault_state WHERE singleton=1").fetchone()
    base = {"query": args.query, "scope": scope, "revision": state["revision"], "vault_id": state["vault_id"]}
    if not query_terms and not getattr(args, "browse", False):
        return {**base, "count": 0, "memories": [], "reason": "empty_or_unsupported_query", "candidate_limit_reached": False}
    where = ["m.status=0", "(m.expires_at IS NULL OR m.expires_at>?)",
             "(m.valid_from IS NULL OR m.valid_from<=?)", "(m.valid_to IS NULL OR m.valid_to>?)"]
    params: list[object] = [now_utc(), now_utc(), now_utc()]
    where.append("m.scope=?")
    params.append(scope)
    if args.type:
        where.append("m.memory_type=?")
        params.append(MEMORY_TYPES[args.type])

    if query_terms:
        placeholders = ",".join("?" for _ in query_terms)
        sql = f"""SELECT m.*,count(t.term_hash) AS hits
                  FROM cortex_memory m JOIN cortex_term t ON t.memory_pk=m.memory_pk
                  WHERE {' AND '.join(where)} AND t.term_hash IN ({placeholders})
                  GROUP BY m.memory_pk ORDER BY (0.60*count(t.term_hash)/?+0.25*m.importance/255.0+0.15*m.confidence/255.0) DESC,m.observed_at DESC,m.memory_id LIMIT ?"""
        params.extend(term_hash(token) for token in sorted(query_terms))
        params.append(len(query_terms))
    else:
        sql = f"""SELECT m.*,0 AS hits FROM cortex_memory m
                  WHERE {' AND '.join(where)} ORDER BY (0.25*m.importance/255.0+0.15*m.confidence/255.0) DESC,m.observed_at DESC,m.memory_id LIMIT ?"""
    params.append(limit + 1)
    rows = conn.execute(sql, params).fetchall()

    memories = []
    for row in rows[:limit]:
        payload = verify_memory_row(row)
        relevance = row["hits"] / max(len(query_terms), 1)
        score = 0.60 * relevance + 0.25 * (row["importance"] / 255) + 0.15 * (row["confidence"] / 255)
        item = {
            "memory_id": row["memory_id"].hex(),
            "type": TYPE_NAMES[row["memory_type"]],
            "scope": row["scope"],
            "subject": payload["subject"],
            "summary": payload["summary"],
            "importance": round(row["importance"] / 255, 3),
            "confidence": round(row["confidence"] / 255, 3),
            "sensitivity": SENSITIVITY_NAMES[row["sensitivity"]],
            "source": payload["source"],
            "updated_at": row["updated_at"],
            "observed_at": row["observed_at"],
            "valid_from": row["valid_from"] if row["valid_from"] != row["observed_at"] else None,
            "valid_to": row["valid_to"],
            "source_hash": row["source_hash"],
            "confidence_reason": row["confidence_reason"],
            "score": round(score, 4),
        }
        if args.include_detail:
            item["detail"] = load_detail(conn, row)
        memories.append(item)
    return {**base, "count": len(memories), "memories": memories, "candidate_limit_reached": len(rows) > limit}


def render_prompt_packet(result: dict, max_chars: int, include_ids: bool) -> str:
    return render_packet(result, max_chars=max_chars, include_ids=include_ids)["text"]


def lifecycle(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    memory_id = parse_id(args.memory_id)
    scope = require_scope(getattr(args, "scope", None))
    status_by_action = {"restore": 0, "unarchive": 0, "forget": 2, "archive": 4, "pin": 0, "unpin": 0, "retention": 0}
    allowed_from = {"restore": (2, 3), "unarchive": (4,), "forget": (0, 1, 3, 4), "archive": (0,),
                    "pin": (0, 4), "unpin": (0, 4), "retention": (0, 2, 3, 4)}
    status = status_by_action[args.action]
    with transaction(conn):
        matches = [(table, key, row) for table, key in (("cortex_memory", "memory_id"), ("cortex_verbatim", "archive_id"))
                   if (row := conn.execute(f"SELECT * FROM {table} WHERE {key}=? AND scope=?", (memory_id, scope)).fetchone())]
        if len(matches) != 1 or matches[0][2]["status"] not in allowed_from[args.action]:
            raise SystemExit("memory not found or lifecycle transition is not allowed")
        table, key, row = matches[0]
        verify_record(row)
        expiry = row["expires_at"]
        if args.action in {"restore", "unarchive"}:
            if table == "cortex_memory":
                verify_memory_row(row)
                load_detail(conn, row)
                if conn.execute("SELECT 1 FROM cortex_memory WHERE scope=? AND claim_id=? AND status=0 AND memory_id!=?", (scope, row["claim_id"], memory_id)).fetchone():
                    raise SystemExit("restoration conflicts with an active version")
                if row["valid_to"] and row["valid_to"] <= now_utc():
                    raise SystemExit("historical validity has ended; create a corrected version")
            else:
                original = decompress(row["original_blob"])
                if hashlib.sha256(original).digest() != row["content_sha256"]:
                    raise SystemExit("exact integrity verification failed")
                check_exact(original, row["media_type"])
            if expiry and expiry <= now_utc():
                if not getattr(args, "renew", False):
                    raise SystemExit("elapsed retention requires an explicit --renew expiry")
                expiry = timestamp(args.renew, optional=False)
                if expiry <= now_utc():
                    raise SystemExit("renewal expiry must be in the future")
        if args.action in {"pin", "unpin", "retention"}:
            status = row["status"]
        if args.action == "retention":
            expiry = timestamp(args.expires)
            if expiry and expiry <= now_utc():
                raise SystemExit("new retention expiry must be in the future")
        conn.execute(f"UPDATE {table} SET status=?,updated_at=?,expires_at=? WHERE {key}=? AND scope=?",
                     (status, now_utc(), expiry, memory_id, scope))
        if args.action in {"pin", "unpin"}:
            conn.execute(f"UPDATE {table} SET pinned=? WHERE {key}=?", (int(args.action == "pin"), memory_id))
        if table == "cortex_verbatim":
            conn.execute("UPDATE cortex_verbatim SET retention=? WHERE archive_id=?", ("expiring" if expiry else "until_user_deletes", memory_id))
        seal_row(conn, table, key, memory_id)
    return {"memory_id": args.memory_id, "status": STATUS_NAMES[status]}


def list_memories(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    scope = require_scope(args.scope)
    kind = getattr(args, "kind", "semantic")
    names = STAGE_STATUS_NAMES if kind == "stage" else STATUS_NAMES
    clauses = ["scope=?"]
    params: list[object] = [scope]
    if args.status:
        if args.status not in names.values():
            raise SystemExit("status is not supported for this record kind")
        status_value = next(value for value, name in names.items() if name == args.status)
        clauses.append("status=?")
        params.append(status_value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit = bounded_integer(args.limit, "limit")
    params.extend((limit + 1, bounded_integer(getattr(args, "offset", 0), "offset", 0, 1000000)))
    table, key, sort = {"semantic": ("cortex_memory", "memory_id", "updated_at"), "exact": ("cortex_verbatim", "archive_id", "updated_at"),
                        "stage": ("hippocampus_stage", "stage_id", "created_at")}[kind]
    rows = conn.execute(f"SELECT * FROM {table} {where} ORDER BY {sort} DESC,{key} LIMIT ? OFFSET ?", params).fetchall()
    memories = []
    for row in rows[:limit]:
        verify_record(row)
        if kind != "semantic":
            if kind == "exact":
                raw = decompress(row["original_blob"])
                if hashlib.sha256(raw).digest() != row["content_sha256"]:
                    raise SystemExit("exact integrity verification failed")
                check_exact(raw, row["media_type"])
            else:
                raw = decompress(row["raw_blob"])
                if hashlib.sha256(raw).digest() != row["checksum_sha256"]:
                    raise SystemExit("stage integrity verification failed")
                check_text(raw.decode())
            memories.append({"memory_id": row[key].hex(), "kind": kind, "scope": scope, "source": row["source"],
                             "expires_at": row["expires_at"], "status": names[row["status"]],
                             "bytes": row["original_bytes"] if kind == "exact" else row["raw_bytes"]})
            continue
        payload = verify_memory_row(row)
        memories.append({
            "memory_id": row["memory_id"].hex(),
            "type": TYPE_NAMES[row["memory_type"]],
            "scope": row["scope"],
            "subject": payload["subject"],
            "summary": payload["summary"],
            "status": STATUS_NAMES[row["status"]],
            "pinned": bool(row["pinned"]),
            "updated_at": row["updated_at"],
            "observed_at": row["observed_at"],
            "prior_version_id": row["prior_version_id"].hex() if row["prior_version_id"] else None,
        })
    return {"scope": scope, "kind": kind, "count": len(memories), "memories": memories, "has_more": len(rows) > limit}


def prune(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    scope = require_scope(getattr(args, "scope", None))
    if not 0 <= args.importance_below <= 1:
        raise SystemExit("importance threshold must be between 0 and 1")
    if args.apply and not args.user_confirmed:
        raise SystemExit("applying a prune requires --user-confirmed")
    reviewed_ids = getattr(args, "reviewed_ids", None)
    if args.apply and not reviewed_ids:
        raise SystemExit("applying a prune requires reviewed candidate IDs")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, args.older_than_days))).isoformat(timespec="seconds")
    limit = bounded_integer(args.limit, "limit")
    candidates = []
    with transaction(conn) if args.apply else contextlib.nullcontext():
        if reviewed_ids:
            if len(reviewed_ids) > limit or len(set(reviewed_ids)) != len(reviewed_ids):
                raise SystemExit("review set is duplicated or exceeds the limit")
            rows = []
            for token in reviewed_ids:
                identifier, separator, digest = token.partition(":")
                if not separator or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise SystemExit("reviewed IDs must include the preview checksum: ID:SHA256")
                row = conn.execute("SELECT * FROM cortex_memory WHERE memory_id=? AND scope=?", (parse_id(identifier), scope)).fetchone()
                if not row or row["record_checksum"].hex() != digest:
                    raise SystemExit("reviewed record changed or is outside the requested scope")
                rows.append(row)
        else:
            rows = conn.execute("""SELECT * FROM cortex_memory WHERE scope=? AND status=0 AND pinned=0
                AND memory_type!=? AND created_at<=? AND importance<=? ORDER BY importance,created_at,memory_id LIMIT ?""",
                (scope, MEMORY_TYPES["procedural"], cutoff, round(args.importance_below * 255), limit)).fetchall()
        for row in rows:
            payload = verify_memory_row(row)
            if row["status"] != 0 or row["pinned"] or row["memory_type"] == MEMORY_TYPES["procedural"] or row["created_at"] > cutoff or row["importance"] > round(args.importance_below * 255):
                raise SystemExit("reviewed prune candidate no longer matches pruning criteria")
            candidates.append({"memory_id": row["memory_id"].hex(), "review_token": row["memory_id"].hex() + ":" + row["record_checksum"].hex(),
                "scope": scope, "type": TYPE_NAMES[row["memory_type"]], "summary": payload["summary"],
                "importance": round(row["importance"] / 255, 3), "created_at": row["created_at"], "proposed_action": "archive"})
            if args.apply:
                changed = conn.execute("UPDATE cortex_memory SET status=4,updated_at=? WHERE memory_id=? AND scope=? AND status=0 AND pinned=0 AND record_checksum=?",
                    (now_utc(), row["memory_id"], scope, row["record_checksum"]))
                if changed.rowcount != 1:
                    raise SystemExit("prune candidate changed during application")
                seal_row(conn, "cortex_memory", "memory_id", row["memory_id"])
    return {"mode": "applied" if args.apply else "preview", "count": len(candidates), "candidates": candidates}


def purge(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    if not args.user_confirmed:
        raise SystemExit("permanent purge requires --user-confirmed")
    memory_id = parse_id(args.memory_id)
    scope = require_scope(getattr(args, "scope", None))
    with transaction(conn):
        matches = [(table, key, row) for table, key in (("cortex_memory", "memory_id"), ("cortex_verbatim", "archive_id"), ("hippocampus_stage", "stage_id"))
                   if (row := conn.execute(f"SELECT * FROM {table} WHERE {key}=? AND scope=?", (memory_id, scope)).fetchone())]
        if len(matches) != 1:
            raise SystemExit("memory not found or identifier was ambiguous")
        table, key, row = matches[0]
        verify_record(row)
        if table == "cortex_memory":
            for dependent_table, dependent_key, field in (("cortex_memory", "memory_id", "supersedes_id"), ("cortex_verbatim", "archive_id", "linked_memory_id")):
                dependents = conn.execute(f"SELECT * FROM {dependent_table} WHERE {field}=?", (memory_id,)).fetchall()
                for child in dependents:
                    verify_record(child)
                    if child["scope"] != scope:
                        raise SystemExit("cross-scope dependency requires repair before purge")
                    conn.execute(f"UPDATE {dependent_table} SET {field}=NULL WHERE {dependent_key}=?", (child[dependent_key],))
                    seal_row(conn, dependent_table, dependent_key, child[dependent_key])
        conn.execute("INSERT OR REPLACE INTO cortex_tombstone VALUES (?,?,?)", (memory_id, scope, now_utc()))
        removed = conn.execute(f"DELETE FROM {table} WHERE {key}=? AND scope=?", (memory_id, scope)).rowcount
        if removed != 1:
            raise SystemExit("purge did not remove exactly one record")
    checkpoint = None if conn.in_transaction else tuple(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
    return {
        "memory_id": args.memory_id,
        "status": "purged",
        "records_removed": removed,
        "checkpoint": checkpoint,
        "prototype_notice": "No backup or cryptographic-erasure guarantee applies to this unencrypted prototype.",
    }


def expire(conn: sqlite3.Connection, args=None) -> dict:
    scope = require_scope(getattr(args, "scope", None))
    now = now_utc()
    counts = []
    with transaction(conn):
        for table, key in (("hippocampus_stage", "stage_id"), ("cortex_memory", "memory_id"), ("cortex_verbatim", "archive_id")):
            rows = conn.execute(f"SELECT * FROM {table} WHERE scope=? AND status=0 AND expires_at IS NOT NULL AND expires_at<=?", (scope, now)).fetchall()
            for row in rows:
                verify_record(row)
                if table == "hippocampus_stage":
                    dispose_stage(conn, row[key], 2)
                else:
                    conn.execute(f"UPDATE {table} SET status=3,updated_at=? WHERE {key}=?", (now, row[key]))
                    seal_row(conn, table, key, row[key])
            counts.append(len(rows))
    return dict(zip(("hippocampus_expired", "cortex_expired", "verbatim_expired"), counts))


def stats(conn: sqlite3.Connection, args=None) -> dict:
    scope = require_scope(getattr(args, "scope", None))
    staged = conn.execute("SELECT count(*) n,coalesce(sum(raw_bytes),0) raw,coalesce(sum(stored_bytes),0) stored FROM hippocampus_stage WHERE scope=?", (scope,)).fetchone()
    cortex = conn.execute("SELECT count(*) n,coalesce(sum(payload_raw_bytes),0) raw,coalesce(sum(payload_stored_bytes),0) stored FROM cortex_memory WHERE scope=?", (scope,)).fetchone()
    verbatim = conn.execute("SELECT count(*) n,coalesce(sum(original_bytes),0) raw,coalesce(sum(stored_bytes),0) stored FROM cortex_verbatim WHERE scope=?", (scope,)).fetchone()
    raw = staged["raw"] + cortex["raw"] + verbatim["raw"]
    stored = staged["stored"] + cortex["stored"] + verbatim["stored"]
    for row in conn.execute("SELECT d.* FROM cortex_detail d JOIN cortex_memory m USING(memory_id) WHERE m.scope=?", (scope,)):
        raw += len(decompress(row["detail_blob"]))
        stored += len(row["detail_blob"])
    return {
        "scope": scope,
        "hippocampus_records": staged["n"],
        "cortex_records": cortex["n"],
        "full_fidelity_records": verbatim["n"],
        "logical_payload_bytes": raw,
        "stored_payload_bytes": stored,
        "binary_compression_ratio": round(raw / stored, 3) if stored else None,
        "whole_database_allocated_bytes": conn.execute("PRAGMA page_count").fetchone()[0] * conn.execute("PRAGMA page_size").fetchone()[0],
        "whole_database_free_pages": conn.execute("PRAGMA freelist_count").fetchone()[0],
        "scope_term_rows": conn.execute("SELECT count(*) FROM cortex_term t JOIN cortex_memory m USING(memory_pk) WHERE m.scope=?", (scope,)).fetchone()[0],
        "token_savings_measured": False,
    }


PORTABLE_TABLES = ("cortex_memory", "cortex_detail", "cortex_verbatim", "hippocampus_stage", "cortex_tombstone")


def encode_row(row):
    return {k: {"base64": base64.b64encode(row[k]).decode()} if isinstance(row[k], bytes) else row[k]
            for k in row.keys() if k != "memory_pk"}


def validate_links(conn, *, migrating=False):
    if conn.execute("PRAGMA foreign_key_check").fetchone():
        raise SystemExit("unresolved database references")
    rows = {r["memory_id"]: r for r in conn.execute("SELECT * FROM cortex_memory")}
    roots = {}
    for identifier, row in rows.items():
        trail, current = set(), identifier
        while current not in roots:
            if current in trail:
                raise SystemExit("cyclic correction history")
            trail.add(current)
            parent = rows[current]["supersedes_id"]
            if parent is None:
                roots[current] = current
                break
            if parent not in rows or rows[parent]["scope"] != row["scope"]:
                raise SystemExit("unresolved or cross-scope correction")
            if not migrating and rows[current]["claim_id"] != rows[parent]["claim_id"]:
                raise SystemExit("correction claim identity disagrees")
            current = parent
        for child in trail:
            roots[child] = roots[current]
    active = set()
    for identifier, row in rows.items():
        claim = (row["scope"], roots[identifier] if migrating else row["claim_id"])
        if row["status"] == 0:
            if claim in active:
                raise SystemExit("multiple active versions of a claim")
            active.add(claim)
        if not migrating:
            prior = row["prior_version_id"]
            if row["supersedes_id"] and prior != row["supersedes_id"]:
                raise SystemExit("correction history disagrees")
            if prior and not row["supersedes_id"]:
                if not conn.execute("SELECT 1 FROM cortex_tombstone WHERE memory_id=? AND scope=?", (prior, row["scope"])).fetchone():
                    raise SystemExit("missing correction tombstone")
            if row["stage_id"]:
                stage_row = conn.execute("SELECT scope,status FROM hippocampus_stage WHERE stage_id=?", (row["stage_id"],)).fetchone()
                tombstone = conn.execute("SELECT 1 FROM cortex_tombstone WHERE memory_id=? AND scope=?", (row["stage_id"], row["scope"])).fetchone()
                if (stage_row and (stage_row["scope"] != row["scope"] or stage_row["status"] != 1)) or (not stage_row and not tombstone):
                    raise SystemExit("invalid stage provenance link")
    for row in conn.execute("SELECT v.scope child,m.scope parent FROM cortex_verbatim v JOIN cortex_memory m ON v.linked_memory_id=m.memory_id"):
        if row["child"] != row["parent"]:
            raise SystemExit("cross-scope exact link")
    return roots


def verify_scope(conn, scope):
    require_scope(scope)
    validate_links(conn)
    for table in ("cortex_memory", "cortex_verbatim", "hippocampus_stage", "cortex_tombstone"):
        for row in conn.execute(f"SELECT * FROM {table} WHERE scope=?", (scope,)):
            for field in ("created_at", "updated_at", "expires_at", "observed_at", "valid_from", "valid_to", "removed_at"):
                if field in row.keys():
                    required = field in {"created_at", "updated_at", "observed_at", "valid_from", "removed_at"}
                    if timestamp(row[field], optional=not required) != row[field]:
                        raise SystemExit("stored timestamp is not canonical UTC")
            if "status" in row.keys() and row["status"] not in (range(3) if table == "hippocampus_stage" else STATUS_NAMES):
                raise SystemExit("invalid lifecycle status")
            if "pinned" in row.keys() and row["pinned"] not in (0, 1):
                raise SystemExit("invalid pin state")
            if table == "cortex_tombstone" and (not isinstance(row["memory_id"], bytes) or len(row["memory_id"]) != 16):
                raise SystemExit("invalid tombstone identity")
    for row in conn.execute("SELECT * FROM cortex_memory WHERE scope=?", (scope,)):
        if any(type(row[field]) is not int or not 0 <= row[field] <= 255 for field in ("importance", "confidence")):
            raise SystemExit("quality metadata must use integer byte values")
        payload = verify_memory_row(row)
        detail = load_detail(conn, row)
        raw = decompress(row["payload_blob"])
        canonical = bytes((row["memory_type"], row["sensitivity"])) + scope.encode() + raw
        if row["memory_type"] not in TYPE_NAMES or row["sensitivity"] not in SENSITIVITY_NAMES or payload["detail"]:
            raise SystemExit("invalid semantic type or payload layout")
        if row["content_fingerprint"] != hashlib.sha256(canonical + hashlib.sha256(detail.encode()).digest()).digest():
            raise SystemExit("semantic fingerprint disagrees")
        if row["payload_raw_bytes"] != len(raw) or row["payload_stored_bytes"] != len(row["payload_blob"]):
            raise SystemExit("semantic byte counts disagree")
        if row["valid_to"] and row["valid_to"] <= row["valid_from"]:
            raise SystemExit("invalid validity interval")
        if row["source_hash"] is not None and (not isinstance(row["source_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["source_hash"])):
            raise SystemExit("invalid source hash")
        for field in ("scope", "confidence_reason"):
            check_text(row[field])
        if not isinstance(row["claim_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", row["claim_id"]):
            raise SystemExit("invalid claim identity")
    for row in conn.execute("SELECT * FROM cortex_verbatim WHERE scope=?", (scope,)):
        verify_record(row)
        raw = decompress(row["original_blob"])
        if hashlib.sha256(raw).digest() != row["content_sha256"]:
            raise SystemExit("exact integrity verification failed")
        check_exact(raw, row["media_type"])
        check_text(row["source"])
        if row["original_bytes"] != len(raw) or row["stored_bytes"] != len(row["original_blob"]):
            raise SystemExit("exact byte counts disagree")
        if row["retention"] not in {"until_user_deletes", "expiring"} or (row["retention"] == "expiring") != bool(row["expires_at"]):
            raise SystemExit("exact retention disagrees with expiry")
    for row in conn.execute("SELECT * FROM hippocampus_stage WHERE scope=?", (scope,)):
        verify_record(row)
        raw = decompress(row["raw_blob"])
        if hashlib.sha256(raw).digest() != row["checksum_sha256"]:
            raise SystemExit("stage integrity verification failed")
        check_text(raw.decode())
        check_text(row["source"])
        if not row["expires_at"] or row["raw_bytes"] != len(raw) or row["stored_bytes"] != len(row["raw_blob"]):
            raise SystemExit("stage expiry or byte counts disagree")
        if row["status"] != 0 and (raw or row["raw_blob"]):
            raise SystemExit("disposed stage retains raw content")


def export_scope(conn, scope, *, _allow_extensions=False):
    scope = require_scope(scope)
    if not _allow_extensions and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_route'").fetchone():
        if conn.execute("SELECT 1 FROM session_route WHERE scope=? LIMIT 1", (scope,)).fetchone():
            raise SystemExit("use RouteOutbox.export to retain pending messages and transition receipts")
    if not _allow_extensions and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_item'").fetchone():
        if conn.execute("SELECT 1 FROM knowledge_item WHERE scope=? LIMIT 1", (scope,)).fetchone():
            raise SystemExit("use Knowledge.export to preserve source bindings, proposals and relations")
    with transaction(conn, write=False):
        verify_scope(conn, scope)
        tables = {}
        for table in PORTABLE_TABLES:
            if table == "cortex_detail":
                rows = conn.execute("SELECT d.* FROM cortex_detail d JOIN cortex_memory m USING(memory_id) WHERE m.scope=? ORDER BY d.memory_id", (scope,))
            else:
                key = {"cortex_memory": "memory_id", "cortex_verbatim": "archive_id", "hippocampus_stage": "stage_id", "cortex_tombstone": "memory_id"}[table]
                rows = conn.execute(f"SELECT * FROM {table} WHERE scope=? ORDER BY {key}", (scope,))
            tables[table] = [encode_row(row) for row in rows]
        package = {"format": "memorycore-ai-scope/2", "scope": scope, "tables": tables}
        canonical = json.dumps(package, sort_keys=True, separators=(",", ":")).encode()
        if len(canonical) > 16 * MAX_TEXT_BYTES:
            raise SystemExit("scope export exceeds the supported 16 MiB limit")
        package["sha256"] = hashlib.sha256(canonical).hexdigest()
        return package


def import_scope(conn, package, scope, *, user_confirmed=False):
    validate_import(package, scope, user_confirmed=user_confirmed)
    return _import_scope(conn, package, scope, user_confirmed=user_confirmed)


def validate_import(package, scope, *, user_confirmed=False):
    # Validate the complete package in volatile SQLite before any target write.
    staging = sqlite3.connect(":memory:")
    staging.row_factory = sqlite3.Row
    staging.execute("PRAGMA foreign_keys=ON")
    try:
        initialize(staging)
        _import_scope(staging, package, scope, user_confirmed=user_confirmed)
    finally:
        staging.close()


def _import_scope(conn, package, scope, *, user_confirmed=False):
    scope = require_scope(scope)
    if not user_confirmed:
        raise SystemExit("scope import requires explicit confirmation")
    if not isinstance(package, dict) or set(package) != {"format", "scope", "tables", "sha256"} or package["format"] != "memorycore-ai-scope/2" or package["scope"] != scope:
        raise SystemExit("unsupported export or scope mismatch")
    canonical = json.dumps({k: v for k, v in package.items() if k != "sha256"}, sort_keys=True, separators=(",", ":")).encode()
    if len(canonical) > 16 * MAX_TEXT_BYTES or hashlib.sha256(canonical).hexdigest() != package["sha256"]:
        raise SystemExit("scope export integrity verification failed")
    if not isinstance(package["tables"], dict) or set(package["tables"]) != set(PORTABLE_TABLES):
        raise SystemExit("unsupported export tables")
    with transaction(conn):
        conn.execute("PRAGMA defer_foreign_keys=ON")
        for table in PORTABLE_TABLES:
            allowed = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")} - {"memory_pk"}
            for encoded in package["tables"][table]:
                if not isinstance(encoded, dict) or set(encoded) != allowed:
                    raise SystemExit("unsupported export row")
                row = {}
                for key, value in encoded.items():
                    if isinstance(value, dict):
                        if set(value) != {"base64"}:
                            raise SystemExit("invalid binary export field")
                        try:
                            value = base64.b64decode(value["base64"], validate=True)
                        except (ValueError, TypeError):
                            raise SystemExit("invalid binary export field") from None
                    row[key] = value
                if table != "cortex_detail" and row["scope"] != scope:
                    raise SystemExit("cross-scope import rejected")
                if table == "cortex_detail":
                    owner = conn.execute("SELECT scope FROM cortex_memory WHERE memory_id=?", (row["memory_id"],)).fetchone()
                    if not owner or owner["scope"] != scope:
                        raise SystemExit("cross-scope detail rejected")
                identifier = row.get("memory_id", row.get("archive_id", row.get("stage_id")))
                if table != "cortex_tombstone" and conn.execute("SELECT 1 FROM cortex_tombstone WHERE memory_id=?", (identifier,)).fetchone():
                    raise SystemExit("import would resurrect a purged identifier")
                fields = list(row)
                conn.execute(f"INSERT INTO {table} ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", [row[k] for k in fields])
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise SystemExit("import contains unresolved references")
        for table, key in (("cortex_memory", "memory_id"), ("cortex_verbatim", "archive_id"), ("hippocampus_stage", "stage_id")):
            if conn.execute(f"SELECT 1 FROM {table} r JOIN cortex_tombstone t ON r.{key}=t.memory_id").fetchone():
                raise SystemExit("import includes a purged identifier")
        for row in conn.execute("SELECT m.scope child,p.scope parent FROM cortex_memory m JOIN cortex_memory p ON m.supersedes_id=p.memory_id UNION ALL SELECT v.scope,m.scope FROM cortex_verbatim v JOIN cortex_memory m ON v.linked_memory_id=m.memory_id"):
            if row["child"] != row["parent"]:
                raise SystemExit("cross-scope link rejected")
        verify_scope(conn, scope)
        for row in conn.execute("SELECT memory_id FROM cortex_memory WHERE scope=?", (scope,)).fetchall():
            index_memory(conn, row[0])
        if conn.execute("SELECT 1 FROM cortex_memory WHERE scope=? AND status=0 GROUP BY claim_id HAVING count(*)>1", (scope,)).fetchone():
            raise SystemExit("import has multiple active versions of a claim")
    return {"scope": scope, "imported": True, "sha256": package["sha256"]}


def inspect_memory(conn, identifier, scope):
    scope = require_scope(scope)
    key = parse_id(identifier)
    for table, field in (("cortex_memory", "memory_id"), ("cortex_verbatim", "archive_id"), ("hippocampus_stage", "stage_id")):
        row = conn.execute(f"SELECT * FROM {table} WHERE {field}=? AND scope=?", (key, scope)).fetchone()
        if row:
            verify_scope(conn, scope)
            item = encode_row(row)
            for content in ("payload_blob", "original_blob", "raw_blob"):
                item.pop(content, None)
            if table == "cortex_memory":
                item.update(verify_memory_row(row))
                item["detail"] = load_detail(conn, row)
            return {"kind": table, "record": item}
    raise SystemExit("scoped memory not found")


def capabilities():
    return {"version": "0.10.0-dev", "schema": SCHEMA_VERSION, "synthetic_only": True,
            "new_database_layout": "scoped-composite-index-and-compact-tombstones",
            "prototype_encryption": False, "production_approved": False, "activated": False,
            "exact_formats": ["text/plain", "text/markdown", "application/json"],
            "operations": ["stage", "remember", "consolidate", "recall", "store-exact", "recall-exact", "list", "inspect", "export", "import", "pin", "unpin", "retention", "archive", "unarchive", "forget", "restore", "prune", "expire", "purge", "stats", "migrate"],
            "development_extensions": ["knowledge_layer", "code_context", "synthetic stdio MCP", "session_routing", "fresh_context", "routing_calibration"],
            "routing_workbench": "python -B -m scripts.routing_cli; explicit synthetic paths required",
            "not_implemented": ["production MCP", "automatic chat ingestion", "automatic desktop dispatch", "attachment forwarding", "contacts", "trained embeddings", "sync", "production broker service"],
            "tokenizer": "optional tiktoken; required for recall packet rendering"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MemoryCore AI binary store")
    parser.add_argument("--db", help="prototype database path; defaults to the operating-system MemoryCore AI home")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("migrate")
    commands.add_parser("capabilities")

    stage_parser = commands.add_parser("stage")
    source = stage_parser.add_mutually_exclusive_group()
    source.add_argument("--text")
    source.add_argument("--file")
    stage_parser.add_argument("--scope", required=True)
    stage_parser.add_argument("--source", default="chat")
    stage_parser.add_argument("--expires")

    def add_memory_fields(command: argparse.ArgumentParser) -> None:
        command.add_argument("--type", required=True, choices=sorted(MEMORY_TYPES))
        command.add_argument("--scope", required=True)
        command.add_argument("--subject", required=True)
        command.add_argument("--summary", required=True)
        command.add_argument("--detail", default="")
        command.add_argument("--keywords", default="")
        command.add_argument("--importance", type=float, default=0.5)
        command.add_argument("--confidence", type=float, default=1.0)
        command.add_argument("--sensitivity", choices=sorted(SENSITIVITY), default="internal")
        command.add_argument("--source", default="user")
        command.add_argument("--expires")
        command.add_argument("--supersedes")
        command.add_argument("--pinned", action="store_true")
        command.add_argument("--user-confirmed", action="store_true")
        command.add_argument("--observed-at")
        command.add_argument("--valid-from")
        command.add_argument("--valid-to")
        command.add_argument("--source-hash")
        command.add_argument("--confidence-reason", default="")
        command.add_argument("--claim-id")

    remember_parser = commands.add_parser("remember")
    add_memory_fields(remember_parser)
    consolidate_parser = commands.add_parser("consolidate")
    add_memory_fields(consolidate_parser)
    consolidate_parser.add_argument("--stage-id", required=True)

    exact_parser = commands.add_parser("store-exact")
    exact_source = exact_parser.add_mutually_exclusive_group()
    exact_source.add_argument("--text")
    exact_source.add_argument("--file")
    exact_parser.add_argument("--scope", required=True)
    exact_parser.add_argument("--source", default="user")
    exact_parser.add_argument("--media-type", default="text/plain; charset=utf-8")
    exact_parser.add_argument("--retention", choices=("until_user_deletes", "expiring"), default="until_user_deletes")
    exact_parser.add_argument("--expires")
    exact_parser.add_argument("--pinned", action="store_true")
    exact_parser.add_argument("--user-confirmed", action="store_true")
    exact_parser.add_argument("--linked-memory-id")

    exact_recall_parser = commands.add_parser("recall-exact")
    exact_recall_parser.add_argument("archive_id")
    exact_recall_parser.add_argument("--scope", required=True)
    exact_recall_parser.add_argument("--offset", type=int, default=0)
    exact_recall_parser.add_argument("--length", type=int)

    recall_parser = commands.add_parser("recall")
    recall_parser.add_argument("query")
    recall_parser.add_argument("--scope", required=True)
    recall_parser.add_argument("--type", choices=sorted(MEMORY_TYPES))
    recall_parser.add_argument("--limit", type=int, default=8)
    recall_parser.add_argument("--include-detail", action="store_true")
    recall_parser.add_argument("--format", choices=("prompt", "json"), default="prompt")
    recall_parser.add_argument("--max-chars", type=int, default=2500)
    recall_parser.add_argument("--include-ids", action="store_true")
    recall_parser.add_argument("--browse", action="store_true")
    recall_parser.add_argument("--max-tokens", type=int, default=700)
    recall_parser.add_argument("--reserve-tokens", type=int, default=32)
    recall_parser.add_argument("--encoding", choices=("o200k_base", "cl100k_base"), default="o200k_base")

    for action in ("forget", "restore", "archive", "unarchive", "pin", "unpin", "retention"):
        command = commands.add_parser(action)
        command.add_argument("memory_id")
        command.add_argument("--scope", required=True)
        command.set_defaults(action=action)
        if action in {"restore", "unarchive"}:
            command.add_argument("--renew", help="explicit future expiry for elapsed retention")
        if action == "retention":
            expiry = command.add_mutually_exclusive_group(required=True)
            expiry.add_argument("--expires")
            expiry.add_argument("--until-user-deletes", action="store_true")

    list_parser = commands.add_parser("list")
    list_parser.add_argument("--scope", required=True)
    list_parser.add_argument("--kind", choices=("semantic", "exact", "stage"), default="semantic")
    list_parser.add_argument("--offset", type=int, default=0)
    list_parser.add_argument("--status", choices=tuple(STATUS_NAMES.values()) + ("consolidated",))
    list_parser.add_argument("--limit", type=int, default=100)

    prune_parser = commands.add_parser("prune")
    prune_parser.add_argument("--scope", required=True)
    prune_parser.add_argument("--older-than-days", type=int, default=180)
    prune_parser.add_argument("--importance-below", type=float, default=0.25)
    prune_parser.add_argument("--limit", type=int, default=100)
    prune_parser.add_argument("--reviewed-id", dest="reviewed_ids", action="append")
    prune_parser.add_argument("--apply", action="store_true")
    prune_parser.add_argument("--user-confirmed", action="store_true")

    purge_parser = commands.add_parser("purge")
    purge_parser.add_argument("memory_id")
    purge_parser.add_argument("--scope", required=True)
    purge_parser.add_argument("--user-confirmed", action="store_true")
    commands.add_parser("expire").add_argument("--scope", required=True)
    commands.add_parser("stats").add_argument("--scope", required=True)
    inspect_parser = commands.add_parser("inspect")
    inspect_parser.add_argument("memory_id")
    inspect_parser.add_argument("--scope", required=True)
    export_parser = commands.add_parser("export")
    export_parser.add_argument("--scope", required=True)
    export_parser.add_argument("--output", required=True)
    import_parser = commands.add_parser("import")
    import_parser.add_argument("--scope", required=True)
    import_parser.add_argument("--file", required=True)
    import_parser.add_argument("--user-confirmed", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "capabilities":
        print(json.dumps(capabilities(), separators=(",", ":")))
        return
    if args.command == "import":
        with Path(args.file).open("rb") as stream:
            args.validated_package = json.loads(read_bounded(stream, 16 * MAX_TEXT_BYTES))
        validate_import(args.validated_package, args.scope, user_confirmed=args.user_confirmed)
    db_path = Path(args.db).expanduser().resolve() if args.db else default_db_path().resolve()
    read_only = args.command in {"recall", "recall-exact", "list", "inspect", "stats", "export"} or (args.command == "prune" and not args.apply)
    existed = db_path.exists()
    conn = connect(str(db_path), read_only=read_only)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if args.command in {"init", "migrate"} or not existed:
            initialize(conn)
        elif version != SCHEMA_VERSION:
            raise SystemExit("database schema requires an explicit migrate command")
        dispatch(conn, args, db_path)
    finally:
        conn.close()


def dispatch(conn, args, db_path):
    if args.command == "recall-exact":
        sys.stdout.buffer.write(recall_exact(conn, args))
        return
    if args.command in {"init", "migrate"}:
        result = {"database": str(db_path), "initialized": True, "wire_format": "CMN/1", "schema": SCHEMA_VERSION}
    elif args.command == "stage":
        result = stage(conn, args)
    elif args.command in {"remember", "consolidate"}:
        result = remember(conn, args)
    elif args.command == "store-exact":
        result = store_exact(conn, args)
    elif args.command == "recall":
        result = recall(conn, args)
    elif args.command in {"forget", "restore", "archive", "unarchive", "pin", "unpin", "retention"}:
        result = lifecycle(conn, args)
    elif args.command == "list":
        result = list_memories(conn, args)
    elif args.command == "prune":
        result = prune(conn, args)
    elif args.command == "purge":
        result = purge(conn, args)
    elif args.command == "expire":
        result = expire(conn, args)
    elif args.command == "inspect":
        result = inspect_memory(conn, args.memory_id, args.scope)
    elif args.command == "export":
        package = export_scope(conn, args.scope)
        with Path(args.output).open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(package, stream, separators=(",", ":"))
        result = {"exported": True, "scope": args.scope, "sha256": package["sha256"], "output": str(Path(args.output).resolve()), "encryption": "UNENCRYPTED_SYNTHETIC_EXPORT"}
    elif args.command == "import":
        result = _import_scope(conn, args.validated_package, args.scope, user_confirmed=args.user_confirmed)
    else:
        result = stats(conn, args)
    if args.command == "recall":
        packet = render_packet(result, max_tokens=args.max_tokens, reserve_tokens=args.reserve_tokens,
                               max_chars=args.max_chars, include_ids=args.include_ids, encoding=args.encoding, output_format=args.format)
        sys.stdout.write(packet["text"])
    else:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, UnicodeError, sqlite3.Error, OSError, KeyError, TypeError, RecursionError):
        raise SystemExit("memory operation failed validation or storage checks; no content is included in this error") from None
