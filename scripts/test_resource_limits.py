"""Enforcement tests run only against disposable, handshake-controlled children."""
import json
import os
import subprocess
import sys
import unittest
from scripts.resource_limits import InferenceLimits


@unittest.skipUnless(os.name=="nt","Windows Job Objects")
class InferenceContainmentTests(unittest.TestCase):
    def child(self,code):
        process=subprocess.Popen([sys.executable,"-I","-c","import sys; sys.stdin.readline(); "+code],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
        def close():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.addCleanup(close)
        return process

    def test_committed_memory_limit_rejects_large_owned_allocation(self):
        limits=InferenceLimits(10,64*1024**2)
        self.addCleanup(limits.close)
        self.assertEqual(limits.state()["mode"],"windows-job")
        process=self.child("exec('try:\\n data=bytearray(256*1024**2)\\n print(\"unbounded\")\\nexcept MemoryError:\\n print(\"bounded\")')")
        self.assertTrue(limits.assign(process.pid))
        output,error=process.communicate(b"start\n",timeout=10)
        self.assertEqual(process.returncode,0,error)
        self.assertEqual(output.strip(),b"bounded")

    def test_cpu_hard_cap_and_kill_owned_process_on_close(self):
        import psutil
        cpus=psutil.cpu_count() or 1
        # Less than one logical CPU; this test cannot saturate the user's machine.
        percent=min(5,50/cpus)
        limits=InferenceLimits(percent,64*1024**2)
        self.addCleanup(limits.close)
        code="import time,json; start=time.perf_counter(); cpu=time.process_time(); exec('while time.perf_counter()-start<2:\\n pass'); print(json.dumps({'wall':time.perf_counter()-start,'cpu':time.process_time()-cpu}))"
        process=self.child(code)
        self.assertTrue(limits.assign(process.pid))
        output,error=process.communicate(b"start\n",timeout=10)
        self.assertEqual(process.returncode,0,error)
        measured=json.loads(output)
        self.assertLess(measured["cpu"]/measured["wall"],min(.85,percent*cpus/100+.2))
        sleeper=self.child("import time; time.sleep(30)")
        self.assertTrue(limits.assign(sleeper.pid))
        limits.close()
        sleeper.wait(timeout=5)
        self.assertIsNotNone(sleeper.poll())

    def test_initial_telemetry_failure_recovers_before_child_start(self):
        budget=[0]
        limits=InferenceLimits(10,0,memory_provider=lambda:budget[0])
        self.addCleanup(limits.close)
        self.assertEqual(limits.state()["setup_error"],"resource_telemetry_unavailable")
        budget[0]=64*1024**2
        process=self.child("print('ready')")
        self.assertTrue(limits.assign(process.pid))
        output,error=process.communicate(b"start\n",timeout=5)
        self.assertEqual(process.returncode,0,error)
        self.assertEqual(output.strip(),b"ready")
        self.assertEqual(limits.state()["mode"],"windows-job")


if __name__=="__main__":
    unittest.main()
