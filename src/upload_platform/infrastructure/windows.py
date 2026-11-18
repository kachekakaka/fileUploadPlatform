"""Windows-only process containment. No PID-only termination or POSIX fallback."""

import ctypes
from ctypes import wintypes as w
from functools import lru_cache
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from upload_platform.domain.core import Problem


@lru_cache(maxsize=1)
def boot_id():
    if os.name != "nt":
        raise Problem("unsupported_os", "Worker 需要 Windows Job Object 环境", 503)
    import pythoncom
    import win32api
    import win32com.client

    pythoncom.CoInitialize()
    service = systems = None
    try:
        service = win32com.client.GetObject(
            "winmgmts:{impersonationLevel=impersonate}!\\\\.\\root\\cimv2"
        )
        systems = list(service.ExecQuery("SELECT LastBootUpTime FROM Win32_OperatingSystem"))
        if len(systems) != 1 or not systems[0].LastBootUpTime:
            raise Problem("boot_identity", "无法核验 Windows 启动身份", 503)
        return win32api.GetComputerName() + "|" + str(systems[0].LastBootUpTime)
    finally:
        # Release IDispatch objects before tearing down their COM apartment.
        systems = None
        service = None
        pythoncom.CoUninitialize()


def process_creation(handle):
    import win32process

    return win32process.GetProcessTimes(handle)["CreationTime"].isoformat()


class STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ("cb", w.DWORD),
        ("lpReserved", w.LPWSTR),
        ("lpDesktop", w.LPWSTR),
        ("lpTitle", w.LPWSTR),
        ("dwX", w.DWORD),
        ("dwY", w.DWORD),
        ("dwXSize", w.DWORD),
        ("dwYSize", w.DWORD),
        ("dwXCountChars", w.DWORD),
        ("dwYCountChars", w.DWORD),
        ("dwFillAttribute", w.DWORD),
        ("dwFlags", w.DWORD),
        ("wShowWindow", w.WORD),
        ("cbReserved2", w.WORD),
        ("lpReserved2", ctypes.POINTER(w.BYTE)),
        ("hStdInput", w.HANDLE),
        ("hStdOutput", w.HANDLE),
        ("hStdError", w.HANDLE),
    ]


class STARTUPINFOEX(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFO), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", w.HANDLE),
        ("hThread", w.HANDLE),
        ("dwProcessId", w.DWORD),
        ("dwThreadId", w.DWORD),
    ]


def _kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        w.DWORD,
        w.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,
        w.DWORD,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    kernel.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel.CreateProcessW.argtypes = [
        w.LPCWSTR,
        w.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        w.BOOL,
        w.DWORD,
        ctypes.c_void_p,
        w.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.ResumeThread.argtypes = [w.HANDLE]
    kernel.ResumeThread.restype = w.DWORD
    return kernel


def _check(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


class JobHost:
    def __init__(
        self,
        execution,
        persist_identity,
        fault=lambda point: None,
        *,
        command=None,
        terminal_stderr=False,
        output_limit=1024 * 1024,
        output_queue_size=256,
    ):
        import msvcrt
        import win32api
        import win32job
        import win32process

        # The caller supplies a deployment-controlled executable, never a command from a work.
        argv = (
            [sys.executable, "-m", "upload_platform.adapters.fake.host"]
            if command is None
            else command
        )
        if (
            not isinstance(argv, (list, tuple))
            or not argv
            or any(not isinstance(arg, str) or "\x00" in arg for arg in argv)
            or not Path(argv[0]).is_absolute()
            or not Path(argv[0]).is_file()
        ):
            raise Problem("invalid_engine_command", "引擎需要可信绝对可执行路径和参数数组", 503)
        argv = list(argv)
        self.execution = execution
        self.job = win32job.CreateJobObject(None, execution["job_name"])
        self.process = self.thread = None
        self.stdin = self.stdout = None
        self.messages = queue.Queue(maxsize=output_queue_size)
        self.protocol_overflow = False
        self.write_in_progress = False
        limits = win32job.QueryInformationJobObject(
            self.job, win32job.JobObjectExtendedLimitInformation
        )
        limits["BasicLimitInformation"]["LimitFlags"] = 0  # no breakaway; recovery owns termination
        win32job.SetInformationJobObject(
            self.job, win32job.JobObjectExtendedLimitInformation, limits
        )
        kernel = _kernel()
        in_read, in_write = os.pipe()
        out_read, out_write = os.pipe()
        err_fd = (
            os.dup(sys.stderr.fileno())
            if terminal_stderr
            else os.open(execution["stderr_path"], os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        )
        owned_fds = {in_read, in_write, out_read, out_write, err_fd}
        private_stderr = not terminal_stderr and "engine_build_id" in execution
        if private_stderr:
            log_fd = err_fd
            err_read, err_fd = os.pipe()
            owned_fds.update((err_read, err_fd))
        attribute_list = None
        try:
            child_handles = [msvcrt.get_osfhandle(fd) for fd in (in_read, out_write, err_fd)] + [
                int(self.job)
            ]
            for handle in child_handles:
                win32api.SetHandleInformation(handle, 1, 1)
            size = ctypes.c_size_t()
            kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            attribute_list = ctypes.cast(buffer, ctypes.c_void_p)
            _check(
                kernel.InitializeProcThreadAttributeList(attribute_list, 1, 0, ctypes.byref(size))
            )
            handles = (w.HANDLE * len(child_handles))(*child_handles)
            _check(
                kernel.UpdateProcThreadAttribute(
                    attribute_list,
                    0,
                    0x00020002,
                    ctypes.cast(handles, ctypes.c_void_p),
                    ctypes.sizeof(handles),
                    None,
                    None,
                )
            )
            startup = STARTUPINFOEX()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = 0x00000100 | 0x00000001
            startup.StartupInfo.wShowWindow = 0
            (
                startup.StartupInfo.hStdInput,
                startup.StartupInfo.hStdOutput,
                startup.StartupInfo.hStdError,
            ) = child_handles[:3]
            startup.lpAttributeList = attribute_list
            info = PROCESS_INFORMATION()
            command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
            _check(
                kernel.CreateProcessW(
                    argv[0],
                    command_line,
                    None,
                    None,
                    True,
                    0x00000004 | 0x00080000 | 0x08000000,
                    None,
                    None,
                    ctypes.byref(startup),
                    ctypes.byref(info),
                )
            )
            self.process, self.thread = int(info.hProcess), int(info.hThread)
            self.pid = int(info.dwProcessId)
            fault("after_create")
            win32job.AssignProcessToJobObject(self.job, self.process)
            fault("after_assign")
            self.execution = {
                **execution,
                "pid": self.pid,
                "creation_time": process_creation(self.process),
                "assigned": True,
            }
            persist_identity(self.execution)
            fault("after_identity")
            for fd in (in_read, out_write, err_fd):
                os.close(fd)
                owned_fds.remove(fd)
            self.stdin = os.fdopen(in_write, "w", encoding="utf-8", buffering=1)
            owned_fds.remove(in_write)
            self.stdout = os.fdopen(out_read, "r", encoding="utf-8")
            owned_fds.remove(out_read)
            if private_stderr:
                source, destination = os.fdopen(err_read, "rb"), os.fdopen(log_fd, "wb")
                owned_fds.remove(err_read)
                owned_fds.remove(log_fd)

                def drain_private_stderr():
                    # Third-party diagnostics may contain cookies/URLs. Retain no raw bytes.
                    # Structured, validated result codes are the operational error evidence.
                    with source, destination:
                        recorded = False
                        while source.read(8192):
                            if not recorded:
                                destination.write(
                                    b"Engine stderr received; consult structured result evidence.\n"
                                )
                                destination.flush()
                                recorded = True

                self.stderr_reader = threading.Thread(target=drain_private_stderr, daemon=True)
                self.stderr_reader.start()
            _check(kernel.ResumeThread(self.thread) != 0xFFFFFFFF)
            kernel.CloseHandle(self.thread)
            self.thread = None
        except BaseException:
            # These live handles were created by this call; ownership is certain.
            if self.process:
                win32process.TerminateProcess(self.process, 75)
            win32job.TerminateJobObject(self.job, 75)
            self.close()
            raise
        finally:
            if attribute_list:
                kernel.DeleteProcThreadAttributeList(attribute_list)
            for fd in owned_fds:
                os.close(fd)
            if self.job:
                win32api.SetHandleInformation(self.job, 1, 0)

        def read_output():
            try:
                while True:
                    line = self.stdout.readline(output_limit + 1)
                    if not line:
                        break
                    if len(line.encode("utf-8")) > output_limit:
                        self.protocol_overflow = True
                        while line and not line.endswith("\n"):
                            line = self.stdout.readline(65536)
                        continue
                    try:
                        self.messages.put_nowait(line)
                    except queue.Full:
                        self.protocol_overflow = True
            except (OSError, ValueError):
                pass
            finally:
                try:
                    self.messages.put_nowait(None)
                except queue.Full:
                    self.protocol_overflow = True

        self.reader = threading.Thread(target=read_output, daemon=True)
        self.reader.start()

    def send(self, message):
        self.write_in_progress = True
        try:
            self.stdin.write(json.dumps(message) + "\n")
            self.stdin.flush()
        finally:
            self.write_in_progress = False

    def exited(self):
        import win32event

        return win32event.WaitForSingleObject(self.process, 0) == win32event.WAIT_OBJECT_0

    def active_count(self):
        import win32job

        return win32job.QueryInformationJobObject(
            self.job, win32job.JobObjectBasicAccountingInformation
        )["ActiveProcesses"]

    def stop(self, timeout=10):
        import win32job

        win32job.TerminateJobObject(self.job, 76)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.active_count() == 0 and self.exited():
                return True
            time.sleep(0.02)
        return False

    def close(self):
        kernel = _kernel()
        if self.stdin and not self.write_in_progress:
            self.stdin.close()
            self.stdin = None
        # stdout may be blocked while an uncertain execution remains alive.
        if self.process and self.exited() and hasattr(self, "reader"):
            self.reader.join(timeout=1)
            if not self.reader.is_alive():
                self.stdout.close()
            if hasattr(self, "stderr_reader"):
                self.stderr_reader.join(timeout=1)
        for name in ("process", "thread"):
            handle = getattr(self, name, None)
            if handle:
                kernel.CloseHandle(handle)
                setattr(self, name, None)
        if self.job:
            self.job.Close()
            self.job = None


def stop_verified(execution, timeout=10, *, terminate=True):
    """Return proof, not merely a successful terminate call."""
    import pywintypes
    import win32api
    import win32event
    import win32job

    current_boot = boot_id()
    old_boot = execution.get("boot_id")
    if not old_boot or old_boot.split("|")[0] != current_boot.split("|")[0]:
        return False, "host_identity_unconfirmed"
    if old_boot != current_boot:
        return True, "previous_boot_exited"
    if (
        not execution.get("assigned")
        or not execution.get("pid")
        or not execution.get("creation_time")
    ):
        return False, "launch_window_unconfirmed"
    process = job = None
    try:
        try:
            process = win32api.OpenProcess(0x00100000 | 0x1000, False, execution["pid"])
        except pywintypes.error as exc:
            if exc.winerror != 87:  # absent PID; access denied is NOT proof
                return False, "process_identity_unreadable"
        if process and process_creation(process) != execution["creation_time"]:
            return False, "process_identity_mismatch"
        try:
            job = win32job.OpenJobObject(0x0004 | 0x0008, False, execution["job_name"])
        except pywintypes.error as exc:
            if exc.winerror != 2:
                return False, "job_identity_unreadable"
        if job is None:
            # Losing the last Job handle can remove its name while descendants live.
            # An absent Job and an exited host are not a whole-tree exit proof.
            return False, "job_exit_unconfirmed"
        if (
            process
            and win32event.WaitForSingleObject(process, 0) != win32event.WAIT_OBJECT_0
            and not win32job.IsProcessInJob(process, job)
        ):
            return False, "job_membership_mismatch"
        if terminate:
            win32job.TerminateJobObject(job, 77)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            active = win32job.QueryInformationJobObject(
                job, win32job.JobObjectBasicAccountingInformation
            )["ActiveProcesses"]
            exited = (
                process is None
                or win32event.WaitForSingleObject(process, 0) == win32event.WAIT_OBJECT_0
            )
            if active == 0 and exited:
                return True, "job_empty_and_host_exited"
            time.sleep(0.02)
        return False, "exit_unconfirmed"
    except (OSError, pywintypes.error):
        return False, "process_check_failed"
    finally:
        if process:
            process.Close()
        if job:
            job.Close()
