"""Test isolation for the durable stores: each test gets its own SQLite file in a
tmp dir, so the conversation log + local users never touch a real volume."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_stores(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_CHANNEL_DB", str(tmp_path / "web_channel.db"))
    import web_channel.convlog as convlog

    convlog._initialized = False
    try:
        import web_channel.localusers as localusers

        localusers._initialized = False
    except ImportError:
        pass  # localusers added in phase C
    yield
