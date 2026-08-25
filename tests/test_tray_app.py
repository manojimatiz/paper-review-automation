"""The tray's process-management logic — no pystray/display needed for any
of this; the GUI wiring in build_icon() is exercised manually, not here."""

import pytest

import tray_app


class FakeProcess:
    """Stands in for subprocess.Popen. `exits_immediately` simulates
    main()'s LAN-without-accounts gate refusing to start."""

    def __init__(self, exits_immediately=False, returncode=1):
        self._alive = not exits_immediately
        self.returncode = returncode if exits_immediately else None
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._alive else self.returncode

    def terminate(self):
        self.terminated = True
        self._alive = False
        self.returncode = 0

    def kill(self):
        self.killed = True
        self._alive = False
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """The controller sleeps to let a subprocess fail fast — instant in tests."""
    monkeypatch.setattr(tray_app.time, "sleep", lambda _seconds: None)


@pytest.fixture
def no_port_conflict(monkeypatch):
    """By default nothing is "already listening" — most tests want a clean
    slate rather than accidentally colliding with a real local server."""
    monkeypatch.setattr(tray_app, "_port_is_open", lambda *a, **kw: False)


def test_start_spawns_the_expected_subprocess(monkeypatch, no_port_conflict, tmp_path):
    calls = []

    def fake_popen(args, cwd=None, **kwargs):
        calls.append((args, cwd, kwargs))
        return FakeProcess()

    monkeypatch.setattr(tray_app.subprocess, "Popen", fake_popen)
    controller = tray_app.ServerController(base_dir=tmp_path, port=5099)

    ok, message = controller.start()

    assert ok is True
    assert controller.running is True
    args, cwd, kwargs = calls[0]
    assert args[1:] == ["ui.py", "--no-browser", "--port", "5099"]
    assert cwd == str(tmp_path)
    # The whole point of this fix: no console flash from the spawned server.
    assert kwargs.get("creationflags") == tray_app._CREATE_NO_WINDOW


def test_start_when_already_running_does_not_spawn_a_second_process(monkeypatch, no_port_conflict, tmp_path):
    calls = []
    monkeypatch.setattr(tray_app.subprocess, "Popen", lambda *a, **kw: calls.append(1) or FakeProcess())
    controller = tray_app.ServerController(base_dir=tmp_path, port=5099)

    controller.start()
    ok, message = controller.start()

    assert ok is True
    assert "Already running" in message
    assert len(calls) == 1


def test_start_detects_a_process_that_exits_immediately(monkeypatch, no_port_conflict, tmp_path):
    monkeypatch.setattr(
        tray_app.subprocess, "Popen",
        lambda *a, **kw: FakeProcess(exits_immediately=True, returncode=2),
    )
    controller = tray_app.ServerController(base_dir=tmp_path, port=5099)

    ok, message = controller.start()

    assert ok is False
    assert controller.running is False
    assert "Could not start" in message
    assert "2" in message


def test_stop_terminates_the_tracked_process(monkeypatch, no_port_conflict, tmp_path):
    process = FakeProcess()
    monkeypatch.setattr(tray_app.subprocess, "Popen", lambda *a, **kw: process)
    controller = tray_app.ServerController(base_dir=tmp_path, port=5099)
    controller.start()

    ok, message = controller.stop()

    assert ok is True
    assert process.terminated is True
    assert controller.running is False


def test_stop_when_already_stopped_is_a_no_op(no_port_conflict, tmp_path):
    controller = tray_app.ServerController(base_dir=tmp_path, port=5099)
    ok, message = controller.stop()
    assert ok is True
    assert "Already stopped" in message


def test_stop_kills_if_terminate_does_not_finish_in_time(monkeypatch, no_port_conflict, tmp_path):
    class SlowProcess(FakeProcess):
        def wait(self, timeout=None):
            if not self.killed:
                import subprocess as sp
                raise sp.TimeoutExpired(cmd="x", timeout=timeout)
            return self.returncode

    process = SlowProcess()
    monkeypatch.setattr(tray_app.subprocess, "Popen", lambda *a, **kw: process)
    controller = tray_app.ServerController(base_dir=tmp_path, port=5099)
    controller.start()

    controller.stop()

    assert process.killed is True


def test_running_is_true_when_something_else_is_already_on_the_port(monkeypatch, tmp_path):
    """Guards against binding a second server to the same port."""
    monkeypatch.setattr(tray_app, "_port_is_open", lambda *a, **kw: True)
    controller = tray_app.ServerController(base_dir=tmp_path, port=5099)
    assert controller.running is True


def test_start_does_not_spawn_when_the_port_is_already_taken(monkeypatch, tmp_path):
    monkeypatch.setattr(tray_app, "_port_is_open", lambda *a, **kw: True)
    calls = []
    monkeypatch.setattr(tray_app.subprocess, "Popen", lambda *a, **kw: calls.append(1))
    controller = tray_app.ServerController(base_dir=tmp_path, port=5099)

    ok, message = controller.start()

    assert ok is True
    assert calls == []


# --- open_or_start() ---------------------------------------------------------


def test_open_or_start_just_opens_the_browser_when_already_running(monkeypatch):
    monkeypatch.setattr(tray_app, "_port_is_open", lambda *a, **kw: True)
    spawned = []
    monkeypatch.setattr(tray_app.subprocess, "Popen", lambda *a, **kw: spawned.append(1))
    opened = []
    monkeypatch.setattr(tray_app.webbrowser, "open", lambda url: opened.append(url))

    tray_app.open_or_start()

    assert spawned == []
    assert opened == [f"http://127.0.0.1:{tray_app.DEFAULT_PORT}/"]


def test_open_or_start_launches_the_tray_when_nothing_is_running(monkeypatch):
    monkeypatch.setattr(tray_app, "_port_is_open", lambda *a, **kw: False)
    monkeypatch.setattr(tray_app.time, "sleep", lambda _s: None)
    spawned = []
    monkeypatch.setattr(tray_app.subprocess, "Popen", lambda args, cwd=None, **kw: spawned.append(args) or FakeProcess())
    opened = []
    monkeypatch.setattr(tray_app.webbrowser, "open", lambda url: opened.append(url))

    tray_app.open_or_start()

    assert len(spawned) == 1
    assert "tray_app.py" in spawned[0][1]
    assert opened == [f"http://127.0.0.1:{tray_app.DEFAULT_PORT}/"]  # always loopback, never a LAN URL
