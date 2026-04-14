"""Pytest configuration for shiplog tests."""

from unittest.mock import patch

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--network", action="store_true", default=False,
        help="Run tests that require network access",
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--network"):
        skip = pytest.mark.skip(reason="Needs --network flag")
        for item in items:
            if "network" in item.keywords:
                item.add_marker(skip)


@pytest.fixture(autouse=True)
def _no_ntfy(monkeypatch):
    """Block ntfy notifications in all tests."""
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("NTFY_ENDPOINT", raising=False)
    monkeypatch.delenv("NTFY_TOKEN", raising=False)
    monkeypatch.delenv("NTFY_PRIORITY", raising=False)


@pytest.fixture(autouse=True)
def _no_dotenv_leak(monkeypatch):
    """Prevent dotenv from loading real .env files during tests."""
    monkeypatch.setattr("shiplog.cli.load_dotenv", lambda *a, **kw: None)
