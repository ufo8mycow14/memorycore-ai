"""Isolated encryption/broker validation model, never a production service.

The host owns authentication and key material. The broker accepts only capabilities
it issued to an authenticated host principal. No listener, MCP, installation or
real-data activation is provided. SQLite plaintext is confined to process memory.
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

try:
    from . import memorycore_ai as bm
    from .memory_packets import render_packet, RecallSession, token_counter
except ImportError:
    import memorycore_ai as bm
    from memory_packets import render_packet, RecallSession, token_counter

MAGIC = b"BM-LAB2\0"
MAX_VAULT_BYTES = 32 * 1024 * 1024


def derive_key(root_key, domain):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=b"MemoryCoreAILab/2/" + domain).derive(root_key)


def wrap_key(data_key, passphrase):
    if not isinstance(data_key, bytes) or len(data_key) != 32:
        raise ValueError("data key must contain 32 bytes")
    if not isinstance(passphrase, bytes) or not 16 <= len(passphrase) <= 4096:
        raise ValueError("lab passphrase must contain at least 16 bytes")
    salt, nonce = os.urandom(16), os.urandom(12)
    key = Argon2id(salt=salt, length=32, iterations=3, lanes=4, memory_cost=65536).derive(passphrase)
    wrapped = AESGCM(key).encrypt(nonce, data_key, b"MemoryCoreAILab/2/key-slot")
    return b"ARGON2ID1" + salt + nonce + wrapped


def unwrap_key(slot, passphrase):
    if not isinstance(slot, bytes) or len(slot) != 9 + 16 + 12 + 48 or not slot.startswith(b"ARGON2ID1"):
        raise ValueError("unsupported key slot")
    if not isinstance(passphrase, bytes) or not 16 <= len(passphrase) <= 4096:
        raise ValueError("invalid passphrase size or type")
    key = Argon2id(salt=slot[9:25], length=32, iterations=3, lanes=4, memory_cost=65536).derive(passphrase)
    try:
        return AESGCM(key).decrypt(slot[25:37], slot[37:], b"MemoryCoreAILab/2/key-slot")
    except InvalidTag:
        raise ValueError("key slot could not be unlocked") from None


def protect_for_current_windows_user(data, *, unprotect=False):
    """User-bound DPAPI slot; not a claim of hardware-backed protection."""
    if os.name != "nt":
        raise OSError("DPAPI slots are Windows-only")
    import ctypes
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = Blob(len(data), buffer)
    output = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt32.CryptUnprotectData if unprotect else crypt32.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise OSError("DPAPI operation failed")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel32.LocalFree(output.data)


class EncryptedVault:
    """Whole-database AEAD snapshot with caller-supplied rollback checkpoint."""

    def __init__(self, vault_id, key, *, snapshot=None, minimum_revision=0):
        if not isinstance(vault_id, str) or not re.fullmatch(r"[0-9a-f]{32}", vault_id) or not isinstance(key, bytes) or len(key) != 32:
            raise ValueError("invalid lab vault identity or key")
        if type(minimum_revision) is not int or minimum_revision < 0 or (snapshot is None and minimum_revision):
            raise ValueError("invalid rollback checkpoint")
        self.vault_id = vault_id
        self._key = key
        self._closed = False
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self.conn.execute("PRAGMA secure_delete=ON")
        try:
            self._initialise(snapshot, minimum_revision)
        except BaseException:
            self.close()
            raise

    def _initialise(self, snapshot, minimum_revision):
        if snapshot is None:
            bm.initialize(self.conn)
            self.conn.execute("UPDATE vault_state SET vault_id=? WHERE singleton=1", (self.vault_id,))
            self.conn.commit()
        else:
            raw, revision = self._decrypt(snapshot, minimum_revision)
            self.conn.deserialize(raw)
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA temp_store=MEMORY")
            self.conn.execute("PRAGMA secure_delete=ON")
            state = self.conn.execute("SELECT * FROM vault_state WHERE singleton=1").fetchone()
            if not state or state["vault_id"] != self.vault_id or state["revision"] != revision or self.conn.execute("PRAGMA user_version").fetchone()[0] != bm.SCHEMA_VERSION:
                raise ValueError("snapshot identity or revision disagrees with its database")
            if self.conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("snapshot database integrity failed")
            for row in self.conn.execute("SELECT scope FROM cortex_memory UNION SELECT scope FROM cortex_verbatim UNION SELECT scope FROM hippocampus_stage UNION SELECT scope FROM cortex_tombstone").fetchall():
                bm.verify_scope(self.conn, row[0])

    def _decrypt(self, snapshot, minimum_revision):
        if not isinstance(snapshot, bytes) or len(snapshot) > MAX_VAULT_BYTES or not snapshot.startswith(MAGIC) or len(snapshot) < len(MAGIC) + 8 + 12 + 16:
            raise ValueError("invalid encrypted snapshot")
        revision = int.from_bytes(snapshot[len(MAGIC):len(MAGIC) + 8], "big")
        if revision < minimum_revision:
            raise ValueError("snapshot predates the trusted rollback checkpoint")
        header = snapshot[:len(MAGIC) + 8]
        nonce = snapshot[len(header):len(header) + 12]
        try:
            raw = AESGCM(derive_key(self._key, b"snapshot")).decrypt(nonce, snapshot[len(header) + 12:], header + self.vault_id.encode())
        except InvalidTag:
            raise ValueError("snapshot authentication failed") from None
        return raw, revision

    def snapshot(self):
        with self._lock:
            if self._closed or self.conn.in_transaction:
                raise ValueError("vault is closed or has an uncommitted transaction")
            revision = self.conn.execute("SELECT revision FROM vault_state").fetchone()[0]
            raw = self.conn.serialize()
            if len(raw) > MAX_VAULT_BYTES - 64:
                raise ValueError("lab snapshot size limit exceeded")
            header = MAGIC + revision.to_bytes(8, "big")
            nonce = os.urandom(12)
            return header + nonce + AESGCM(derive_key(self._key, b"snapshot")).encrypt(nonce, raw, header + self.vault_id.encode())

    def save_snapshot(self, path):
        path = Path(path)
        # Exclusive creation avoids silently replacing a backup or live vault.
        data = self.snapshot()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "encryption": "AES-256-GCM", "production_approved": False}

    def rotate_key(self, new_key):
        if not isinstance(new_key, bytes) or len(new_key) != 32 or self._closed:
            raise ValueError("key must contain 32 bytes")
        with self._lock:
            previous = self._key
            self._key = new_key
            try:
                return self.snapshot()
            except BaseException:
                self._key = previous
                raise

    def close(self):
        with self._lock:
            if not self._closed:
                self.conn.close()
                self._key = b""
                self._closed = True


class LocalBroker:
    """Host-side authority model; clients cannot create or modify issued grants."""

    OPERATIONS = frozenset({"recall", "remember", "stage", "store-exact", "prune", "purge", "expire",
                            "forget", "restore", "archive", "unarchive", "pin", "unpin", "retention"})

    def __init__(self, vault, principals):
        self._vault = vault
        self._principals = {key: {"scopes": frozenset(value["scopes"]), "operations": frozenset(value["operations"])}
                            for key, value in principals.items()}
        self._grants = {}
        self._audit = []
        self._audit_key = derive_key(vault._key, b"audit")
        self._permission_revision = 0
        self._sessions = {}
        self._approvals = {}

    def approve(self, token, operation, scope, identifiers, *, ttl=60):
        """Trusted host binds a confirmation to exact IDs; never a client RPC."""
        self._authorise(token, operation, scope)
        if operation not in {"purge", "prune"} or not identifiers or not 1 <= ttl <= 300:
            raise ValueError("invalid destructive confirmation")
        approval = secrets.token_bytes(32)
        self._approvals[approval] = (token, operation, scope, tuple(sorted(identifiers)), time.monotonic() + ttl)
        return approval

    def issue(self, authenticated_principal, *, client, scopes, operations, purpose, max_tokens=700, ttl=300):
        # This method belongs exclusively to the trusted host, never a client RPC.
        allowed = self._principals.get(authenticated_principal)
        if client != "codex" or not allowed or not set(scopes) <= allowed["scopes"] or not set(operations) <= (allowed["operations"] & self.OPERATIONS):
            raise PermissionError("grant exceeds host policy")
        if not purpose or not 1 <= ttl <= 3600 or not 64 <= max_tokens <= 4096:
            raise ValueError("invalid capability limits")
        token = secrets.token_bytes(32)
        self._grants[token] = {"principal": authenticated_principal, "scopes": frozenset(scopes),
                               "operations": frozenset(operations), "purpose": purpose,
                               "expires": time.monotonic() + ttl, "max_tokens": max_tokens}
        return token

    def revoke_all(self):
        with self._vault._lock:
            self._grants.clear()
            self._sessions.clear()
            self._approvals.clear()
            self._permission_revision += 1

    def _authorise(self, token, operation, scope):
        grant = self._grants.get(token)
        if not grant or grant["expires"] <= time.monotonic() or operation not in grant["operations"] or scope not in grant["scopes"]:
            raise PermissionError("capability is unavailable or does not authorise this operation")
        return grant

    def _event(self, operation, outcome):
        previous = self._audit[-1]["mac"] if self._audit else ""
        event = {"sequence": len(self._audit) + 1, "operation": operation, "outcome": outcome, "previous": previous}
        event["mac"] = hmac.new(self._audit_key, json.dumps(event, sort_keys=True).encode(), hashlib.sha256).hexdigest()
        self._audit.append(event)

    def verify_audit(self, *, expected_tip=None, expected_length=None):
        previous = ""
        for i, event in enumerate(self._audit, 1):
            if not isinstance(event, dict) or set(event) != {"sequence", "operation", "outcome", "previous", "mac"}:
                return False
            if type(event["sequence"]) is not int or not isinstance(event["operation"], str) or not isinstance(event["outcome"], str):
                return False
            if event["operation"] not in self.OPERATIONS | {"unsupported"} or event["outcome"] not in {"accepted", "rejected"}:
                return False
            if not isinstance(event["mac"], str) or not re.fullmatch(r"[0-9a-f]{64}", event["mac"]) or not isinstance(event["previous"], str):
                return False
            body = {k: v for k, v in event.items() if k != "mac"}
            expected = hmac.new(self._audit_key, json.dumps(body, sort_keys=True).encode(), hashlib.sha256).hexdigest()
            if body["sequence"] != i or body["previous"] != previous or not hmac.compare_digest(expected, event["mac"]):
                return False
            previous = event["mac"]
        return (expected_tip is None or previous == expected_tip) and (expected_length is None or len(self._audit) == expected_length)

    def call(self, token, operation, args, *, session_id=None, acknowledgement=None, context_retained=False, approval=None):
        if type(args) is not argparse.Namespace:
            raise ValueError("request must be a plain argument namespace")
        # Own the exact request that is authorised; discard every caller-owned container.
        encoded = json.dumps(vars(args).copy(), allow_nan=False, separators=(",", ":"))
        if len(encoded.encode()) > 16 * bm.MAX_TEXT_BYTES:
            raise ValueError("request exceeds the lab byte limit")
        args = argparse.Namespace(**json.loads(encoded))
        with self._vault._lock:
            audit_length = len(self._audit)
            try:
                if self._vault.conn.in_transaction:
                    raise ValueError("broker requires ownership of the complete transaction")
                grant = self._authorise(token, operation, args.scope)
                count = token_counter(getattr(args, "encoding", "o200k_base"))
                with bm.transaction(self._vault.conn):
                    result = self._call(token, operation, args, session_id=session_id, acknowledgement=acknowledgement,
                                        context_retained=context_retained, approval=approval)
                    if count(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) > grant["max_tokens"]:
                        raise ValueError("complete response exceeds capability token allowance; operation rolled back")
                    self._event(operation, "accepted")
            except BaseException:
                del self._audit[audit_length:]
                try:
                    self._event(operation if operation in self.OPERATIONS else "unsupported", "rejected")
                except BaseException:
                    pass
                raise
            return result

    def _call(self, token, operation, args, *, session_id=None, acknowledgement=None, context_retained=False, approval=None):
        with self._vault._lock:
            grant = self._authorise(token, operation, args.scope)
            if operation in {"stage", "store-exact"} and (getattr(args, "file", None) or getattr(args, "text", None) is None):
                raise PermissionError("broker accepts supplied text, not filesystem paths or stdin")
            if operation == "purge" or (operation == "prune" and args.apply):
                identifiers = [args.memory_id] if operation == "purge" else args.reviewed_ids
                expected = self._approvals.pop(approval, None)
                if not expected or expected[:4] != (token, operation, args.scope, tuple(sorted(identifiers or []))) or expected[4] <= time.monotonic():
                    raise PermissionError("operation requires a live host approval for the exact reviewed identifiers")
            handlers = {"remember": bm.remember, "stage": bm.stage, "store-exact": bm.store_exact,
                        "prune": bm.prune, "purge": bm.purge, "expire": bm.expire}
            try:
                if operation == "recall":
                    result = bm.recall(self._vault.conn, args)
                    encoding = getattr(args, "encoding", "o200k_base")
                    count = token_counter(encoding)
                    allowance = min(grant["max_tokens"], getattr(args, "max_tokens", grant["max_tokens"]))
                    body_budget = allowance
                    while True:
                        packet = render_packet(result, max_tokens=body_budget, reserve_tokens=0, encoding=encoding,
                                               max_chars=getattr(args, "max_chars", 2500), include_ids=getattr(args, "include_ids", False))
                        used = count(json.dumps(dict(packet, unchanged=False), ensure_ascii=False, separators=(",", ":")))
                        if used <= allowance:
                            break
                        body_budget -= used - allowance + 4
                        if body_budget <= 0:
                            raise ValueError("response budget too small for complete envelope")
                    if session_id:
                        cache = self._sessions.setdefault((token, session_id), RecallSession(session_id))
                        packet = cache.respond(packet, vault_id=self._vault.vault_id, scopes=grant["scopes"],
                                               revision=result["revision"], query=args.query, representation="packet/2",
                                               permission_revision=self._permission_revision,
                                               acknowledgement=acknowledgement, context_retained=context_retained)
                    result = packet
                elif operation in {"forget", "restore", "archive", "unarchive", "pin", "unpin", "retention"}:
                    if args.action != operation:
                        raise PermissionError("lifecycle action does not match capability")
                    result = bm.lifecycle(self._vault.conn, args)
                elif operation in handlers:
                    result = handlers[operation](self._vault.conn, args)
                else:
                    raise PermissionError("operation is not exposed by the lab broker")
            except BaseException:
                raise
            return result
