from types import SimpleNamespace

from tools import registry


class FakeSshChannel:
    def __init__(
        self,
        *,
        stdout: list[bytes] | None = None,
        stderr: list[bytes] | None = None,
        exit_ready: bool = True,
        exit_status: int = 0,
    ) -> None:
        self.stdout = list(stdout or [])
        self.stderr = list(stderr or [])
        self.exit_ready = exit_ready
        self.exit_status = exit_status
        self.closed = False

    def recv_ready(self) -> bool:
        return bool(self.stdout)

    def recv(self, _size: int) -> bytes:
        return self.stdout.pop(0)

    def recv_stderr_ready(self) -> bool:
        return bool(self.stderr)

    def recv_stderr(self, _size: int) -> bytes:
        return self.stderr.pop(0)

    def exit_status_ready(self) -> bool:
        return self.exit_ready

    def recv_exit_status(self) -> int:
        return self.exit_status

    def close(self) -> None:
        self.closed = True


def test_read_ssh_command_output_returns_completed_output():
    channel = FakeSshChannel(stdout=[b"hello\n"], stderr=[b"warn\n"], exit_status=7)
    stdout_chan = SimpleNamespace(channel=channel)
    stderr_chan = SimpleNamespace(channel=channel)

    exit_status, stdout, stderr, timed_out = registry._read_ssh_command_output(stdout_chan, stderr_chan, 5)

    assert exit_status == 7
    assert stdout == "hello\n"
    assert stderr == "warn\n"
    assert timed_out is False
    assert channel.closed is False


def test_read_ssh_command_output_closes_channel_on_timeout(monkeypatch):
    current = {"value": -0.6}

    def fake_monotonic() -> float:
        current["value"] += 0.6
        return current["value"]

    monkeypatch.setattr(registry.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(registry.time, "sleep", lambda _seconds: None)
    channel = FakeSshChannel(stdout=[b"partial\n"], exit_ready=False)
    stdout_chan = SimpleNamespace(channel=channel)
    stderr_chan = SimpleNamespace(channel=channel)

    exit_status, stdout, stderr, timed_out = registry._read_ssh_command_output(stdout_chan, stderr_chan, 1)

    assert exit_status is None
    assert stdout == "partial\n"
    assert stderr == ""
    assert timed_out is True
    assert channel.closed is True
