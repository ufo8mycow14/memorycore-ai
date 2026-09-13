"""SQLCipher process tests with temporary synthetic keys, never real credentials."""
import base64
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import time
import unittest
from pathlib import Path
from scripts.test_rust_broker import Harness

BINARY = Path(os.environ.get("MEMORYCORE_AI_SQLCIPHER_BINARY", "missing-sqlcipher-binary"))


@unittest.skipUnless(BINARY.is_file(), "set MEMORYCORE_AI_SQLCIPHER_BINARY to the SQLCipher build")
class EncryptedNativeTests(unittest.TestCase):
    def setUp(self):
        self.key = secrets.token_hex(32)
        self.env = dict(os.environ, MEMORYCORE_AI_SYNTHETIC_TEST_KEY=self.key)
        self.h = Harness(0, binary=BINARY, environment=self.env)
        self.addCleanup(self.h.close)
        self.target = self.h.root / "encrypted.sqlite3"
        self.h.config.update(backend="native", allow_plaintext=False, database=str(self.target), key_env="MEMORYCORE_AI_SYNTHETIC_TEST_KEY")
        self.h.config.pop("python")
        self.h.config.pop("backend_root")
        for s in self.h.config["sessions"]:
            s["allow_admin"] = True
        self.h.file.write_text(json.dumps(self.h.config), encoding="utf-8")
        result = subprocess.run([str(BINARY), "--init", "--config", str(self.h.file)], env=self.env, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def command(self, action, arguments=None, *, env=None, config=None, success=True):
        request = {"session":"chat-0","id":"one","operation":"admin","arguments":{"action":action,"arguments":arguments or {}}}
        result = subprocess.run([str(BINARY), "--native-command", "--config", str(config or self.h.file)],
            input=json.dumps(request).encode(), env=env or self.env, capture_output=True, timeout=30)
        self.assertNotIn(self.key.encode(), result.stdout + result.stderr)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)["result"]
        self.assertNotEqual(result.returncode, 0)

    def test_encrypted_reopen_wrong_key_and_ordinary_sqlite_rejected(self):
        exact = self.command("store-exact", {"text":"Synthetic encrypted record.", "user_confirmed":True})
        self.assertEqual(exact["encryption"], "SQLCIPHER")
        self.assertTrue(self.command("verify")["verified"])
        self.assertNotEqual(self.target.read_bytes()[:16], b"SQLite format 3\0")
        self.assertNotIn(b"Synthetic encrypted record.", self.target.read_bytes())
        self.command("verify", env=dict(self.env, MEMORYCORE_AI_SYNTHETIC_TEST_KEY=secrets.token_hex(32)), success=False)
        missing = dict(self.env)
        missing.pop("MEMORYCORE_AI_SYNTHETIC_TEST_KEY")
        self.command("verify", env=missing, success=False)
        conn = sqlite3.connect(self.target)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("SELECT * FROM sqlite_master").fetchall()
        finally:
            conn.close()

    def test_closed_encrypted_database_is_portable_with_same_key(self):
        exact = self.command("store-exact", {"text":"Synthetic portable encrypted record.", "user_confirmed":True})
        destination = self.h.root / "portable.sqlite3"
        shutil.copy2(self.target, destination)
        config = dict(self.h.config, database=str(destination))
        path = self.h.root / "portable.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        self.assertTrue(self.command("verify", config=path)["verified"])
        self.assertEqual(self.command("recall-exact", {"archive_id":exact["archive_id"]}, config=path),
                         self.command("recall-exact", {"archive_id":exact["archive_id"]}))

    def test_ten_simultaneous_encrypted_writes_and_wal(self):
        self.h.start()
        for i in range(10):
            self.h.send(f"chat-{i}", "admin", {"action":"remember","arguments":{"type":"semantic","subject":f"Synthetic{i}","summary":f"Encrypted synthetic service{i} data."}})
        for _ in range(10):
            result = self.h.receive()
            self.assertNotIn("error", result)
            self.assertEqual(result["lane"], "write")
        wal = Path(str(self.target) + "-wal")
        self.assertTrue(wal.is_file())
        self.assertNotIn(b"Encrypted synthetic", wal.read_bytes())
        self.h.send("chat-0", "admin", {"action":"verify","arguments":{}})
        self.assertTrue(self.h.receive()["result"]["verified"])

    def test_tampered_ciphertext_is_rejected(self):
        self.command("store-exact", {"text":"Synthetic integrity protected record.", "user_confirmed":True})
        raw = bytearray(self.target.read_bytes())
        raw[4096+64] ^= 1
        self.target.write_bytes(raw)
        self.command("verify", success=False)

    def test_background_checkpoint_reports_progress_without_losing_encrypted_writes(self):
        self.h.start()
        receipts=[]
        saved=[]
        deadline=time.monotonic()+5
        while time.monotonic()<deadline and not receipts:
            self.h.send("chat-0","admin",{"action":"remember","arguments":{"type":"semantic",
                "subject":"Checkpoint fixture "+str(len(saved)),"summary":"Durable encrypted checkpoint evidence."}})
            response=self.h.receive()
            self.assertNotIn("error",response)
            saved.append(response["result"]["memory_id"])
            if "checkpoint" in response:
                receipts.append(response["checkpoint"])
            time.sleep(.05)
        self.assertTrue(receipts)
        self.assertTrue(receipts[0]["background"])
        self.assertFalse(receipts[0]["failed"])
        self.assertGreater(receipts[0]["checkpointed_pages"],0)
        self.assertGreaterEqual(receipts[0]["log_pages"],receipts[0]["checkpointed_pages"])
        self.h.process.stdin.close()
        self.assertEqual(self.h.process.wait(timeout=15),0)
        for memory_id in saved:
            stored=self.command("inspect",{"memory_id":memory_id})["record"]["memory_id"]
            self.assertEqual(base64.b64decode(stored["base64"],validate=True).hex(),memory_id)
        self.assertTrue(self.command("verify")["verified"])

    def test_live_encrypted_snapshot_and_independent_key_restore(self):
        self.h.start()
        self.h.send("chat-0", "admin", {"action":"store-exact", "arguments":{"text":"Synthetic live snapshot content", "user_confirmed":True}})
        exact = self.h.receive()["result"]
        destination = self.h.root / "snapshot.sqlite3"
        new_env = dict(self.env, MEMORYCORE_AI_BACKUP_TEST_KEY=secrets.token_hex(32))
        options = {"destination":str(destination), "key_env":"MEMORYCORE_AI_BACKUP_TEST_KEY"}
        def backup(environment):
            return subprocess.run([str(BINARY), "--backup", "--config", str(self.h.file)],
                input=json.dumps(options).encode(), env=environment, capture_output=True, timeout=40)
        same = backup(dict(new_env, MEMORYCORE_AI_BACKUP_TEST_KEY=self.key))
        self.assertNotEqual(same.returncode, 0)
        self.assertFalse(destination.exists())
        result = backup(new_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["encrypted"])
        self.assertNotIn(b"Synthetic live snapshot content", destination.read_bytes())
        before = destination.read_bytes()
        self.assertNotEqual(backup(new_env).returncode, 0)
        self.assertEqual(before, destination.read_bytes())
        config = dict(self.h.config, database=str(destination), key_env="MEMORYCORE_AI_BACKUP_TEST_KEY")
        path = self.h.root / "restored.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        self.assertTrue(self.command("verify", config=path, env=new_env)["verified"])
        restored = self.command("recall-exact", {"archive_id":exact["archive_id"]}, config=path, env=new_env)
        self.assertEqual(restored, self.command("recall-exact", {"archive_id":exact["archive_id"]}))
        self.command("verify", config=path, env=dict(new_env, MEMORYCORE_AI_BACKUP_TEST_KEY=self.key), success=False)

    def test_encrypted_plaintext_export_requires_host_opt_in(self):
        self.command("export", success=False)
        config = json.loads(self.h.file.read_text(encoding="utf-8"))
        config["sessions"][0]["allow_plaintext_export"] = True
        path = self.h.root / "explicit-export.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        self.assertEqual(self.command("export", config=path)["format"], "brain-knowledge/1")


if __name__ == "__main__":
    unittest.main()
