"""Direct executable launch uses the same suspended-create and Job containment as Fake."""

import json
import sys
import time

import pytest
import win32event

from upload_platform.domain.core import Problem, uid
from upload_platform.infrastructure.windows import JobHost


def test_direct_engine_arguments_and_persist_before_execution(tmp_path):
    marker = tmp_path / "started.json"
    arguments = ["中文 空格", 'quoted"value', "a&b|c;$(echo ignored)", "tail\\", ""]
    execution = {
        "job_name": "Local\\UploadPlatform-test-" + uid(),
        "stderr_path": str(tmp_path / "stderr.log"),
    }
    saved = []

    def persist(value):
        assert not marker.exists(), "the process must remain suspended until identity is saved"
        assert value["assigned"] and value["pid"] and value["creation_time"]
        saved.append(value)

    program = (
        "import json,sys; from pathlib import Path; "
        "Path(sys.argv[1]).write_text('started',encoding='utf-8'); "
        "print(json.dumps(sys.argv[2:]),flush=True)"
    )
    host = JobHost(
        execution, persist, command=[sys.executable, "-c", program, str(marker), *arguments]
    )
    try:
        assert json.loads(host.messages.get(timeout=15)) == arguments
        assert win32event.WaitForSingleObject(host.process, 15000) == win32event.WAIT_OBJECT_0
        deadline = time.monotonic() + 5
        while host.active_count() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert host.active_count() == 0
        assert marker.read_text(encoding="utf-8") == "started"
        assert len(saved) == 1
    finally:
        if not host.exited() or host.active_count():
            host.stop()
        host.close()


@pytest.mark.parametrize(
    "command", [[], "python -c print(1)", ["python.exe"], [sys.executable, "bad\x00argument"]]
)
def test_invalid_engine_command_never_creates_process(tmp_path, command):
    with pytest.raises(Problem, match="绝对可执行路径"):
        JobHost(
            {"stderr_path": str(tmp_path / "stderr.log")},
            lambda _: pytest.fail("started"),
            command=command,
        )
    assert not (tmp_path / "stderr.log").exists()


def test_hidden_host_has_no_console_and_bounds_output(tmp_path):
    host = JobHost(
        {
            "job_name": "Local\\UploadPlatform-test-" + uid(),
            "stderr_path": str(tmp_path / "stderr.log"),
        },
        lambda _: None,
        command=[
            sys.executable,
            "-c",
            "import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow(),flush=True); print('x'*1024,flush=True)",
        ],
        output_limit=128,
        output_queue_size=4,
    )
    try:
        assert host.messages.get(timeout=10).strip() == "0"
        assert win32event.WaitForSingleObject(host.process, 10000) == win32event.WAIT_OBJECT_0
        host.reader.join(timeout=5)
        assert host.protocol_overflow
    finally:
        if not host.exited() or host.active_count():
            host.stop()
        host.close()
