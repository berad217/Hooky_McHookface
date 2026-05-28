"""Tests for `hooky start` / `hooky stop` with the server + signals mocked.

We never launch a real uvicorn server here — ``uvicorn.run`` and
``create_app`` are patched so we can assert the CLI wiring (pidfile
handling, host/port resolution, signal dispatch) deterministically.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from hooky.cli.main import _bind_exclusive_socket, cli


def test_start_posix_writes_pidfile_and_runs_uvicorn(tmp_path) -> None:
    """POSIX: start() builds the app, writes a pidfile, calls uvicorn.run, cleans up."""
    pidfile = tmp_path / "hooky.pid"
    fake_app = object()

    with patch("hooky.cli.main._IS_WINDOWS", False), patch(
        "hooky.app.create_app", return_value=fake_app
    ) as mk_app, patch("uvicorn.run") as mk_run:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "start",
                "--db", str(tmp_path / "x.db"),
                "--host", "127.0.0.1",
                "--port", "9999",
                "--pidfile", str(pidfile),
            ],
        )

    assert result.exit_code == 0, result.output
    mk_app.assert_called_once()
    mk_run.assert_called_once()
    # host/port flowed into uvicorn.run
    _, kwargs = mk_run.call_args
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 9999
    # pidfile removed in the finally block after uvicorn.run returns
    assert not pidfile.exists()


def test_start_windows_binds_exclusive_socket(tmp_path) -> None:
    """Windows: start() claims the port via an exclusive pre-bound socket and hands
    it to uvicorn — never the plain uvicorn.run (SO_REUSEADDR) path — so a stray
    binder can't shadow it. REGRESSION: the two-day port-shadow incident."""
    pidfile = tmp_path / "hooky.pid"
    fake_app = object()
    fake_sock = object()

    with patch("hooky.cli.main._IS_WINDOWS", True), patch(
        "hooky.app.create_app", return_value=fake_app
    ), patch(
        "hooky.cli.main._bind_exclusive_socket", return_value=fake_sock
    ) as mk_bind, patch("uvicorn.Server") as mk_server, patch("uvicorn.run") as mk_run:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "start",
                "--db", str(tmp_path / "x.db"),
                "--host", "0.0.0.0",
                "--port", "8473",
                "--pidfile", str(pidfile),
            ],
        )

    assert result.exit_code == 0, result.output
    # Must NOT fall through to the plain SO_REUSEADDR uvicorn.run path.
    mk_run.assert_not_called()
    mk_bind.assert_called_once_with("0.0.0.0", 8473)
    # The exclusive socket is handed to the server, not re-bound by uvicorn.
    mk_server.return_value.run.assert_called_once()
    _, run_kwargs = mk_server.return_value.run.call_args
    assert run_kwargs["sockets"] == [fake_sock]
    assert not pidfile.exists()


@pytest.mark.skipif(os.name != "nt", reason="SO_EXCLUSIVEADDRUSE is Windows-only")
def test_bind_exclusive_socket_rejects_reuseaddr_intruder() -> None:
    """REGRESSION (port-shadow): once we hold the port exclusively, a second binder
    using SO_REUSEADDR — exactly what `python -m http.server` does — is rejected
    instead of silently co-binding and stealing our traffic."""
    held = _bind_exclusive_socket("127.0.0.1", 0)  # port 0 -> OS picks a free port
    try:
        port = held.getsockname()[1]
        intruder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        intruder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            with pytest.raises(OSError):
                intruder.bind(("127.0.0.1", port))
        finally:
            intruder.close()
    finally:
        held.close()


def test_stop_no_pidfile_exits_1(tmp_path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["stop", "--pidfile", str(tmp_path / "absent.pid")])
    assert result.exit_code == 1
    assert "is the server running" in result.output


def test_stop_posix_sends_sigterm(tmp_path) -> None:
    """POSIX: stop() sends a graceful SIGTERM so uvicorn runs its shutdown
    (lifespan teardown -> tunnel.stop() reaps cloudflared). The pidfile is left
    in place for `start`'s own graceful-exit cleanup."""
    pidfile = tmp_path / "hooky.pid"
    pidfile.write_text("4242", encoding="utf-8")

    with patch("hooky.cli.main._IS_WINDOWS", False), patch(
        "hooky.cli.main.os.kill"
    ) as mk_kill:
        runner = CliRunner()
        result = runner.invoke(cli, ["stop", "--pidfile", str(pidfile)])

    assert result.exit_code == 0, result.output
    mk_kill.assert_called_once_with(4242, signal.SIGTERM)
    assert "Sent stop signal to pid 4242" in result.output
    # POSIX leaves the pidfile for `start` to remove on graceful exit.
    assert pidfile.exists()


def test_stop_windows_kills_process_tree(tmp_path) -> None:
    """REGRESSION (orphaned cloudflared): on Windows, `os.kill(SIGTERM)` is a
    hard TerminateProcess that skips uvicorn's graceful shutdown, so the lifespan
    teardown never runs and the cloudflared child is orphaned. stop() must
    instead tree-kill (`taskkill /T`) so the child is reaped, and remove the
    pidfile itself (since `start`'s finally can't run after a hard kill)."""
    pidfile = tmp_path / "hooky.pid"
    pidfile.write_text("4242", encoding="utf-8")

    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("hooky.cli.main._IS_WINDOWS", True), patch(
        "hooky.cli.main.subprocess.run", return_value=completed
    ) as mk_run, patch("hooky.cli.main.os.kill") as mk_kill:
        runner = CliRunner()
        result = runner.invoke(cli, ["stop", "--pidfile", str(pidfile)])

    assert result.exit_code == 0, result.output
    # Must NOT fall through to the single-process POSIX path.
    mk_kill.assert_not_called()
    mk_run.assert_called_once()
    argv = mk_run.call_args[0][0]
    assert argv[0] == "taskkill"
    assert "/T" in argv  # tree kill -> reaps the cloudflared child
    assert "4242" in argv
    # Pidfile removed by stop() because `start`'s finally never runs.
    assert not pidfile.exists()
    assert "child processes" in result.output


def test_stop_posix_stale_pidfile_cleaned(tmp_path) -> None:
    """POSIX: if the pid is gone (ProcessLookupError), stop cleans up the stale pidfile."""
    pidfile = tmp_path / "hooky.pid"
    pidfile.write_text("4242", encoding="utf-8")

    with patch("hooky.cli.main._IS_WINDOWS", False), patch(
        "hooky.cli.main.os.kill", side_effect=ProcessLookupError()
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["stop", "--pidfile", str(pidfile)])

    assert result.exit_code == 0, result.output
    assert "stale pidfile" in result.output
    assert not pidfile.exists()


def test_stop_windows_stale_pid_cleaned(tmp_path) -> None:
    """Windows: taskkill exit 128 (no such process) maps onto the same stale
    pidfile cleanup as the POSIX ProcessLookupError path."""
    pidfile = tmp_path / "hooky.pid"
    pidfile.write_text("4242", encoding="utf-8")

    completed = subprocess.CompletedProcess(
        args=[], returncode=128, stdout="", stderr='ERROR: The process "4242" not found.'
    )
    with patch("hooky.cli.main._IS_WINDOWS", True), patch(
        "hooky.cli.main.subprocess.run", return_value=completed
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["stop", "--pidfile", str(pidfile)])

    assert result.exit_code == 0, result.output
    assert "stale pidfile" in result.output
    assert not pidfile.exists()


def test_stop_windows_taskkill_failure_exits_1(tmp_path) -> None:
    """Windows: a non-zero, non-128 taskkill exit surfaces as a failure and does
    NOT delete the pidfile (the server may well still be running)."""
    pidfile = tmp_path / "hooky.pid"
    pidfile.write_text("4242", encoding="utf-8")

    completed = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="ERROR: Access is denied."
    )
    with patch("hooky.cli.main._IS_WINDOWS", True), patch(
        "hooky.cli.main.subprocess.run", return_value=completed
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["stop", "--pidfile", str(pidfile)])

    assert result.exit_code == 1
    assert "Failed to signal pid 4242" in result.output
    assert "Access is denied" in result.output
    assert pidfile.exists()


def test_stop_unreadable_pid_exits_1(tmp_path) -> None:
    pidfile = tmp_path / "hooky.pid"
    pidfile.write_text("not-a-number", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli, ["stop", "--pidfile", str(pidfile)])
    assert result.exit_code == 1
    assert "Could not read pid" in result.output
