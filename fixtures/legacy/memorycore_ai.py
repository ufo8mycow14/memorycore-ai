#!/usr/bin/env python3
"""Dependency-free binary memory store for the MemoryCore AI skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
        return zlib.decompress(blob[1:])
    if blob[:1] == b"N":
        return blob[1:]
    raise ValueError("unknown MemoryCore AI compression marker")


def encode_payload(subject: str, summary: str, detail: str, keywords: str, source: str) -> bytes:
    """CMN/1 wire record: magic plus five varint-length UTF-8 fields."""
    raw = WIRE_MAGIC + b"".join(pack_text(value.strip()) for value in (subject, summary, detail, keywords, source))
    return compress(raw)


def decode_payload(blob: bytes) -> dict[str, str]:
    raw = decompress(blob)
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
    return {token for token in cleaned.split() if len(token) > 1}


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


def connect(db_path: str | None) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve() if db_path else default_db_path().resolve()
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

CREATE INDEX IF NOT EXISTS cortex_scope_status ON cortex_memory(scope, status);
CREATE INDEX IF NOT EXISTS cortex_type_status ON cortex_memory(memory_type, status);
CREATE INDEX IF NOT EXISTS cortex_expiry ON cortex_memory(expires_at);
CREATE INDEX IF NOT EXISTS cortex_term_hash ON cortex_term(term_hash);
CREATE INDEX IF NOT EXISTS verbatim_scope_status ON cortex_verbatim(scope, status);
CREATE INDEX IF NOT EXISTS verbatim_expiry ON cortex_verbatim(expires_at);
"""


def initialize(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(cortex_memory)")}
    if "pinned" not in columns:
        conn.execute("ALTER TABLE cortex_memory ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
    conn.commit()


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


def read_input(args: argparse.Namespace) -> str:
    if getattr(args, "text", None) is not None:
        return args.text
    if getattr(args, "file", None):
        return Path(args.file).read_text(encoding="utf-8")
    return sys.stdin.read()


def read_exact_input(args: argparse.Namespace) -> bytes:
    if getattr(args, "text", None) is not None:
        return args.text.encode("utf-8")
    if getattr(args, "file", None):
        return Path(args.file).read_bytes()
    return sys.stdin.buffer.read()


def stage(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    text = read_input(args)
    if not text.strip():
        raise SystemExit("nothing to stage")
    raw = text.encode("utf-8")
    blob = compress(raw)
    stage_id = uuid.uuid4().bytes
    with conn:
        conn.execute(
            """INSERT INTO hippocampus_stage
            (stage_id,created_at,expires_at,scope,source,raw_blob,raw_bytes,stored_bytes,checksum_sha256)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (stage_id, now_utc(), args.expires, args.scope, args.source, blob,
             len(raw), len(blob), hashlib.sha256(raw).digest()),
        )
    return {"stage_id": stage_id.hex(), "raw_bytes": len(raw), "stored_bytes": len(blob)}


def remember(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    if args.type == "classical_conditioning" and not args.user_confirmed:
        raise SystemExit("classical_conditioning requires --user-confirmed")
    if not args.subject.strip() or not args.summary.strip():
        raise SystemExit("subject and summary are required")
    if not 0 <= args.importance <= 1 or not 0 <= args.confidence <= 1:
        raise SystemExit("importance and confidence must be between 0 and 1")

    payload = encode_payload(args.subject, args.summary, args.detail or "", args.keywords, args.source)
    raw_payload = decompress(payload)
    metadata = bytes((MEMORY_TYPES[args.type], SENSITIVITY[args.sensitivity])) + args.scope.encode("utf-8")
    canonical = metadata + raw_payload
    memory_id = hashlib.blake2b(canonical, digest_size=16, person=b"MemoryCoreAI").digest()
    supersedes = parse_id(args.supersedes)
    created = now_utc()

    with conn:
        existing = conn.execute("SELECT memory_id FROM cortex_memory WHERE memory_id=?", (memory_id,)).fetchone()
        if existing:
            return {"memory_id": memory_id.hex(), "deduplicated": True}
        if supersedes:
            row = conn.execute("SELECT status FROM cortex_memory WHERE memory_id=?", (supersedes,)).fetchone()
            if not row:
                raise SystemExit("superseded memory not found")
            conn.execute("UPDATE cortex_memory SET status=1,updated_at=? WHERE memory_id=?", (created, supersedes))
        cursor = conn.execute(
            """INSERT INTO cortex_memory
            (memory_id,memory_type,scope,payload_blob,payload_raw_bytes,payload_stored_bytes,
             importance,confidence,sensitivity,created_at,updated_at,expires_at,pinned,
             supersedes_id,checksum_sha256)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (memory_id, MEMORY_TYPES[args.type], args.scope, payload, len(raw_payload), len(payload),
             round(args.importance * 255), round(args.confidence * 255), SENSITIVITY[args.sensitivity],
             created, created, args.expires, 1 if args.pinned else 0, supersedes,
             hashlib.sha256(canonical).digest()),
        )
        terms = tokenize(" ".join((args.subject, args.summary, args.keywords)))
        conn.executemany(
            "INSERT INTO cortex_term(memory_pk,term_hash) VALUES (?,?)",
            ((cursor.lastrowid, term_hash(token)) for token in terms),
        )
        if getattr(args, "stage_id", None):
            stage_id = parse_id(args.stage_id)
            changed = conn.execute("UPDATE hippocampus_stage SET status=1 WHERE stage_id=? AND status=0", (stage_id,))
            if changed.rowcount != 1:
                raise SystemExit("active staged memory not found")
    return {"memory_id": memory_id.hex(), "deduplicated": False, "supersedes_id": supersedes.hex() if supersedes else None}


def store_exact(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    if not args.user_confirmed:
        raise SystemExit("full-fidelity storage requires --user-confirmed")
    original = read_exact_input(args)
    if not original:
        raise SystemExit("nothing to store exactly")
    if args.retention == "expiring" and not args.expires:
        raise SystemExit("expiring full-fidelity memory requires --expires")
    digest = hashlib.sha256(original).digest()
    identity = args.scope.encode("utf-8") + b"\0" + args.media_type.encode("utf-8") + b"\0" + original
    archive_id = hashlib.blake2b(identity, digest_size=16, person=b"BMemExact").digest()
    blob = compress(original)
    created = now_utc()
    with conn:
        existing = conn.execute("SELECT archive_id FROM cortex_verbatim WHERE archive_id=?", (archive_id,)).fetchone()
        if not existing:
            conn.execute(
                """INSERT INTO cortex_verbatim
                (archive_id,scope,media_type,original_blob,original_bytes,stored_bytes,
                 content_sha256,source,created_at,updated_at,retention,expires_at,pinned)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (archive_id, args.scope, args.media_type, blob, len(original), len(blob),
                 digest, args.source, created, created, args.retention, args.expires,
                 1 if args.retention == "until_user_deletes" or args.pinned else 0),
            )
    verified = hashlib.sha256(decompress(blob)).digest() == digest
    return {
        "archive_id": archive_id.hex(),
        "original_bytes": len(original),
        "content_sha256": digest.hex(),
        "scope": args.scope,
        "media_type": args.media_type,
        "stored_at": created,
        "retention": args.retention,
        "pinned": args.retention == "until_user_deletes" or args.pinned,
        "encryption": "UNENCRYPTED_PROTOTYPE",
        "verified": verified,
        "deduplicated": bool(existing),
    }


def recall_exact(conn: sqlite3.Connection, args: argparse.Namespace) -> bytes:
    archive_id = parse_id(args.archive_id)
    row = conn.execute(
        "SELECT original_blob,content_sha256 FROM cortex_verbatim WHERE archive_id=? AND status=0 AND (expires_at IS NULL OR expires_at>?)",
        (archive_id, now_utc()),
    ).fetchone()
    if not row:
        raise SystemExit("active full-fidelity memory not found")
    original = decompress(row["original_blob"])
    if hashlib.sha256(original).digest() != row["content_sha256"]:
        raise SystemExit("full-fidelity integrity verification failed")
    return original


def recall(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    query_terms = tokenize(args.query)
    where = ["m.status=0", "(m.expires_at IS NULL OR m.expires_at>?)"]
    params: list[object] = [now_utc()]
    if args.scope:
        where.append("m.scope=?")
        params.append(args.scope)
    if args.type:
        where.append("m.memory_type=?")
        params.append(MEMORY_TYPES[args.type])

    if query_terms:
        placeholders = ",".join("?" for _ in query_terms)
        sql = f"""SELECT m.*,count(t.term_hash) AS hits
                  FROM cortex_memory m JOIN cortex_term t ON t.memory_pk=m.memory_pk
                  WHERE {' AND '.join(where)} AND t.term_hash IN ({placeholders})
                  GROUP BY m.memory_pk ORDER BY hits DESC,m.importance DESC,m.updated_at DESC LIMIT ?"""
        params.extend(term_hash(token) for token in sorted(query_terms))
    else:
        sql = f"""SELECT m.*,0 AS hits FROM cortex_memory m
                  WHERE {' AND '.join(where)} ORDER BY m.importance DESC,m.updated_at DESC LIMIT ?"""
    params.append(max(1, args.limit))
    rows = conn.execute(sql, params).fetchall()

    memories = []
    for row in rows:
        payload = decode_payload(row["payload_blob"])
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
            "score": round(score, 4),
        }
        if args.include_detail:
            item["detail"] = payload["detail"]
        memories.append(item)
    return {"query": args.query, "count": len(memories), "memories": memories}


def render_prompt_packet(result: dict, max_chars: int, include_ids: bool) -> str:
    """Render only model-useful text; omit repeated JSON field names."""
    lines = []
    used = 0
    for memory in result["memories"]:
        identity = f" #{memory['memory_id'][:8]}" if include_ids else ""
        line = f"[{memory['type']}|{memory['scope']}{identity}] {memory['subject']}: {memory['summary']}"
        if memory.get("detail"):
            line += f" — {memory['detail']}"
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(line) > remaining:
            if not lines and remaining >= 24:
                lines.append(line[: remaining - 1].rstrip() + "…")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def lifecycle(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    memory_id = parse_id(args.memory_id)
    status_by_action = {"restore": 0, "unarchive": 0, "forget": 2, "archive": 4}
    allowed_from = {"restore": (2, 3), "unarchive": (4,), "forget": (0, 4), "archive": (0,)}
    status = status_by_action[args.action]
    placeholders = ",".join("?" for _ in allowed_from[args.action])
    values = (status, now_utc(), memory_id, *allowed_from[args.action])
    with conn:
        changed_memory = conn.execute(
            f"UPDATE cortex_memory SET status=?,updated_at=? WHERE memory_id=? AND status IN ({placeholders})", values
        )
        changed_archive = conn.execute(
            f"UPDATE cortex_verbatim SET status=?,updated_at=? WHERE archive_id=? AND status IN ({placeholders})", values
        )
    if changed_memory.rowcount + changed_archive.rowcount != 1:
        raise SystemExit("memory not found or lifecycle transition is not allowed")
    return {"memory_id": args.memory_id, "status": STATUS_NAMES[status]}


def list_memories(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    clauses = []
    params: list[object] = []
    if args.scope:
        clauses.append("scope=?")
        params.append(args.scope)
    if args.status:
        status_value = next(value for value, name in STATUS_NAMES.items() if name == args.status)
        clauses.append("status=?")
        params.append(status_value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, args.limit))
    rows = conn.execute(
        f"SELECT * FROM cortex_memory {where} ORDER BY updated_at DESC LIMIT ?", params
    ).fetchall()
    memories = []
    for row in rows:
        payload = decode_payload(row["payload_blob"])
        memories.append({
            "memory_id": row["memory_id"].hex(),
            "type": TYPE_NAMES[row["memory_type"]],
            "scope": row["scope"],
            "subject": payload["subject"],
            "summary": payload["summary"],
            "status": STATUS_NAMES[row["status"]],
            "pinned": bool(row["pinned"]),
            "updated_at": row["updated_at"],
        })
    return {"count": len(memories), "memories": memories}


def prune(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    if not 0 <= args.importance_below <= 1:
        raise SystemExit("importance threshold must be between 0 and 1")
    if args.apply and not args.user_confirmed:
        raise SystemExit("applying a prune requires --user-confirmed")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, args.older_than_days))).isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT * FROM cortex_memory
           WHERE status=0 AND pinned=0 AND memory_type!=? AND created_at<=? AND importance<=?
           ORDER BY importance ASC,created_at ASC LIMIT ?""",
        (MEMORY_TYPES["procedural"], cutoff, round(args.importance_below * 255), max(1, args.limit)),
    ).fetchall()
    candidates = []
    ids = []
    for row in rows:
        payload = decode_payload(row["payload_blob"])
        ids.append(row["memory_id"])
        candidates.append({
            "memory_id": row["memory_id"].hex(),
            "scope": row["scope"],
            "type": TYPE_NAMES[row["memory_type"]],
            "summary": payload["summary"],
            "importance": round(row["importance"] / 255, 3),
            "created_at": row["created_at"],
            "reason": f"older than {args.older_than_days} days and importance at or below {args.importance_below}",
            "proposed_action": "archive",
        })
    if args.apply and ids:
        changed_at = now_utc()
        with conn:
            conn.executemany(
                "UPDATE cortex_memory SET status=4,updated_at=? WHERE memory_id=? AND status=0",
                ((changed_at, memory_id) for memory_id in ids),
            )
    return {"mode": "applied" if args.apply else "preview", "count": len(candidates), "candidates": candidates}


def purge(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    if not args.user_confirmed:
        raise SystemExit("permanent purge requires --user-confirmed")
    memory_id = parse_id(args.memory_id)
    with conn:
        conn.execute("UPDATE cortex_verbatim SET linked_memory_id=NULL WHERE linked_memory_id=?", (memory_id,))
        removed_memory = conn.execute("DELETE FROM cortex_memory WHERE memory_id=?", (memory_id,))
        removed_archive = conn.execute("DELETE FROM cortex_verbatim WHERE archive_id=?", (memory_id,))
    removed = removed_memory.rowcount + removed_archive.rowcount
    if removed != 1:
        raise SystemExit("memory not found or identifier was ambiguous")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return {
        "memory_id": args.memory_id,
        "status": "purged",
        "records_removed": removed,
        "prototype_notice": "No backup or cryptographic-erasure guarantee applies to this unencrypted prototype.",
    }


def expire(conn: sqlite3.Connection) -> dict:
    now = now_utc()
    with conn:
        staged = conn.execute("UPDATE hippocampus_stage SET status=2 WHERE status=0 AND expires_at IS NOT NULL AND expires_at<=?", (now,))
        cortex = conn.execute("UPDATE cortex_memory SET status=3,updated_at=? WHERE status=0 AND expires_at IS NOT NULL AND expires_at<=?", (now, now))
        verbatim = conn.execute("UPDATE cortex_verbatim SET status=3,updated_at=? WHERE status=0 AND pinned=0 AND expires_at IS NOT NULL AND expires_at<=?", (now, now))
    return {"hippocampus_expired": staged.rowcount, "cortex_expired": cortex.rowcount, "verbatim_expired": verbatim.rowcount}


def stats(conn: sqlite3.Connection) -> dict:
    staged = conn.execute("SELECT count(*) n,coalesce(sum(raw_bytes),0) raw,coalesce(sum(stored_bytes),0) stored FROM hippocampus_stage").fetchone()
    cortex = conn.execute("SELECT count(*) n,coalesce(sum(payload_raw_bytes),0) raw,coalesce(sum(payload_stored_bytes),0) stored FROM cortex_memory").fetchone()
    verbatim = conn.execute("SELECT count(*) n,coalesce(sum(original_bytes),0) raw,coalesce(sum(stored_bytes),0) stored FROM cortex_verbatim").fetchone()
    raw = staged["raw"] + cortex["raw"] + verbatim["raw"]
    stored = staged["stored"] + cortex["stored"] + verbatim["stored"]
    return {
        "hippocampus_records": staged["n"],
        "cortex_records": cortex["n"],
        "full_fidelity_records": verbatim["n"],
        "logical_payload_bytes": raw,
        "stored_payload_bytes": stored,
        "binary_compression_ratio": round(raw / stored, 3) if stored else None,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MemoryCore AI binary store")
    parser.add_argument("--db", help="prototype database path; defaults to the operating-system MemoryCore AI home")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")

    stage_parser = commands.add_parser("stage")
    source = stage_parser.add_mutually_exclusive_group()
    source.add_argument("--text")
    source.add_argument("--file")
    stage_parser.add_argument("--scope", default="global")
    stage_parser.add_argument("--source", default="chat")
    stage_parser.add_argument("--expires")

    def add_memory_fields(command: argparse.ArgumentParser) -> None:
        command.add_argument("--type", required=True, choices=sorted(MEMORY_TYPES))
        command.add_argument("--scope", default="global")
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

    remember_parser = commands.add_parser("remember")
    add_memory_fields(remember_parser)
    consolidate_parser = commands.add_parser("consolidate")
    add_memory_fields(consolidate_parser)
    consolidate_parser.add_argument("--stage-id", required=True)

    exact_parser = commands.add_parser("store-exact")
    exact_source = exact_parser.add_mutually_exclusive_group()
    exact_source.add_argument("--text")
    exact_source.add_argument("--file")
    exact_parser.add_argument("--scope", default="global")
    exact_parser.add_argument("--source", default="user")
    exact_parser.add_argument("--media-type", default="text/plain; charset=utf-8")
    exact_parser.add_argument("--retention", choices=("until_user_deletes", "expiring"), default="until_user_deletes")
    exact_parser.add_argument("--expires")
    exact_parser.add_argument("--pinned", action="store_true")
    exact_parser.add_argument("--user-confirmed", action="store_true")

    exact_recall_parser = commands.add_parser("recall-exact")
    exact_recall_parser.add_argument("archive_id")

    recall_parser = commands.add_parser("recall")
    recall_parser.add_argument("query")
    recall_parser.add_argument("--scope")
    recall_parser.add_argument("--type", choices=sorted(MEMORY_TYPES))
    recall_parser.add_argument("--limit", type=int, default=8)
    recall_parser.add_argument("--include-detail", action="store_true")
    recall_parser.add_argument("--format", choices=("prompt", "json"), default="prompt")
    recall_parser.add_argument("--max-chars", type=int, default=2500)
    recall_parser.add_argument("--include-ids", action="store_true")

    for action in ("forget", "restore", "archive", "unarchive"):
        command = commands.add_parser(action)
        command.add_argument("memory_id")
        command.set_defaults(action=action)

    list_parser = commands.add_parser("list")
    list_parser.add_argument("--scope")
    list_parser.add_argument("--status", choices=tuple(STATUS_NAMES.values()))
    list_parser.add_argument("--limit", type=int, default=100)

    prune_parser = commands.add_parser("prune")
    prune_parser.add_argument("--older-than-days", type=int, default=180)
    prune_parser.add_argument("--importance-below", type=float, default=0.25)
    prune_parser.add_argument("--limit", type=int, default=100)
    prune_parser.add_argument("--apply", action="store_true")
    prune_parser.add_argument("--user-confirmed", action="store_true")

    purge_parser = commands.add_parser("purge")
    purge_parser.add_argument("memory_id")
    purge_parser.add_argument("--user-confirmed", action="store_true")
    commands.add_parser("expire")
    commands.add_parser("stats")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    db_path = Path(args.db).expanduser().resolve() if args.db else default_db_path().resolve()
    conn = connect(str(db_path))
    initialize(conn)
    if args.command == "recall-exact":
        sys.stdout.buffer.write(recall_exact(conn, args))
        return
    if args.command == "init":
        result = {"database": str(db_path), "initialized": True, "wire_format": "CMN/1"}
    elif args.command == "stage":
        result = stage(conn, args)
    elif args.command in {"remember", "consolidate"}:
        result = remember(conn, args)
    elif args.command == "store-exact":
        result = store_exact(conn, args)
    elif args.command == "recall":
        result = recall(conn, args)
    elif args.command in {"forget", "restore", "archive", "unarchive"}:
        result = lifecycle(conn, args)
    elif args.command == "list":
        result = list_memories(conn, args)
    elif args.command == "prune":
        result = prune(conn, args)
    elif args.command == "purge":
        result = purge(conn, args)
    elif args.command == "expire":
        result = expire(conn)
    else:
        result = stats(conn)
    if args.command == "recall" and args.format == "prompt":
        print(render_prompt_packet(result, max(0, args.max_chars), args.include_ids))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
