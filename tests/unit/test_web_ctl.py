from pathlib import Path

from hosts.web import ctl


def test_port_probe_host_uses_loopback_for_wildcard_hosts() -> None:
    assert ctl._port_probe_host("0.0.0.0") == "127.0.0.1"
    assert ctl._port_probe_host("") == "127.0.0.1"
    assert ctl._port_probe_host("::") == "::1"
    assert ctl._port_probe_host("localhost") == "localhost"


def test_persist_running_pid_prefers_listener_pid(
    monkeypatch,
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "web" / "server.pid"
    monkeypatch.setattr(ctl, "_find_pid_by_port", lambda port: 83872)

    pid = ctl._persist_running_pid(85500, 60000, home=tmp_path)

    assert pid == 83872
    assert pid_file.read_text(encoding="utf-8") == "83872"


def test_persist_running_pid_falls_back_to_started_pid(
    monkeypatch,
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "web" / "server.pid"
    monkeypatch.setattr(ctl, "_find_pid_by_port", lambda port: None)

    pid = ctl._persist_running_pid(85500, 60000, home=tmp_path)

    assert pid == 85500
    assert pid_file.read_text(encoding="utf-8") == "85500"
