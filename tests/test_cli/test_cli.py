"""CLI tests via Click's CliRunner.

Commands operate on a file-backed temp SQLite DB so the CLI's own
connection (opened inside asyncio.run) sees the rows we seed from the test.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio
from click.testing import CliRunner

from hooky.cli.main import cli
from hooky.storage.models import WebhookRequest
from hooky.storage.sqlite import AsyncSQLiteStorage


def _make_webhook(path: str = "/webhook/test", body: bytes = b'{"x": 1}') -> WebhookRequest:
    return WebhookRequest(
        path=path,
        method="POST",
        headers={"content-type": "application/json", "x-test": "abc"},
        body=body,
        body_json={"x": 1},
        received_at=datetime.now(UTC),
        source_ip="127.0.0.1",
    )


@pytest_asyncio.fixture
async def populated_db(tmp_path):
    """A file-backed DB with three stored webhooks; returns (path, ids)."""
    db_path = tmp_path / "cli.db"
    storage = AsyncSQLiteStorage(str(db_path))
    await storage.connect()
    ids = []
    ids.append((await storage.store(_make_webhook("/webhook/github"))).id)
    ids.append((await storage.store(_make_webhook("/webhook/stripe"))).id)
    ids.append((await storage.store(_make_webhook("/webhook/github"))).id)
    await storage.close()
    return str(db_path), ids


def test_list_shows_webhooks(populated_db) -> None:
    db_path, ids = populated_db
    runner = CliRunner()
    result = runner.invoke(cli, ["list", "--db", db_path])
    assert result.exit_code == 0, result.output
    assert "3 webhook(s)." in result.output
    for wid in ids:
        assert wid in result.output


def test_list_empty_db(tmp_path) -> None:
    db_path = tmp_path / "empty.db"
    runner = CliRunner()
    result = runner.invoke(cli, ["list", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "No webhooks stored." in result.output


def test_list_path_filter(populated_db) -> None:
    db_path, _ids = populated_db
    runner = CliRunner()
    result = runner.invoke(cli, ["list", "--db", db_path, "--path", "/webhook/stripe"])
    assert result.exit_code == 0, result.output
    assert "1 webhook(s)." in result.output
    assert "/webhook/stripe" in result.output
    assert "/webhook/github" not in result.output


def test_list_limit(populated_db) -> None:
    db_path, _ids = populated_db
    runner = CliRunner()
    result = runner.invoke(cli, ["list", "--db", db_path, "--limit", "2"])
    assert result.exit_code == 0, result.output
    assert "2 webhook(s)." in result.output


def test_show_detail(populated_db) -> None:
    db_path, ids = populated_db
    runner = CliRunner()
    result = runner.invoke(cli, ["show", ids[0], "--db", db_path])
    assert result.exit_code == 0, result.output
    assert ids[0] in result.output
    assert "/webhook/github" in result.output
    assert "Headers:" in result.output
    assert "x-test: abc" in result.output
    assert "Handler runs:" in result.output
    assert "(none)" in result.output  # no handlers ran


def test_show_unknown_id_exits_1(populated_db) -> None:
    db_path, _ids = populated_db
    runner = CliRunner()
    result = runner.invoke(cli, ["show", "01NOPE", "--db", db_path])
    assert result.exit_code == 1
    assert "No webhook with id" in result.output


def test_replay_without_handlers_exits_2(populated_db) -> None:
    """No handlers configured -> replay refuses with exit code 2."""
    db_path, ids = populated_db
    runner = CliRunner()
    result = runner.invoke(cli, ["replay", ids[0], "--db", db_path])
    assert result.exit_code == 2
    assert "No handlers configured" in result.output


def test_replay_with_configured_handler(populated_db, tmp_path) -> None:
    """A config with a handlers section makes replay dispatch to it."""
    db_path, ids = populated_db
    config_text = f"""
storage:
  path: {db_path}
handlers:
  - path: "hooky.handlers.echo:EchoHandler"
tunnel:
  provider: none
"""
    config_file = tmp_path / "replay.yaml"
    config_file.write_text(config_text, encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli, ["replay", ids[0], "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    assert "Replayed" in result.output
    assert "echo" in result.output
    assert "[OK ]" in result.output


def test_replay_unknown_handler_name(populated_db, tmp_path) -> None:
    db_path, ids = populated_db
    config_text = f"""
storage:
  path: {db_path}
handlers:
  - path: "hooky.handlers.echo:EchoHandler"
tunnel:
  provider: none
"""
    config_file = tmp_path / "replay.yaml"
    config_file.write_text(config_text, encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        cli, ["replay", ids[0], "--config", str(config_file), "--handler", "nope"]
    )
    assert result.exit_code == 1
    assert "Replay failed" in result.output


def test_version_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "hooky" in result.output.lower()
