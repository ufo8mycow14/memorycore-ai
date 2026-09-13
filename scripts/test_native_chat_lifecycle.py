"""Explicit synthetic lifecycle receipts, never native event-capture claims."""
import hashlib
import json
import os
import secrets
import subprocess
from pathlib import Path
import unittest
from scripts import test_rust_encryption as fixtures


@unittest.skipUnless(os.environ.get("MEMORYCORE_AI_SQLCIPHER_BINARY"),"encrypted native build required")
class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.EncryptedNativeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.command=self.fixture.command
        self.root=Path(self.fixture.h.config["sessions"][0]["source_root"])
        self.text="Synthetic release window is Thursday at 14:00."
        self.bind_source("chat-source.md")
        self.id=self.command("remember",{"type":"semantic","subject":"Synthetic release window","summary":self.text,
            "source":"chat-source.md","source_hash":self.digest})["memory_id"]
        self.command("bind-source",{"memory_id":self.id,"path":"chat-source.md","sha256":self.digest})
        self.event("active",1)
        self.command("chat-link",{"chat_id":"synthetic-chat-a","memory_id":self.id,"path":"chat-source.md"})

    def bind_source(self,name):
        raw=self.text.encode()
        (self.root/name).write_bytes(raw)
        self.digest=hashlib.sha256(raw).hexdigest()

    def event(self,state,version,success=True):
        return self.command("chat-event",{"chat_id":"synthetic-chat-a","state":state,"version":version},success=success)

    def recalled(self):
        return [r["id"] for r in self.command("recall",{"query":"release window"})["memories"]]

    def test_archive_unarchive_unknown_and_reconnect(self):
        self.assertIn(self.id,self.recalled())
        self.event("archived",2)
        self.assertNotIn(self.id,self.recalled())
        self.event("active",3)
        self.assertIn(self.id,self.recalled())
        self.event("unknown",4)
        self.assertNotIn(self.id,self.recalled())
        self.event("active",5)
        self.assertIn(self.id,self.recalled())

    def test_retention_setting_survives_reopen_and_cleanup_removes_owned_copies(self):
        self.assertEqual(self.command("archive-retention-status")["days"],365)
        self.command("archive-retention",{"days":90})
        self.assertEqual(self.command("archive-retention-status")["days"],90)
        exact=self.command("store-exact",{"text":"Synthetic retained chat copy.","source":"chat-source.md","user_confirmed":True})
        self.command("stage",{"text":"Synthetic staged chat copy.","source":"chat-source.md"})
        self.event("deleted",2)
        self.command("archive-cleanup")
        self.command("archive-cleanup")
        self.command("recall-exact",{"archive_id":exact["archive_id"]},success=False)
        self.assertEqual(self.command("stats")["hippocampus_records"],0)
        self.command("stage",{"text":"Synthetic rejected copy.","source":"chat-source.md"},success=False)
        self.assertTrue(self.command("verify")["verified"])

    def test_delete_purges_chat_only_fact_and_refuses_resurrection(self):
        self.assertEqual(self.event("deleted",2)["purged"],1)
        self.assertNotIn(self.id,self.recalled())
        self.assertTrue(self.event("deleted",2)["duplicate"])
        self.assertTrue(self.event("active",1)["stale"])
        self.event("active",3,success=False)
        self.command("lifecycle",{"memory_id":self.id,"action":"restore"},success=False)
        self.assertTrue(self.command("verify")["verified"])

    def test_surviving_independent_source_retained_without_deleted_provenance(self):
        self.bind_source("independent-source.md")
        self.command("bind-source",{"memory_id":self.id,"path":"independent-source.md","sha256":self.digest})
        self.assertEqual(self.event("deleted",2)["purged"],0)
        self.assertIn(self.id,self.recalled())
        page=self.command("knowledge-page")
        self.assertNotIn("chat-source.md",str(page))
        self.assertIn("independent-source.md",str(page))
        recalled=self.command("recall",{"query":"release window"})
        self.assertNotIn("chat-source.md",str(recalled))
        self.assertTrue(self.command("verify")["verified"])

    def test_repeated_version_collision_and_provenance_reassignment_rejected(self):
        self.event("archived",1,success=False)
        self.command("chat-event",{"chat_id":"synthetic-chat-b","state":"active","version":1})
        self.command("chat-link",{"chat_id":"synthetic-chat-b","memory_id":self.id,"path":"chat-source.md"},success=False)
        self.assertIn(self.id,self.recalled())

    def test_later_copies_inherit_source_chat_and_deleted_paths_cannot_rebind(self):
        arguments={"type":"semantic","subject":"Copied release window","summary":self.text,
            "source":"chat-source.md","source_hash":self.digest}
        copied=self.command("remember-bound",arguments)["memory_id"]
        self.assertIn(copied,self.recalled())
        self.event("archived",2)
        self.assertNotIn(copied,self.recalled())
        self.command("remember-bound",dict(arguments,subject="Archived copy"),success=False)
        self.event("active",3)
        self.assertIn(copied,self.recalled())
        self.assertEqual(self.event("deleted",4)["purged"],2)
        self.command("remember-bound",dict(arguments,subject="Deleted copy"),success=False)
        self.assertFalse(self.recalled())

    def test_copies_created_before_chat_link_are_also_revoked(self):
        self.bind_source("second-chat-source.md")
        arguments={"type":"semantic","summary":self.text,"source":"second-chat-source.md","source_hash":self.digest}
        first=self.command("remember-bound",dict(arguments,subject="First additional release window"))["memory_id"]
        second=self.command("remember-bound",dict(arguments,subject="Second additional release window"))["memory_id"]
        self.command("chat-event",{"chat_id":"synthetic-chat-b","state":"active","version":1})
        self.command("chat-link",{"chat_id":"synthetic-chat-b","memory_id":first,"path":"second-chat-source.md"})
        result=self.command("chat-event",{"chat_id":"synthetic-chat-b","state":"deleted","version":2})
        self.assertEqual(result["purged"],2)
        self.assertNotIn(second,self.recalled())

    def test_encrypted_snapshot_hides_chat_facts_until_fresh_reconciliation(self):
        destination=self.fixture.h.root/"chat-snapshot.sqlite3"
        environment=dict(self.fixture.env,MEMORYCORE_AI_RESTORE_TEST_KEY=secrets.token_hex(32))
        result=subprocess.run([str(self.fixture.h.binary),"--backup","--config",str(self.fixture.h.file)],
            input=json.dumps({"destination":str(destination),"key_env":"MEMORYCORE_AI_RESTORE_TEST_KEY"}).encode(),
            env=environment,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)["chats_requiring_reconciliation"],1)
        self.assertIn(self.id,self.recalled())
        config=dict(self.fixture.h.config,database=str(destination),key_env="MEMORYCORE_AI_RESTORE_TEST_KEY")
        path=self.fixture.h.root/"chat-restored.json"
        path.write_text(json.dumps(config),encoding="utf-8")
        def restored(action,args=None,success=True):
            return self.command(action,args,config=path,env=environment,success=success)
        self.assertTrue(restored("verify")["verified"])
        self.assertFalse(restored("recall",{"query":"release window"})["memories"])
        restored("chat-event",{"chat_id":"synthetic-chat-a","state":"active","version":1},success=False)
        self.event("deleted",2)
        self.assertEqual(restored("chat-event",{"chat_id":"synthetic-chat-a","state":"deleted","version":2})["purged"],1)
        self.assertFalse(restored("recall",{"query":"release window"})["memories"])
        restored("chat-event",{"chat_id":"synthetic-chat-a","state":"active","version":3},success=False)
        self.assertTrue(restored("verify")["verified"])
