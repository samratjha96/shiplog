"""Pytest configuration for shiplog tests."""

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
