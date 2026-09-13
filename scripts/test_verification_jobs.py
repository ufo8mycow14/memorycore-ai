"""Asynchronous integrity checks preserve scope, bounds and honest completion."""
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch
from scripts.verification_jobs import VerificationJobs


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.config={"sessions":[{"id":"admin","scope":"a","allow_admin":True},
                                 {"id":"other","scope":"b","allow_admin":True},
                                 {"id":"reader","scope":"a","allow_admin":False}]}
        self.budget=Mock()
        self.budget.acquire.return_value=True
        self.jobs=VerificationJobs("synthetic-binary",self.config,{},self.budget)
        self.addCleanup(self.jobs.close)

    def request(self,action,arguments=None,session="admin",ident="start"):
        return {"id":ident,"session":session,"operation":"admin", "arguments":{"action":action,"arguments":arguments or {}}}

    def wait(self,ident):
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            result=self.jobs.exchange(self.request("verify-status",{"job_id":ident}))["result"]
            if result["state"] in {"complete","failed"} and "elapsed_ms" in result:
                return result
            time.sleep(.001)
        self.fail("Verification fixture did not complete")

    def test_start_is_nonblocking_idempotent_and_globally_bounded(self):
        entered=threading.Event()
        release=threading.Event()
        def verify(*args):
            entered.set()
            release.wait(2)
        with patch.object(self.jobs,"verify",side_effect=verify) as runner:
            reply=self.jobs.exchange(self.request("verify-start"))
            ident=reply["result"]["job_id"]
            try:
                self.assertTrue(entered.wait(1))
                self.assertFalse(reply["result"]["verified"])
                self.assertEqual(self.jobs.exchange(self.request("verify-start"))["result"]["job_id"],ident)
                self.assertEqual(self.jobs.exchange(self.request("verify-start",ident="second"))["error"],"verification_busy")
                self.assertEqual(self.jobs.exchange(self.request("verify-forget",{"job_id":ident}))["error"],"verification_still_active")
            finally:
                release.set()
            self.assertTrue(self.wait(ident)["verified"])
            runner.assert_called_once()
        self.budget.release.assert_called_once_with("background")

    def test_permission_scope_and_extra_fields_are_rejected(self):
        self.assertIn("error",self.jobs.exchange(self.request("verify-start",session="reader")))
        self.assertIn("error",self.jobs.exchange(self.request("verify-start",{"database":"untrusted"})))
        self.assertIn("error",self.jobs.exchange(self.request("verify-start")|{"recovery":{}}))
        with patch.object(self.jobs,"verify"):
            ident=self.jobs.exchange(self.request("verify-start"))["result"]["job_id"]
            self.wait(ident)
        self.assertEqual(self.jobs.exchange(self.request("verify-status",{"job_id":ident},session="other"))["error"],"verification_job_unavailable")

    def test_failure_is_not_reported_as_verification(self):
        with patch.object(self.jobs,"verify",side_effect=RuntimeError("native_rejected")):
            ident=self.jobs.exchange(self.request("verify-start"))["result"]["job_id"]
            result=self.wait(ident)
        self.assertFalse(result["verified"])
        self.assertEqual(result["error"],"native_rejected")

    def test_completed_receipt_can_be_explicitly_forgotten(self):
        with patch.object(self.jobs,"verify"):
            ident=self.jobs.exchange(self.request("verify-start"))["result"]["job_id"]
            self.wait(ident)
        self.assertTrue(self.jobs.exchange(self.request("verify-forget",{"job_id":ident}))["result"]["forgotten"])
        self.assertFalse(self.jobs.jobs)
        self.assertFalse(self.jobs.requests)

    def test_close_cancels_resource_wait_without_spending_a_permit(self):
        self.budget.acquire.return_value=False
        ident=self.jobs.exchange(self.request("verify-start"))["result"]["job_id"]
        self.jobs.close()
        self.assertEqual(self.jobs.jobs[ident]["state"],"failed")
        self.assertEqual(self.jobs.jobs[ident]["error"],"cancelled")
        self.budget.release.assert_not_called()

    def test_native_response_is_checked_and_configuration_is_pinned(self):
        self.config["sessions"][0]["scope"]="changed-after-construction"
        process=Mock()
        process.returncode=0
        process.poll.return_value=0
        process.communicate.return_value=(json.dumps({"id":"job","result":{"scope":"wrong","verified":True}}).encode(),b"")
        with patch("scripts.verification_jobs.subprocess.Popen",return_value=process):
            with self.assertRaisesRegex(RuntimeError,"invalid_response"):
                self.jobs.verify({"session":"admin","scope":"a","job_id":"job"},time.monotonic())
        self.assertEqual(self.jobs.configuration["sessions"][0]["scope"],"a")

    def test_deadline_kills_only_owned_native_process(self):
        process=Mock()
        process.poll.return_value=None
        with patch("scripts.verification_jobs.subprocess.Popen",return_value=process):
            with self.assertRaisesRegex(RuntimeError,"deadline"):
                self.jobs.verify({"session":"admin","scope":"a","job_id":"job"},time.monotonic()-91)
        process.kill.assert_called_once()


if __name__=="__main__":
    unittest.main()
