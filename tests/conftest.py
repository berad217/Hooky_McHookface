"""Shared pytest fixtures for Hooky_McHookface.

Each test that touches storage gets a fresh in-memory SQLite backend, already
connected. Tests do not need to manage connect/close themselves.

App-level fixtures build a ``FastAPI`` via :func:`create_app` with the
test-scoped storage injected — bypassing the real lifespan (which would
overwrite our backend). For HTTP tests we still go through the FastAPI
``TestClient`` so we exercise the real routing layer end-to-end.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from hooky.app import create_app
from hooky.log import configure_logging
from hooky.storage.sqlite import AsyncSQLiteStorage


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    """Reconfigure structlog before each test.

    structlog's ``PrintLoggerFactory`` captures ``sys.stdout`` at configure
    time. ``capsys`` (and our own test of stdout capture) swaps stdout for
    the test, so the previously-bound logger ends up writing to a closed
    file. Re-running ``configure_logging`` between tests ensures the logger
    always holds a live stream.
    """
    configure_logging(level="INFO", json_format=True)
    yield


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[AsyncSQLiteStorage]:
    """An in-memory, connected AsyncSQLiteStorage."""
    backend = AsyncSQLiteStorage(":memory:")
    await backend.connect()
    try:
        yield backend
    finally:
        await backend.close()


@pytest.fixture
def client(storage: AsyncSQLiteStorage) -> Iterator[TestClient]:
    """A FastAPI TestClient with in-memory storage wired in.

    Because we inject storage, the app's lifespan will NOT manage it. This
    keeps the test's connection alive across the request and lets the test
    assert directly against the backend afterward.
    """
    app = create_app(storage=storage)
    with TestClient(app) as test_client:
        yield test_client
