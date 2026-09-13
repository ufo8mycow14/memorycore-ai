"""Optional platform containment of this host's inference children, never other apps."""
import ctypes
import os
import threading


class InferenceLimits:
    def __init__(self, cpu_percent, memory_bytes, memory_provider=None):
        self.lock=threading.RLock()
        self.handle = None
        self.error = None
        self.cpu_percent = cpu_percent
        self.memory_bytes = memory_bytes
        self.memory_provider=memory_provider
        self.setup()

    def setup(self):
        if os.name != "nt":
            return
        memory_bytes=self.memory_provider() if self.memory_provider else self.memory_bytes
        if memory_bytes<=0:
            self.error="resource_telemetry_unavailable"
            return
        self.memory_bytes=memory_bytes
        self.error=None
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = ctypes.c_void_p
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        self.kernel.CreateJobObjectW.restype = handle
        self.kernel.SetInformationJobObject.argtypes = [handle, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        self.kernel.SetInformationJobObject.restype = ctypes.c_int
        self.kernel.AssignProcessToJobObject.argtypes = [handle, handle]
        self.kernel.AssignProcessToJobObject.restype = ctypes.c_int
        self.kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        self.kernel.OpenProcess.restype = handle
        self.kernel.CloseHandle.argtypes = [handle]
        self.kernel.CloseHandle.restype = ctypes.c_int

        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", ctypes.c_uint32), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("process_limit", ctypes.c_uint32),
                        ("affinity", ctypes.c_size_t), ("priority", ctypes.c_uint32),
                        ("scheduling", ctypes.c_uint32)]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", ctypes.c_uint64*6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        class Cpu(ctypes.Structure):
            _fields_ = [("flags", ctypes.c_uint32), ("rate", ctypes.c_uint32)]
        try:
            self.handle = self.kernel.CreateJobObjectW(None, None)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = Extended()
            limits.basic.flags = 0x200 | 0x2000  # Job memory and kill owned children on final close.
            limits.job_memory = int(memory_bytes)
            cpu = Cpu(0x1 | 0x4, int(self.cpu_percent*100))
            for kind, value in ((9, limits), (15, cpu)):
                if not self.kernel.SetInformationJobObject(self.handle, kind, ctypes.byref(value), ctypes.sizeof(value)):
                    raise ctypes.WinError(ctypes.get_last_error())
        except OSError as exc:
            self.error = exc.winerror
            self.close()

    def assign(self, pid):
        with self.lock:
            return self.assign_locked(pid)

    def assign_locked(self,pid):
        if self.handle is None and os.name=="nt":
            self.setup()
        if self.handle is None:
            if os.name=="nt":
                raise OSError("Required Windows inference containment unavailable")
            return False
        process = self.kernel.OpenProcess(0x100 | 0x1, False, pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.kernel.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
            return True
        finally:
            self.kernel.CloseHandle(process)

    def state(self):
        return {"mode": "windows-job" if self.handle else "soft-governor",
                "scope": "inference-children", "cpu_percent": self.cpu_percent,
                "committed_memory_limit_bytes": self.memory_bytes, "setup_error": self.error}

    def close(self):
        with self.lock:
            if self.handle:
                self.kernel.CloseHandle(self.handle)
                self.handle = None
