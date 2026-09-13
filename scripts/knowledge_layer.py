"""Synthetic-only knowledge acquisition and retrieval, extension format 1.

No network, watcher, implicit installation or model-generated truth assertions.
Source roots are supplied by the trusted host, never taken from recalled data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import uuid
from pathlib import Path, PurePosixPath

from . import memorycore_ai as bm
from .memory_policy import check_text, check_exact, read_bounded, bounded_integer, timestamp
from .memory_packets import token_counter, render_packet

VERSION = "0.10.0-dev"
MAX_ITEMS = 2000
MAX_SOURCE = 1024 * 1024
RELATIONS = {"supported_by", "applies_to", "contradicts", "depends_on"}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def checked(value):
    raw = canonical(value).encode()
    check_exact(raw, "application/json")
    return json.loads(raw)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("invalid identifier")
    return value


def relative_path(value):
    check_text(value, maximum=512)
    p = PurePosixPath(value)
    if (not value or p.is_absolute() or ".." in p.parts or "\\" in value
            or ":" in value or str(p) != value or value == "."):
        raise ValueError("source path must be canonical and relative")
    return value


class SourceRoot:
    """Bounded UTF-8 reads within an explicitly selected local synthetic root.

    Symlinks and Windows reparse points are rejected. This is not protection
    against a privileged process racing filesystem replacement.
    """
    def __init__(self, root, *, redact_secrets=False):
        if type(redact_secrets) is not bool:
            raise ValueError("redaction setting must be boolean")
        self.redact_secrets = redact_secrets
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("source root must be a directory")

    def read(self, relative):
        relative_path(relative)
        if PurePosixPath(relative).suffix.lower() not in {".txt", ".md", ".json", ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".h", ".cpp", ".cs", ".rb", ".ps1", ".toml", ".yaml", ".yml", ".sql"}:
            raise ValueError("unsupported source format")
        path = self.root
        for part in PurePosixPath(relative).parts:
            path = path / part
            info = path.lstat()
            if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("source links are not supported")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.root) or not resolved.is_file():
            raise ValueError("source is outside the selected root")
        with resolved.open("rb") as stream:
            raw = read_bounded(stream, MAX_SOURCE)
        if not raw:
            raise ValueError("empty source")
        if self.redact_secrets:
            if path.suffix.lower() not in {".txt", ".md"}:
                raise ValueError("redacted source mode supports text and Markdown only")
            from .secret_redaction import redact_text
            redacted = redact_text(raw.decode("utf-8"))
            header = ("[Redacted source view v1; original SHA256: "
                      + hashlib.sha256(raw).hexdigest() + "; redactions: "
                      + str(redacted["redactions"]) + "]\n")
            raw = (header + redacted["text"]).encode("utf-8")
        check_exact(raw, "application/json" if path.suffix.lower() == ".json" else "text/plain")
        return raw

    def inspect(self, binding, cache=None):
        cache = {} if cache is None else cache
        path = binding["path"]
        if path in cache:
            value = cache[path]
            return ("fresh" if value == binding["sha256"] else "changed") if len(value) == 64 else value
        try:
            raw = self.read(path)
            current = hashlib.sha256(raw).hexdigest()
            cache[path] = current
            return "fresh" if current == binding["sha256"] else "changed"
        except FileNotFoundError:
            cache[path] = "missing"
            return "missing"
        except (OSError, ValueError, SystemExit, UnicodeError):
            cache[path] = "unreadable_or_rejected"
            return "unreadable_or_rejected"


def source_binding(path, raw):
    return {"path": relative_path(path), "sha256": hashlib.sha256(raw).hexdigest()}


class Knowledge:
    def __init__(self, conn, *, scope, sources, synthetic=False, create=False):
        if synthetic is not True:
            raise ValueError("synthetic-only acknowledgement is required")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise ValueError("foreign-key enforcement is required")
        self.conn = conn
        self.scope = bm.require_scope(scope)
        self.sources = sources
        if create:
            self.initialize()
        row = conn.execute("SELECT version FROM knowledge_format WHERE singleton=1").fetchone()
        if not row or row[0] != 1:
            raise ValueError("unsupported knowledge extension")

    def initialize(self):
        if self.conn.in_transaction:
            raise ValueError("initialisation requires an idle connection")
        if self.conn.execute("PRAGMA user_version").fetchone()[0] != bm.SCHEMA_VERSION:
            raise ValueError("initialise or migrate the core explicitly first")
        with bm.transaction(self.conn):
            self.conn.execute("CREATE TABLE IF NOT EXISTS knowledge_format (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL)")
            self.conn.execute("INSERT OR IGNORE INTO knowledge_format VALUES(1,1)")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS knowledge_item (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL,
                owner BLOB REFERENCES cortex_memory(memory_id) ON DELETE CASCADE,
                target BLOB REFERENCES cortex_memory(memory_id) ON DELETE CASCADE,
                payload TEXT NOT NULL, checksum TEXT NOT NULL)""")
            self.conn.execute("CREATE INDEX IF NOT EXISTS knowledge_scope_kind ON knowledge_item(scope,kind)")
            for event in ("INSERT", "UPDATE", "DELETE"):
                self.conn.execute(f"CREATE TRIGGER IF NOT EXISTS revision_knowledge_{event} AFTER {event} ON knowledge_item BEGIN UPDATE vault_state SET revision=revision+1 WHERE singleton=1; END")

    def _items(self, kind=None):
        sql = "SELECT * FROM knowledge_item WHERE scope=?"
        params = [self.scope]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        rows = self.conn.execute(sql + " ORDER BY id LIMIT ?", (*params, MAX_ITEMS + 1)).fetchall()
        if len(rows) > MAX_ITEMS:
            raise ValueError("knowledge scope limit exceeded")
        items = []
        for row in rows:
            value = {"id": row["id"], "scope": row["scope"], "kind": row["kind"],
                     "owner": row["owner"].hex() if row["owner"] else None,
                     "target": row["target"].hex() if row["target"] else None,
                     "payload": json.loads(row["payload"])}
            if digest(value) != row["checksum"]:
                raise ValueError("knowledge integrity verification failed")
            self._validate(value)
            items.append(value)
        if sum(i["kind"] == "aliases" for i in items) > 1:
            raise ValueError("multiple alias sets in a scope")
        sources = [(i["owner"], i["payload"]["path"]) for i in items if i["kind"] == "source"]
        if len(set(sources)) != len(sources):
            raise ValueError("duplicate source bindings")
        return items

    def _validate(self, item):
        checked(item)
        if set(item) != {"id", "scope", "kind", "owner", "target", "payload"}:
            raise ValueError("invalid knowledge record fields")
        identifier(item["id"])
        if item["scope"] != self.scope:
            raise ValueError("scope mismatch")
        for key in ("owner", "target"):
            if item[key]:
                self._memory(item[key], active=False)
        p = item["payload"]
        if not isinstance(p, dict):
            raise ValueError("invalid payload")
        kind = item["kind"]
        if kind == "source":
            if not item["owner"] or item["target"]:
                raise ValueError("invalid source owner")
            self._binding(p)
        elif kind == "relation":
            if not item["owner"] or not item["target"] or item["owner"] == item["target"]:
                raise ValueError("invalid relation endpoints")
            if set(p) != {"relation", "evidence"} or p["relation"] not in RELATIONS or not p["evidence"].strip():
                raise ValueError("invalid relation")
        elif kind == "aliases":
            if item["owner"] or item["target"] or set(p) != {"groups"}:
                raise ValueError("invalid aliases")
            self._alias_groups(p["groups"])
        elif kind == "proposal":
            if item["owner"] or item["target"] or set(p) != {"binding", "subject", "summary", "type", "line_start", "line_end", "reviewed", "created_at", "expires_at"}:
                raise ValueError("invalid proposal")
            self._binding(p["binding"])
            if p["reviewed"] is not False or p["type"] not in {"semantic", "procedural", "episodic"}:
                raise ValueError("invalid proposal type or state")
            check_text(p["subject"], maximum=512)
            check_text(p["summary"], maximum=8192)
            if not p["subject"].strip() or not p["summary"].strip():
                raise ValueError("empty proposal")
            bounded_integer(p["line_start"], "line", maximum=50000)
            bounded_integer(p["line_end"], "line", minimum=p["line_start"], maximum=50000)
            if timestamp(p["created_at"], optional=False) != p["created_at"] or timestamp(p["expires_at"], optional=False) != p["expires_at"] or p["expires_at"] != bm.after_days(1, p["created_at"]):
                raise ValueError("proposal must have canonical 24-hour retention")
        else:
            raise ValueError("unsupported knowledge kind")

    @staticmethod
    def _binding(p):
        if set(p) != {"path", "sha256"}:
            raise ValueError("invalid source binding")
        relative_path(p["path"])
        if not isinstance(p["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", p["sha256"]):
            raise ValueError("invalid source digest")

    @staticmethod
    def _alias_groups(groups):
        if not isinstance(groups, list) or len(groups) > 64:
            raise ValueError("alias groups exceed limit")
        for group in groups:
            if not isinstance(group, list) or not 2 <= len(group) <= 8:
                raise ValueError("invalid alias group")
            for word in group:
                if not isinstance(word, str) or not re.fullmatch(r"[a-z]{2,32}", word):
                    raise ValueError("aliases must be lowercase single words")
            if len(set(group)) != len(group):
                raise ValueError("duplicate alias")

    def _put(self, kind, payload, *, owner=None, target=None):
        item = {"id": uuid.uuid4().hex, "scope": self.scope, "kind": kind,
                "owner": owner, "target": target, "payload": payload}
        self._validate(item)
        if self.conn.execute("SELECT count(*) FROM knowledge_item WHERE scope=?", (self.scope,)).fetchone()[0] >= MAX_ITEMS:
            raise ValueError("knowledge scope limit exceeded")
        self.conn.execute("INSERT INTO knowledge_item VALUES(?,?,?,?,?,?,?)",
                          (item["id"], self.scope, kind, bytes.fromhex(owner) if owner else None,
                           bytes.fromhex(target) if target else None, canonical(payload), digest(item)))
        return item

    def _memory(self, mid, *, active=True):
        identifier(mid)
        row = self.conn.execute("SELECT * FROM cortex_memory WHERE memory_id=? AND scope=?", (bytes.fromhex(mid), self.scope)).fetchone()
        if not row:
            raise ValueError("scoped memory not found")
        bm.verify_memory_row(row)
        now = bm.now_utc()
        if active and (row["status"] != 0 or (row["expires_at"] and row["expires_at"] <= now)
                       or row["valid_from"] > now or (row["valid_to"] and row["valid_to"] <= now)):
            raise ValueError("memory is not currently applicable")
        return row

    def bind_source(self, mid, path, expected_sha256):
        """Explicit expected digest prevents accidentally blessing a changed file."""
        raw = self.sources.read(path)
        binding = source_binding(path, raw)
        if binding["sha256"] != expected_sha256:
            raise ValueError("source changed before binding")
        with bm.transaction(self.conn):
            row = self._memory(mid)
            if row["source_hash"] and row["source_hash"] != expected_sha256:
                raise ValueError("memory source digest disagrees")
            existing = [i for i in self._items("source") if i["owner"] == mid and i["payload"]["path"] == path]
            if existing:
                if existing[0]["payload"] != binding:
                    raise ValueError("source rebinding requires a corrected memory")
                return existing[0]
            return self._put("source", binding, owner=mid)

    def freshness(self, mid):
        self._memory(mid)
        return self._freshness(mid, self._items("source"), {})

    def _freshness(self, mid, items, cache):
        bindings = [i["payload"] for i in items if i["owner"] == mid]
        if not bindings:
            return {"state": "unverified", "sources": []}
        statuses = [{"path": b["path"], "state": self.sources.inspect(b, cache)} for b in bindings]
        return {"state": "fresh" if all(s["state"] == "fresh" for s in statuses) else "stale", "sources": statuses}

    def propose(self, path):
        """Extract verbatim, labelled blocks; preserve continuation/exception lines.

        Unlabelled prose is not guessed. More capable extractors can submit
        evidence spans through propose_spans, subject to the same review.
        """
        raw = self.sources.read(path)
        lines = raw.decode("utf-8").splitlines()
        starts = [(n, re.match(r"^(Decision|Fact|Procedure|Episode|Requirement|Constraint|Unfinished):\s+(.+)$", line)) for n, line in enumerate(lines)]
        starts = [(n, m) for n, m in starts if m]
        spans = []
        for index, (start, match) in enumerate(starts):
            end = starts[index + 1][0] if index + 1 < len(starts) else len(lines)
            # An explicit source delimiter can bound the evidence; never guess
            # that a negation or an exception is expendable boilerplate.
            stop = next((n for n in range(start + 1, end) if lines[n] == "End memory."), None)
            if stop is not None:
                end = stop
            while end > start + 1 and not lines[end - 1].strip():
                end -= 1
            spans.append({"line_start": start + 1, "line_end": end, "subject": match[2][:180],
                          "type": {"Procedure": "procedural", "Episode": "episodic"}.get(match[1], "semantic")})
        return self.propose_spans(path, spans, expected_sha256=hashlib.sha256(raw).hexdigest())

    def propose_spans(self, path, spans, *, expected_sha256):
        raw = self.sources.read(path)
        binding = source_binding(path, raw)
        if binding["sha256"] != expected_sha256:
            raise ValueError("source changed before extraction")
        if not isinstance(spans, list) or len(spans) > 64:
            raise ValueError("too many proposal spans")
        lines = raw.decode().splitlines()
        prepared = []
        created = bm.now_utc()
        for span in checked(spans):
            if set(span) != {"line_start", "line_end", "subject", "type"}:
                raise ValueError("invalid evidence span")
            start = bounded_integer(span["line_start"], "line", maximum=len(lines))
            end = bounded_integer(span["line_end"], "line", minimum=start, maximum=len(lines))
            prepared.append({**span, "summary": "\n".join(lines[start-1:end]), "binding": binding, "reviewed": False,
                             "created_at": created, "expires_at": bm.after_days(1, created)})
        with bm.transaction(self.conn):
            self.expire_proposals()
            content_key = lambda p: digest({key:value for key,value in p.items() if key not in {"created_at", "expires_at"}})
            existing = {content_key(i["payload"]): i for i in self._items("proposal")}
            proposals = []
            for p in prepared:
                key = content_key(p)
                if key not in existing:
                    existing[key] = self._put("proposal", p)
                item = existing[key]
                proposals.append({"id": item["id"], "review_digest": digest(item), **item["payload"]})
        return {"proposals": proposals, "extraction": "verbatim_evidence_spans", "automatic_truth_verification": False}

    def review(self, pid):
        item = next((i for i in self._items("proposal") if i["id"] == identifier(pid)), None)
        if item is None:
            raise ValueError("proposal not found")
        return {"item": item, "review_digest": digest(item), "source_state": self.sources.inspect(item["payload"]["binding"]),
                "expired": item["payload"]["expires_at"] <= bm.now_utc()}

    def expire_proposals(self):
        with bm.transaction(self.conn):
            ids = [i["id"] for i in self._items("proposal") if i["payload"]["expires_at"] <= bm.now_utc()]
            self.conn.executemany("DELETE FROM knowledge_item WHERE scope=? AND id=?", ((self.scope, pid) for pid in ids))
        return {"expired_proposals_disposed": len(ids)}

    def accept(self, pid, review_digest, *, supersedes=None):
        with bm.transaction(self.conn):
            review = self.review(pid)
            if review["review_digest"] != review_digest or review["source_state"] != "fresh" or review["expired"]:
                raise ValueError("proposal review is stale")
            p = review["item"]["payload"]
            row = self._memory(supersedes) if supersedes else None
            # Same subject is deliberately ambiguous; require a correction decision.
            candidates = self._candidates()
            if not row and any(r["subject"].casefold() == p["subject"].casefold() for r in candidates):
                raise ValueError("existing subject requires explicit correction review")
            args = bm.build_parser().parse_args(["remember", "--scope", self.scope, "--type", p["type"],
                "--subject", p["subject"], "--summary", p["summary"], "--source", p["binding"]["path"],
                "--source-hash", p["binding"]["sha256"], "--confidence", "0.5",
                "--confidence-reason", "Reviewed source excerpt; factual truth not independently verified"])
            args.supersedes = supersedes
            result = bm.remember(self.conn, args)
            self.bind_source(result["memory_id"], p["binding"]["path"], p["binding"]["sha256"])
            self.conn.execute("DELETE FROM knowledge_item WHERE id=? AND scope=?", (pid, self.scope))
            return result

    def reject(self, pid, review_digest):
        with bm.transaction(self.conn):
            if self.review(pid)["review_digest"] != review_digest:
                raise ValueError("proposal review is stale")
            self.conn.execute("DELETE FROM knowledge_item WHERE id=? AND scope=?", (pid, self.scope))
        return {"discarded": True}

    def aliases(self, groups, *, reviewed=False):
        if reviewed is not True:
            raise ValueError("alias changes require explicit review")
        self._alias_groups(groups)
        with bm.transaction(self.conn):
            self.conn.execute("DELETE FROM knowledge_item WHERE scope=? AND kind='aliases'", (self.scope,))
            return self._put("aliases", {"groups": groups})

    def relate(self, owner, target, relation, evidence, *, reviewed=False):
        if reviewed is not True:
            raise ValueError("relations require explicit review")
        with bm.transaction(self.conn):
            self._memory(owner)
            self._memory(target)
            payload = {"relation": relation, "evidence": evidence}
            for item in self._items("relation"):
                if (item["owner"], item["target"], item["payload"]) == (owner, target, payload):
                    return item
            return self._put("relation", payload, owner=owner, target=target)

    def relations(self, mid, *, depth=1):
        self._memory(mid)
        bounded_integer(depth, "depth", maximum=3)
        edges = self._items("relation")
        frontier, seen, result, included = {mid}, {mid}, [], set()
        for _ in range(depth):
            following = set()
            for edge in edges:
                if edge["id"] in included or not ({edge["owner"], edge["target"]} & frontier):
                    continue
                try:
                    states = [self.freshness(edge[k])["state"] for k in ("owner", "target")]
                except ValueError:
                    continue
                if "stale" in states:
                    continue
                result.append({**edge, "endpoint_freshness": {"owner": states[0], "target": states[1]}})
                included.add(edge["id"])
                following.update({edge["owner"], edge["target"]} - seen)
            seen.update(following)
            frontier = following
        return {"edges": result, "inferred_truth": False}

    def _candidates(self):
        args = argparse.Namespace(query="", scope=self.scope, type=None, limit=100, include_detail=False, browse=True)
        # Bounded experiment scans all visible records, refusing silent truncation.
        result = bm.recall(self.conn, args, _allow_extensions=True)
        if result["candidate_limit_reached"]:
            raise ValueError("experimental retrieval supports at most 100 visible memories per scope")
        return result["memories"]

    def recall(self, query, *, mode="lexical", limit=8, max_tokens=700, require_fresh=True, count=None):
        check_text(query, maximum=4096)
        bounded_integer(limit, "limit", maximum=100)
        bounded_integer(max_tokens, "max_tokens", minimum=64, maximum=8000)
        if mode not in {"lexical", "aliases", "hybrid"} or type(require_fresh) is not bool:
            raise ValueError("unsupported recall options")
        count = count or token_counter()
        with bm.transaction(self.conn, write=False):
            terms = bm.tokenize(query)
            if len(terms) > 64:
                raise ValueError("too many query terms")
            groups = [g for i in self._items("aliases") for g in i["payload"]["groups"]] if mode != "lexical" else []
            expanded = set(terms)
            for group in groups:
                if terms.intersection(group):
                    expanded.update(group)
            candidates = self._candidates()
            source_items, source_cache = self._items("source"), {}
            scored, excluded = [], {"stale": 0, "unverified": 0}
            for item in candidates:
                row = self._memory(item["memory_id"])
                payload = bm.verify_memory_row(row)
                # Search includes details, output remains summary-first.
                words = bm.tokenize(" ".join((payload["subject"], payload["summary"], payload["keywords"], bm.load_detail(self.conn, row))))
                hits = len(words & (terms if mode == "lexical" else expanded))
                if not terms or not hits:
                    continue
                score = 0.60 * hits / max(1, len(expanded if mode != "lexical" else terms)) + 0.25 * item["importance"] + 0.15 * item["confidence"]
                if mode == "hybrid":
                    # Interpretable concept cosine, not a pretrained embedding model.
                    concept_words, concept_query = set(words), set(terms)
                    for n, group in enumerate(groups):
                        if words.intersection(group):
                            concept_words.difference_update(group)
                            concept_words.add(f"concept:{n}")
                        if terms.intersection(group):
                            concept_query.difference_update(group)
                            concept_query.add(f"concept:{n}")
                    cosine = len(concept_words & concept_query) / math.sqrt(max(1, len(concept_words) * len(concept_query)))
                    score = 0.5 * score + 0.5 * cosine
                fresh = self._freshness(item["memory_id"], source_items, source_cache)
                if fresh["state"] == "stale" or (require_fresh and fresh["state"] == "unverified"):
                    excluded[fresh["state"]] += 1
                    continue
                scored.append({"id": item["memory_id"], "subject": item["subject"], "summary": item["summary"],
                               "source": item["source"], "source_hash": item["source_hash"],
                               "confidence": item["confidence"], "confidence_reason": item["confidence_reason"],
                               "observed_at": item["observed_at"], "valid_from": item["valid_from"], "valid_to": item["valid_to"],
                               "freshness": fresh, "score": round(score, 6)})
            scored.sort(key=lambda i: (-i["score"], i["id"]))
            result = {"scope": self.scope, "mode": mode, "data_only": True, "memories": [],
                      "omitted": len(scored), "excluded": excluded,
                      "revision": self.conn.execute("SELECT revision FROM vault_state").fetchone()[0]}
            for item in scored[:limit]:
                candidate = {**result, "memories": result["memories"] + [item], "omitted": result["omitted"] - 1}
                if count(canonical(candidate)) <= max_tokens:
                    result = candidate
            if count(canonical(result)) > max_tokens:
                raise ValueError("budget too small for recall receipt")
            return result

    def recall_compact(self, query, *, mode="lexical", limit=8, max_tokens=700, require_fresh=True, count=None):
        """Reuse the existing whole-fact packet renderer, retaining provenance.

        Freshness is checked each request; this is not an unchanged-cache marker.
        A fresh process gets real facts, including exceptions and unfinished work.
        """
        count = count or token_counter()
        bounded_integer(max_tokens, "max_tokens", minimum=64, maximum=8000)
        result = self.recall(query, mode=mode, limit=limit, max_tokens=8000, require_fresh=require_fresh, count=count)
        rows = []
        for item in result["memories"]:
            row = dict(item, memory_id=item["id"], type="semantic")
            # Preserve the actual core category without decompressing content.
            row["type"] = bm.TYPE_NAMES[self.conn.execute("SELECT memory_type FROM cortex_memory WHERE memory_id=? AND scope=?", (bytes.fromhex(item["id"]), self.scope)).fetchone()[0]]
            rows.append(row)
        payload = {"scope": result["scope"], "revision": result["revision"], "memories": rows,
                   "candidate_limit_reached": bool(result["omitted"])}
        receipt = {"source_checked": True, "requires_fresh": require_fresh, "excluded": result["excluded"],
                   "mode": mode, "retrieval_omitted": result["omitted"]}
        # Count the actual outer serialization, including escaped packet text.
        for allowance in range(max_tokens, 31, -16):
            try:
                packet = render_packet(payload, max_tokens=allowance, reserve_tokens=0, max_chars=32000,
                                       include_ids=True, count=count)
            except SystemExit:
                continue
            response = {**receipt, "packet": packet["text"]}
            if count(canonical(response)) <= max_tokens:
                return response
        raise ValueError("budget too small for compact recall receipt")

    def export(self, *, _allow_routing=False):
        if not _allow_routing and self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_route'").fetchone():
            if self.conn.execute("SELECT 1 FROM session_route WHERE scope=? LIMIT 1", (self.scope,)).fetchone():
                raise ValueError("use RouteOutbox.export to retain routing state")
        with bm.transaction(self.conn, write=False):
            package = {"format": "brain-knowledge/1", "scope": self.scope,
                       "core": bm.export_scope(self.conn, self.scope, _allow_extensions=True), "items": self._items()}
            return {**package, "sha256": digest(package)}

    def import_package(self, package, *, reviewed=False):
        package = checked(package)
        if reviewed is not True or set(package) != {"format", "scope", "core", "items", "sha256"}:
            raise ValueError("reviewed knowledge package required")
        if package["format"] != "brain-knowledge/1" or package["scope"] != self.scope or digest({k:v for k,v in package.items() if k != "sha256"}) != package["sha256"]:
            raise ValueError("knowledge package integrity or scope mismatch")
        if not isinstance(package["items"], list) or len(package["items"]) > MAX_ITEMS:
            raise ValueError("knowledge package limit exceeded")
        with bm.transaction(self.conn):
            bm.import_scope(self.conn, package["core"], self.scope, user_confirmed=True)
            for item in package["items"]:
                self._validate(item)
                self.conn.execute("INSERT INTO knowledge_item VALUES(?,?,?,?,?,?,?)", (item["id"], self.scope, item["kind"],
                    bytes.fromhex(item["owner"]) if item["owner"] else None,
                    bytes.fromhex(item["target"]) if item["target"] else None, canonical(item["payload"]), digest(item)))
            self._items()
        return {"imported": True, "source_freshness": "must_be_rechecked_in_selected_root"}
